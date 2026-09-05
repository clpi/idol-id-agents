from __future__ import annotations

from contextlib import contextmanager
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
    ControllerLease,
    RepositoryClaimTransaction,
    SemanticClaimStore,
)


class ClaimTests(unittest.TestCase):
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
