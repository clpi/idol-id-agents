from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import unittest

from fleet_control.control import (
    ControlIntegrityError,
    ControlRefusal,
    LocalControl,
)


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class LocalControlTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        clock = MutableClock(100)
        root = Path(temporary.name) / "control"
        return temporary, clock, root, LocalControl(root, clock=clock)

    def token(self, control: LocalControl, attempt_id: str = "attempt-1"):
        with control.transition_permit("claim") as guard:
            return guard.issue_attempt_permit(attempt_id)

    def test_missing_status_is_default_off_and_read_only(self) -> None:
        temporary, _, root, control = self.fixture()
        with temporary:
            status = control.status()
            self.assertEqual(status.mode, "disabled")
            self.assertFalse(status.permitted)
            self.assertEqual(status.reason, "missing")
            self.assertFalse(root.exists())
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("claim"):
                    pass

    def test_enable_creates_only_private_single_link_records(self) -> None:
        temporary, _, root, control = self.fixture()
        with temporary:
            status = control.enable(60)
            self.assertTrue(status.permitted)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual({path.name for path in root.iterdir()}, {
                "control-state.json", "control.key", "control.lock",
            })
            for path in root.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_nlink, 1)
            state = json.loads((root / "control-state.json").read_text())
            anchor = json.loads((root / "control.lock").read_text())
            self.assertEqual(state["state_inode"], (root / "control-state.json").stat().st_ino)
            self.assertEqual(anchor["state_inode"], state["state_inode"])
            self.assertEqual(anchor["key_inode"], (root / "control.key").stat().st_ino)

    def test_expired_lease_requires_explicit_refresh_and_preserves_epoch(self) -> None:
        temporary, clock, root, control = self.fixture()
        with temporary:
            control.enable(60)
            first = json.loads((root / "control-state.json").read_text())
            clock.value = 160
            self.assertEqual(control.status().reason, "lease_expired")
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("claim"):
                    pass
            with self.assertRaises(ControlRefusal):
                control.enable(60)
            refreshed = control.refresh(60)
            second = json.loads((root / "control-state.json").read_text())
            self.assertTrue(refreshed.permitted)
            self.assertEqual(second["enable_epoch"], first["enable_epoch"])
            self.assertEqual(second["generation"], first["generation"] + 1)

    def test_refresh_never_enables_disabled_or_draining_state(self) -> None:
        temporary, _, _, control = self.fixture()
        with temporary:
            control.disable()
            with self.assertRaises(ControlRefusal):
                control.refresh(60)
            control.enable(60)
            control.drain()
            with self.assertRaises(ControlRefusal):
                control.refresh(60)
            with self.assertRaisesRegex(ControlRefusal, "disable"):
                control.enable(60)
            control.disable()
            self.assertTrue(control.enable(60).permitted)

    def test_claim_guard_mints_token_only_while_held(self) -> None:
        temporary, _, _, control = self.fixture()
        with temporary:
            control.enable(60)
            with control.transition_permit("claim") as guard:
                token = guard.issue_attempt_permit("owned-attempt")
                with self.assertRaises(ControlRefusal):
                    guard.issue_attempt_permit("owned-attempt")
            with self.assertRaises(ControlRefusal):
                guard.issue_attempt_permit("late-attempt")
            with control.transition_permit("worktree", token) as status:
                self.assertTrue(status.permitted)
            with control.transition_permit("claim", token):
                pass
            with control.transition_permit("process", token):
                pass

    def test_maintenance_requires_a_current_enable_lease_and_never_mints(self) -> None:
        temporary, clock, _, control = self.fixture()
        with temporary:
            control.enable(60)
            with control.transition_permit("maintenance") as status:
                self.assertTrue(status.permitted)
                self.assertFalse(hasattr(status, "issue_attempt_permit"))
            token = self.token(control)
            with self.assertRaises(ValueError):
                with control.transition_permit("maintenance", token):
                    pass
            clock.value = 160
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("maintenance"):
                    pass
            control.disable()
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("maintenance"):
                    pass
            clock.value = 161
            control.enable(60)
            control.drain()
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("maintenance"):
                    pass

    def test_drain_grandfathers_only_owned_epoch_and_disable_refuses_it(self) -> None:
        temporary, clock, _, control = self.fixture()
        with temporary:
            control.enable(60)
            token = self.token(control)
            clock.value = 101
            control.drain()
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("claim"):
                    pass
            with control.transition_permit("claim", token):
                pass
            with control.transition_permit("worktree", token):
                pass
            control.disable()
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("process", token):
                    pass

    def test_later_enable_rotates_epoch_and_never_revives_old_token(self) -> None:
        temporary, clock, _, control = self.fixture()
        with temporary:
            control.enable(60)
            old = self.token(control)
            control.disable()
            clock.value = 101
            control.enable(60)
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("process", old):
                    pass

    def test_token_tamper_and_cross_control_use_are_refused(self) -> None:
        temporary, _, root, control = self.fixture()
        with temporary:
            control.enable(60)
            token = self.token(control)
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("process", replace(token, attempt_id="other")):
                    pass
            other = LocalControl(root.parent / "other", clock=lambda: 100)
            other.enable(60)
            with self.assertRaises(ControlRefusal):
                with other.transition_permit("process", token):
                    pass

    def test_owned_attempt_survives_enable_expiry_while_new_claims_stop(self) -> None:
        temporary, clock, _, control = self.fixture()
        with temporary:
            control.enable(60)
            token = self.token(control)
            control.drain(now=101)
            clock.value = 160
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("claim"):
                    pass
            with control.transition_permit("process", token):
                pass

    def test_enabled_expiry_stops_new_claims_but_not_owned_attempt(self) -> None:
        temporary, clock, _, control = self.fixture()
        with temporary:
            control.enable(60)
            token = self.token(control)
            clock.value = 160
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("claim"):
                    pass
            with control.transition_permit("worktree", token):
                pass

    def test_malformed_truncated_and_mac_tampered_state_fail_closed(self) -> None:
        variants = (b"{", b"", None)
        for variant in variants:
            with self.subTest(variant=variant):
                temporary, _, root, control = self.fixture()
                with temporary:
                    control.enable(60)
                    path = root / "control-state.json"
                    if variant is None:
                        value = json.loads(path.read_text())
                        value["mode"] = "disabled"
                        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
                        path.chmod(0o600)
                    else:
                        path.write_bytes(variant)
                    status = control.status()
                    self.assertFalse(status.permitted)
                    self.assertEqual(status.mode, "invalid")
                    with self.assertRaises(ControlIntegrityError):
                        control.disable()

    def test_state_key_and_lock_replacement_are_detected(self) -> None:
        for name in ("control-state.json", "control.key", "control.lock"):
            with self.subTest(name=name):
                temporary, _, root, control = self.fixture()
                with temporary:
                    control.enable(60)
                    path = root / name
                    copied = root / "copy"
                    shutil.copyfile(path, copied)
                    copied.chmod(0o600)
                    os.replace(copied, path)
                    status = control.status()
                    self.assertFalse(status.permitted)
                    self.assertEqual(status.mode, "invalid")

    def test_inflight_state_rewrite_is_detected_on_guard_exit(self) -> None:
        temporary, _, root, control = self.fixture()
        with temporary:
            control.enable(60)
            path = root / "control-state.json"
            with self.assertRaises(ControlIntegrityError):
                with control.transition_permit("claim"):
                    path.write_bytes(path.read_bytes() + b" ")

    def test_historical_signed_record_replay_is_detected(self) -> None:
        temporary, clock, root, control = self.fixture()
        with temporary:
            control.enable(60)
            historical = (root / "control-state.json").read_bytes()
            clock.value = 101
            control.refresh(60)
            replay = root / "replay"
            replay.write_bytes(historical)
            replay.chmod(0o600)
            os.replace(replay, root / "control-state.json")
            self.assertEqual(control.status().mode, "invalid")

    def test_symlink_hardlink_and_unsafe_modes_fail_closed(self) -> None:
        cases = ("symlink", "hardlink", "mode")
        for case in cases:
            with self.subTest(case=case):
                temporary, _, root, control = self.fixture()
                with temporary:
                    control.enable(60)
                    state = root / "control-state.json"
                    saved = root / "saved"
                    state.rename(saved)
                    if case == "symlink":
                        state.symlink_to(saved.name)
                    elif case == "hardlink":
                        os.link(saved, state)
                    else:
                        saved.rename(state)
                        state.chmod(0o644)
                    status = control.status()
                    self.assertFalse(status.permitted)
                    self.assertEqual(status.mode, "invalid")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_fifo_state_and_key_are_rejected_without_blocking(self) -> None:
        for name in ("control-state.json", "control.key"):
            with self.subTest(name=name):
                temporary, _, root, control = self.fixture()
                with temporary:
                    control.enable(60)
                    path = root / name
                    path.unlink()
                    os.mkfifo(path, mode=0o600)
                    started = time.monotonic()
                    status = control.status()
                    elapsed = time.monotonic() - started
                    self.assertEqual(status.mode, "invalid")
                    self.assertFalse(status.permitted)
                    self.assertLess(elapsed, 1.0)

    def test_unsafe_directory_mode_and_symlink_root_fail_closed(self) -> None:
        temporary, _, root, control = self.fixture()
        with temporary:
            control.enable(60)
            root.chmod(0o755)
            self.assertEqual(control.status().mode, "invalid")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            actual = base / "actual"
            actual.mkdir(mode=0o700)
            linked = base / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            self.assertEqual(LocalControl(linked).status().mode, "invalid")

    def test_partial_initialization_never_repairs_itself(self) -> None:
        temporary, _, root, control = self.fixture()
        with temporary:
            root.mkdir(mode=0o700)
            (root / "control.lock").write_text("")
            (root / "control.lock").chmod(0o600)
            self.assertEqual(control.status().mode, "invalid")
            with self.assertRaises(ControlIntegrityError):
                control.enable(60)
            self.assertFalse((root / "control.key").exists())
            self.assertFalse((root / "control-state.json").exists())

    def test_clock_rollback_fails_closed(self) -> None:
        temporary, clock, _, control = self.fixture()
        with temporary:
            control.enable(60)
            clock.value = 90
            status = control.status()
            self.assertFalse(status.permitted)
            self.assertEqual(status.reason, "clock_rollback")
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("claim"):
                    pass
            with self.assertRaises(ControlRefusal):
                control.refresh(60)
            self.assertEqual(control.disable().mode, "disabled")

    def test_ttl_and_control_root_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            LocalControl(Path("relative-control"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "control"
            control = LocalControl(root, max_enable_seconds=120, clock=lambda: 100)
            for ttl in (29, 121, True, 60.5):
                with self.subTest(ttl=ttl):
                    with self.assertRaises(ValueError):
                        control.enable(ttl)  # type: ignore[arg-type]
            self.assertFalse(root.exists())

    def test_disable_linearizes_after_held_process_spawn_boundary(self) -> None:
        temporary, _, _, control = self.fixture()
        with temporary:
            control.enable(60)
            token = self.token(control)
            entered = threading.Event()
            release = threading.Event()
            disabled = threading.Event()

            def spawn_boundary() -> None:
                with control.transition_permit("process", token):
                    entered.set()
                    self.assertTrue(release.wait(2))

            def disable() -> None:
                control.disable()
                disabled.set()

            worker = threading.Thread(target=spawn_boundary)
            worker.start()
            self.assertTrue(entered.wait(2))
            switch = threading.Thread(target=disable)
            switch.start()
            time.sleep(0.05)
            self.assertFalse(disabled.is_set())
            release.set()
            worker.join(2)
            switch.join(2)
            self.assertTrue(disabled.is_set())
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("process", token):
                    pass

    def test_disable_waits_for_an_admitted_maintenance_boundary(self) -> None:
        temporary, _, _, control = self.fixture()
        with temporary:
            control.enable(60)
            entered = threading.Event()
            release = threading.Event()
            disabled = threading.Event()

            def maintenance() -> None:
                with control.transition_permit("maintenance"):
                    entered.set()
                    self.assertTrue(release.wait(2))

            def disable() -> None:
                control.disable()
                disabled.set()

            worker = threading.Thread(target=maintenance)
            worker.start()
            self.assertTrue(entered.wait(2))
            switch = threading.Thread(target=disable)
            switch.start()
            time.sleep(0.05)
            self.assertFalse(disabled.is_set())
            release.set()
            worker.join(2)
            switch.join(2)
            self.assertTrue(disabled.is_set())
            with self.assertRaises(ControlRefusal):
                with control.transition_permit("maintenance"):
                    pass

    def test_generation_compare_and_swap_serializes_concurrent_updates(self) -> None:
        temporary, _, _, control = self.fixture()
        with temporary:
            generation = control.enable(60).generation
            barrier = threading.Barrier(3)
            outcomes: list[str] = []

            def mutate(operation: str) -> None:
                barrier.wait()
                try:
                    if operation == "disable":
                        control.disable(expected_generation=generation)
                    else:
                        control.drain(expected_generation=generation)
                    outcomes.append("changed")
                except ControlRefusal:
                    outcomes.append("stale")

            threads = [
                threading.Thread(target=mutate, args=("disable",)),
                threading.Thread(target=mutate, args=("drain",)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(2)
            self.assertCountEqual(outcomes, ["changed", "stale"])
            status = control.status()
            self.assertTrue(status.integrity_valid)
            self.assertFalse(status.permitted)


if __name__ == "__main__":
    unittest.main()
