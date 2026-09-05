from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time
from typing import Iterator


STATE_DIRECTORY_ENV = "IDOL_FLEET_INVENTORY_STATE_DIR"
LOCK_NAME = "snapshot.lock"
LOCK_WAIT_SECONDS = 8.0
LOCK_POLL_SECONDS = 0.05


class InventorySnapshotLockError(RuntimeError):
    pass


def inventory_state_directory() -> Path:
    configured = os.environ.get(STATE_DIRECTORY_ENV)
    if configured is None:
        return Path.home() / ".local" / "state" / "idol-fleet-inventory"
    if not configured or configured != os.path.normpath(configured):
        raise InventorySnapshotLockError("inventory state directory path is not canonical")
    path = Path(configured)
    if not path.is_absolute() or path == Path("/"):
        raise InventorySnapshotLockError("inventory state directory must be an absolute child path")
    return path


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_directory(path: Path) -> int:
    descriptor = os.open("/", _directory_flags())
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise InventorySnapshotLockError("inventory state path is not a directory")
    if metadata.st_uid != os.geteuid():
        raise InventorySnapshotLockError("inventory state directory has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise InventorySnapshotLockError("inventory state directory must have mode 0700")


def _validate_lock_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise InventorySnapshotLockError("inventory snapshot lock is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise InventorySnapshotLockError("inventory snapshot lock has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise InventorySnapshotLockError("inventory snapshot lock must have mode 0600")
    if metadata.st_nlink != 1:
        raise InventorySnapshotLockError("inventory snapshot lock must have one link")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_state_directory(path: Path) -> int:
    parent = path.parent
    parent_descriptor = _open_directory(parent)
    created = False
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(path.name, _directory_flags(), dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        if created:
            os.fchmod(descriptor, 0o700)
        _validate_directory(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_lock_file(directory_descriptor: int) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(
            LOCK_NAME,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_descriptor,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(LOCK_NAME, flags, dir_fd=directory_descriptor)
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _validate_lock_file(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_binding(path: Path, directory_descriptor: int, lock_descriptor: int) -> None:
    directory_metadata = os.fstat(directory_descriptor)
    _validate_directory(directory_metadata)
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise InventorySnapshotLockError("inventory state directory binding changed") from exc
    if not _same_file(directory_metadata, path_metadata):
        raise InventorySnapshotLockError("inventory state directory binding changed")

    lock_metadata = os.fstat(lock_descriptor)
    _validate_lock_file(lock_metadata)
    try:
        path_lock_metadata = os.stat(LOCK_NAME, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise InventorySnapshotLockError("inventory snapshot lock binding changed") from exc
    _validate_lock_file(path_lock_metadata)
    if not _same_file(lock_metadata, path_lock_metadata):
        raise InventorySnapshotLockError("inventory snapshot lock binding changed")


def _acquire_bounded(
    path: Path,
    directory_descriptor: int,
    lock_descriptor: int,
    wait_seconds: float,
) -> None:
    if not 0 <= wait_seconds <= LOCK_WAIT_SECONDS:
        raise InventorySnapshotLockError("inventory snapshot lock wait is outside its bound")
    deadline = time.monotonic() + wait_seconds
    while True:
        _validate_binding(path, directory_descriptor, lock_descriptor)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InventorySnapshotLockError("timed out waiting for inventory snapshot lock") from None
            time.sleep(min(LOCK_POLL_SECONDS, remaining))
            continue
        _validate_binding(path, directory_descriptor, lock_descriptor)
        return


@contextmanager
def inventory_snapshot_lock(
    *,
    state_directory: Path | None = None,
    wait_seconds: float = LOCK_WAIT_SECONDS,
) -> Iterator[None]:
    path = state_directory if state_directory is not None else inventory_state_directory()
    if not path.is_absolute() or path == Path("/"):
        raise InventorySnapshotLockError("inventory state directory must be an absolute child path")
    directory_descriptor = _open_state_directory(path)
    try:
        lock_descriptor = _open_lock_file(directory_descriptor)
        try:
            _acquire_bounded(path, directory_descriptor, lock_descriptor, wait_seconds)
            try:
                yield
                _validate_binding(path, directory_descriptor, lock_descriptor)
            finally:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(directory_descriptor)
