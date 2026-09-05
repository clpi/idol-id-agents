from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "inventory_snapshot_lock", ROOT / "scripts" / "inventory_snapshot_lock.py"
)
assert SPEC and SPEC.loader
snapshot_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot_lock)


class InventorySnapshotLockTests(unittest.TestCase):
    def private_directory(self, parent: Path) -> Path:
        path = parent / "inventory"
        path.mkdir(mode=0o700)
        return path

    def test_creates_private_directory_and_lock_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "inventory"
            with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                lock_path = path / "snapshot.lock"
                self.assertTrue(lock_path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                self.assertEqual(lock_path.stat().st_uid, os.geteuid())
                self.assertEqual(lock_path.stat().st_nlink, 1)
                identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
            with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), identity)

    def test_existing_directory_with_broad_mode_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.private_directory(Path(temporary).resolve())
            path.chmod(0o750)
            with self.assertRaisesRegex(snapshot_lock.InventorySnapshotLockError, "mode 0700"):
                with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                    self.fail("unsafe directory admitted")

    def test_directory_symlink_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            target = self.private_directory(parent)
            alias = parent / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                with snapshot_lock.inventory_snapshot_lock(state_directory=alias):
                    self.fail("directory symlink admitted")

    def test_lock_symlink_and_hardlink_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            path = self.private_directory(parent)
            target = parent / "target"
            target.write_text("", encoding="utf-8")
            target.chmod(0o600)
            (path / "snapshot.lock").symlink_to(target)
            with self.assertRaises(OSError):
                with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                    self.fail("lock symlink admitted")

            (path / "snapshot.lock").unlink()
            os.link(target, path / "snapshot.lock")
            with self.assertRaisesRegex(snapshot_lock.InventorySnapshotLockError, "one link"):
                with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                    self.fail("multiply linked lock admitted")

    def test_replacement_while_held_refuses_before_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.private_directory(Path(temporary).resolve())
            with self.assertRaisesRegex(snapshot_lock.InventorySnapshotLockError, "binding changed"):
                with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                    original = path / "snapshot.lock"
                    original.rename(path / "detached.lock")
                    replacement = path / "snapshot.lock"
                    replacement.touch(mode=0o600)
                    replacement.chmod(0o600)

    def test_directory_replacement_while_held_refuses_before_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            path = self.private_directory(parent)
            with self.assertRaisesRegex(snapshot_lock.InventorySnapshotLockError, "directory binding changed"):
                with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                    path.rename(parent / "detached-inventory")
                    path.mkdir(mode=0o700)

    def test_wait_is_bounded_and_failure_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.private_directory(Path(temporary).resolve())
            with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                competing = os.open(path / "snapshot.lock", os.O_RDWR | os.O_CLOEXEC)
                try:
                    with self.assertRaisesRegex(snapshot_lock.InventorySnapshotLockError, "timed out"):
                        with snapshot_lock.inventory_snapshot_lock(state_directory=path, wait_seconds=0.02):
                            self.fail("competing observer admitted")
                finally:
                    os.close(competing)

            with self.assertRaisesRegex(RuntimeError, "observation failed"):
                with snapshot_lock.inventory_snapshot_lock(state_directory=path):
                    raise RuntimeError("observation failed")

            descriptor = os.open(path / "snapshot.lock", os.O_RDWR | os.O_CLOEXEC)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def test_process_death_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.private_directory(Path(temporary).resolve())
            read_descriptor, write_descriptor = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(read_descriptor)
                try:
                    context = snapshot_lock.inventory_snapshot_lock(state_directory=path)
                    context.__enter__()
                    os.write(write_descriptor, b"locked")
                except BaseException:
                    os._exit(2)
                os._exit(0)

            os.close(write_descriptor)
            try:
                self.assertEqual(os.read(read_descriptor, 6), b"locked")
            finally:
                os.close(read_descriptor)
            _, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            with snapshot_lock.inventory_snapshot_lock(state_directory=path, wait_seconds=0):
                pass

    def test_noncanonical_or_relative_config_refuses(self):
        for configured in ("relative/path", "/tmp/../tmp/inventory", ""):
            with self.subTest(configured=configured), mock.patch.dict(
                os.environ, {snapshot_lock.STATE_DIRECTORY_ENV: configured}
            ):
                with self.assertRaises(snapshot_lock.InventorySnapshotLockError):
                    snapshot_lock.inventory_state_directory()


if __name__ == "__main__":
    unittest.main()
