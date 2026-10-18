from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from fleet_control.claims import (
    ClaimConflict,
    ClaimCommandError,
    ControllerLease,
    RepositoryClaimTransaction,
    SemanticClaimStore,
)


class ClaimTests(unittest.TestCase):
    def test_failed_unlock_closes_handle_and_preserves_committed_state(self) -> None:
        for failure in (OSError, KeyboardInterrupt):
            for operation in ("constructor", "list", "acquire", "renew", "release"):
                with self.subTest(failure=failure, operation=operation), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    store = SemanticClaimStore(root)
                    expired = asdict(store.acquire(owner="expired", task_id="expired", targets=("graph/expired",), ttl_seconds=30, now=0)[0])
                    active = asdict(store.acquire(owner="one", task_id="task-one", targets=("graph/application",), ttl_seconds=60, now=10)[0])
                    before = store.path.read_bytes()
                    expected = {
                        "constructor": [expired, active],
                        "list": [active],
                        "acquire": [active, dict(owner="two", task_id="task-two", target="graph/other", acquired_at=50, expires_at=110)],
                        "renew": [{**active, "expires_at": 140}],
                        "release": [expired],
                    }[operation]
                    operations = {
                        "constructor": lambda: SemanticClaimStore(root),
                        "list": lambda: store.list(now=50),
                        "acquire": lambda: store.acquire(owner="two", task_id="task-two", targets=("graph/other",), ttl_seconds=60, now=50),
                        "renew": lambda: store.renew(owner="one", task_id="task-one", ttl_seconds=90, now=50),
                        "release": lambda: store.release(owner="one", task_id="task-one"),
                    }
                    original_open = Path.open
                    original_flock = fcntl.flock
                    opened = []
                    calls = []
                    captured = None

                    def track(path, *args, **kwargs):
                        handle = original_open(path, *args, **kwargs)
                        if path == store.lock_path:
                            opened.append(handle)
                        return handle

                    def fail_unlock(fd, mode):
                        calls.append(mode)
                        if len(calls) == 2:
                            self.assertEqual(mode, fcntl.LOCK_UN)
                            raise failure("planted unlock failure")
                        return original_flock(fd, mode)

                    try:
                        with mock.patch.object(Path, "open", track), mock.patch.object(fcntl, "flock", side_effect=fail_unlock):
                            try:
                                operations[operation]()
                            except failure as exc:
                                captured = exc
                        self.assertIsInstance(captured, failure)
                        self.assertEqual(str(captured), "planted unlock failure")
                        self.assertEqual(calls, [fcntl.LOCK_EX, fcntl.LOCK_UN])
                        self.assertEqual(len(opened), 1)
                        self.assertTrue(opened[0].closed)
                        with store.lock_path.open("a+") as handle:
                            original_flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            original_flock(handle.fileno(), fcntl.LOCK_UN)
                        self.assertEqual(json.loads(store.path.read_bytes()), expected)
                        if operation == "constructor":
                            self.assertEqual(store.path.read_bytes(), before)
                    finally:
                        for handle in opened:
                            handle.close()

    def test_failed_lock_acquisition_closes_handle_without_mutating_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SemanticClaimStore(root)
            store.acquire(owner="one", task_id="task-one", targets=("graph/application",), ttl_seconds=60, now=10)
            before = store.path.read_bytes()
            original = Path.open
            operations = (
                lambda: SemanticClaimStore(root),
                lambda: store.list(now=11),
                lambda: store.acquire(owner="two", task_id="task-two", targets=("graph/application",), ttl_seconds=60, now=11),
                lambda: store.renew(owner="one", task_id="task-one", ttl_seconds=60, now=11),
                lambda: store.release(owner="one", task_id="task-one"),
            )
            for failure in (OSError, KeyboardInterrupt):
                for operation in operations:
                    with self.subTest(failure=failure, operation=operation):
                        opened = []

                        def track(path, *args, **kwargs):
                            handle = original(path, *args, **kwargs)
                            if path == store.lock_path:
                                opened.append(handle)
                            return handle

                        with mock.patch.object(Path, "open", track), mock.patch.object(fcntl, "flock", side_effect=failure("planted lock failure")):
                            with self.assertRaises(failure):
                                operation()
                        self.assertEqual(len(opened), 1)
                        self.assertTrue(opened[0].closed)
                        self.assertEqual(store.path.read_bytes(), before)

    def test_constructor_initialization_holds_store_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initializing = threading.Event()
            proceed = threading.Event()
            competing = threading.Event()
            failures: list[BaseException] = []
            original = SemanticClaimStore._write

            def delayed_write(store, rows):
                if threading.current_thread().name == "first-initializer" and rows == []:
                    initializing.set()
                    if not proceed.wait(3):
                        raise AssertionError("initialization was not resumed")
                original(store, rows)

            def initialize() -> None:
                try:
                    SemanticClaimStore(root)
                except BaseException as exc:
                    failures.append(exc)

            def acquire() -> None:
                competing.set()
                try:
                    store = SemanticClaimStore(root)
                    store.acquire(owner="two", task_id="task-two", targets=("graph/application",), ttl_seconds=60, now=10)
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch.object(SemanticClaimStore, "_write", delayed_write):
                first = threading.Thread(target=initialize, name="first-initializer")
                second = threading.Thread(target=acquire, name="second-initializer")
                first.start()
                try:
                    self.assertTrue(initializing.wait(2))
                    second.start()
                    self.assertTrue(competing.wait(2))
                    with (root / "semantic-claims.lock").open("a+") as handle:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    proceed.set()
                    first.join(3)
                    if second.ident is not None:
                        second.join(3)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(failures, [])
            self.assertEqual(tuple(row.owner for row in SemanticClaimStore(root).list(now=11)), ("two",))

    def test_corrupt_rows_refuse_all_operations_without_rewriting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            claim = store.acquire(owner="one", task_id="task-one", targets=("graph/application",), ttl_seconds=60, now=10)[0]
            valid = asdict(claim)
            damaged = []
            for field in valid:
                row = dict(valid)
                del row[field]
                damaged.append(("missing-" + field, row))
            for field in ("owner", "task_id", "target"):
                for value in (None, 1, True, "", " "):
                    damaged.append((field + "=" + repr(value), {**valid, field: value}))
            for field in ("acquired_at", "expires_at"):
                for value in (None, "60", True, -1, float("nan"), float("inf"), -float("inf"), 10 ** 1000):
                    damaged.append((field + "=" + repr(value), {**valid, field: value}))
            damaged.extend((
                ("zero-duration", {**valid, "expires_at": 10}),
                ("reversed-time", {**valid, "expires_at": 9}),
                ("extra-field", {**valid, "unrecognized": 1}),
                ("invalid-target", {**valid, "target": "graph//application"}),
            ))
            operations = (
                ("list", lambda: store.list(now=11)),
                ("acquire", lambda: store.acquire(owner="two", task_id="task-two", targets=("graph/application",), ttl_seconds=60, now=11)),
                ("renew", lambda: store.renew(owner="one", task_id="task-one", ttl_seconds=60, now=11)),
                ("release", lambda: store.release(owner="one", task_id="task-one")),
            )
            for damage, row in damaged:
                for operation, call in operations:
                    with self.subTest(damage=damage, operation=operation):
                        original = json.dumps([row]).encode()
                        store.path.write_bytes(original)
                        with self.assertRaises(ClaimCommandError):
                            call()
                        self.assertEqual(store.path.read_bytes(), original)

    def test_invalid_clock_or_ttl_cannot_discard_or_shorten_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(owner="one", task_id="task-one", targets=("graph/application",), ttl_seconds=60, now=10)
            original = store.path.read_bytes()
            for now in (True, -1, float("nan"), float("inf"), "11", 10 ** 1000):
                for operation in ("list", "acquire", "renew"):
                    with self.subTest(now=repr(now), operation=operation):
                        store.path.write_bytes(original)
                        kwargs = {"now": now}
                        if operation != "list":
                            kwargs.update(owner="one", task_id="task-one", ttl_seconds=60)
                        if operation == "acquire":
                            kwargs["targets"] = ("graph/application",)
                        with self.assertRaises(ValueError):
                            getattr(store, operation)(**kwargs)
                        self.assertEqual(store.path.read_bytes(), original)
            for ttl in (True, -1, 29, 86401, 60.5, float("nan"), float("inf"), "60"):
                for operation in ("acquire", "renew"):
                    with self.subTest(ttl=repr(ttl), operation=operation):
                        store.path.write_bytes(original)
                        kwargs = dict(owner="one", task_id="task-one", ttl_seconds=ttl, now=11)
                        if operation == "acquire":
                            kwargs["targets"] = ("graph/application",)
                        with self.assertRaises(ValueError):
                            getattr(store, operation)(**kwargs)
                        self.assertEqual(store.path.read_bytes(), original)

    def test_renewal_and_exact_expiry_preserve_ownership_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(owner="one", task_id="task-one", targets=("graph/application",), ttl_seconds=60, now=10)
            renewed = store.renew(owner="one", task_id="task-one", ttl_seconds=90, now=50)
            self.assertEqual(renewed[0].acquired_at, 10)
            self.assertEqual(renewed[0].expires_at, 140)
            with self.assertRaises(ClaimConflict):
                store.acquire(owner="two", task_id="task-two", targets=("graph/application/operand",), ttl_seconds=60, now=139)
            self.assertEqual(store.list(now=140), ())
            acquired = store.acquire(owner="two", task_id="task-two", targets=("graph/application",), ttl_seconds=60, now=140)
            self.assertEqual(acquired[0].owner, "two")

    def test_independent_roots_are_only_local_claim_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("first", "second"):
                store = SemanticClaimStore(root / name)
                self.assertEqual(len(store.acquire(owner=name, task_id=name, targets=("graph/application",), ttl_seconds=60, now=10)), 1)

    @staticmethod
    def claim_repository(root: Path, body: str) -> Path:
        repository = root / "repository"
        command = repository / "tools/node/dev/claim"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        command.chmod(0o755)
        return repository

    def test_parent_and_child_semantic_claims_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(
                owner="one",
                task_id="task-one",
                targets=("world/process",),
                ttl_seconds=60,
                now=10,
            )
            with self.assertRaises(ClaimConflict):
                store.acquire(
                    owner="two",
                    task_id="task-two",
                    targets=("world/process/run",),
                    ttl_seconds=60,
                    now=11,
                )

    def test_disjoint_semantic_claims_can_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(
                owner="one",
                task_id="task-one",
                targets=("world/process",),
                ttl_seconds=60,
                now=10,
            )
            store.acquire(
                owner="two",
                task_id="task-two",
                targets=("graph/application",),
                ttl_seconds=60,
                now=11,
            )
            self.assertEqual(len(store.list(now=12)), 2)

    def test_expired_claim_is_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(
                owner="one",
                task_id="task-one",
                targets=("world/process",),
                ttl_seconds=60,
                now=10,
            )
            self.assertFalse(store.list(now=70))

    def test_release_removes_only_matching_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(owner="one", task_id="one", targets=("a",), ttl_seconds=60, now=1)
            store.acquire(owner="two", task_id="two", targets=("b",), ttl_seconds=60, now=1)
            store.release(owner="one", task_id="one")
            rows = store.list(now=2)
            self.assertEqual(tuple(row.target for row in rows), ("b",))

    def test_controller_lease_is_singleton(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.lock"
            with ControllerLease(path):
                with self.assertRaises(ClaimConflict):
                    with ControllerLease(path):
                        self.fail("second lease should not be acquired")

    def test_repository_guard_refusal_launches_no_acquire(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "claim.log"
            repository = self.claim_repository(
                root,
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n",
            )

            @contextmanager
            def reject_acquire():
                raise ClaimConflict("local control disabled")
                yield

            transaction = RepositoryClaimTransaction(
                repository=repository,
                owner="owner",
                task_id="task",
                paths=("src/a",),
                ttl_seconds=60,
                acquire_guard=reject_acquire,
            )
            with self.assertRaisesRegex(ClaimConflict, "local control disabled"):
                transaction.__enter__()
            self.assertFalse(log.exists())

    def test_repository_guard_is_released_while_acquire_child_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = root / "started"
            finish = root / "finish"
            repository = self.claim_repository(
                root,
                "if [ \"${1:-}\" = acquire ]; then\n"
                f"  : > {shlex.quote(str(started))}\n"
                f"  while [ ! -e {shlex.quote(str(finish))} ]; do sleep 0.01; done\n"
                "fi\n",
            )
            guard_released = threading.Event()

            @contextmanager
            def acquire_guard():
                try:
                    yield
                finally:
                    guard_released.set()

            transaction = RepositoryClaimTransaction(
                repository=repository,
                owner="owner",
                task_id="task",
                paths=("src/a",),
                ttl_seconds=60,
                acquire_guard=acquire_guard,
            )
            failure: list[BaseException] = []

            def acquire() -> None:
                try:
                    transaction.__enter__()
                except BaseException as exc:
                    failure.append(exc)

            worker = threading.Thread(target=acquire)
            worker.start()
            try:
                self.assertTrue(guard_released.wait(2), "guard remained held during communicate")
                deadline = time.monotonic() + 2
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists(), "claim helper did not start")
                self.assertTrue(worker.is_alive(), "claim helper did not remain active")
            finally:
                finish.touch()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failure, [])
            transaction.release()

    def test_repository_guard_covers_renew_but_not_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "claim.log"
            repository = self.claim_repository(
                root,
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n",
            )
            guarded_launches = 0
            reject = False

            @contextmanager
            def acquire_guard():
                nonlocal guarded_launches
                guarded_launches += 1
                if reject:
                    raise ClaimConflict("guard must not wrap release")
                yield

            transaction = RepositoryClaimTransaction(
                repository=repository,
                owner="owner",
                task_id="task",
                paths=("src/a",),
                ttl_seconds=60,
                acquire_guard=acquire_guard,
            )
            transaction.__enter__()
            transaction.renew()
            reject = True
            transaction.release()
            self.assertEqual(guarded_launches, 2)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [
                    "acquire owner src/a task",
                    "acquire owner src/a task",
                    "release owner src/a",
                ],
            )

    def test_repository_guard_exit_failure_reaps_and_releases_launched_acquire(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "claim.pid"
            log = root / "claim.log"
            repository = self.claim_repository(
                root,
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
                "if [ \"${1:-}\" = acquire ]; then\n"
                f"  printf '%s\\n' \"$$\" > {shlex.quote(str(pid_file))}\n"
                "  exec sleep 60\n"
                "fi\n",
            )

            @contextmanager
            def acquire_guard():
                yield
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not pid_file.exists():
                    raise AssertionError("claim helper did not start")
                raise ClaimConflict("local control changed during launch")

            transaction = RepositoryClaimTransaction(
                repository=repository,
                owner="owner",
                task_id="task",
                paths=("src/a",),
                ttl_seconds=60,
                acquire_guard=acquire_guard,
            )
            with self.assertRaisesRegex(ClaimConflict, "changed during launch"):
                transaction.__enter__()
            pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertEqual(transaction.acquired, [])
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                ["acquire owner src/a task", "release owner src/a"],
            )

    def test_repository_acquire_timeout_kills_and_reaps_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "claim.pid"
            repository = self.claim_repository(
                root,
                "if [ \"${1:-}\" = acquire ]; then\n"
                f"  printf '%s\\n' \"$$\" > {shlex.quote(str(pid_file))}\n"
                "  exec sleep 60\n"
                "fi\n",
            )
            transaction = RepositoryClaimTransaction(
                repository=repository,
                owner="owner",
                task_id="task",
                paths=("src/a",),
                ttl_seconds=60,
            )
            with mock.patch("fleet_control.claims._CLAIM_TIMEOUT_SECONDS", 0.5):
                with self.assertRaises(subprocess.TimeoutExpired):
                    transaction.__enter__()
            pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
