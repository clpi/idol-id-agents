from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
import base64
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from typing import Callable, Iterator, Mapping


STATE_SCHEMA = "idol.fleet.control.v1"
KEY_SCHEMA = "idol.fleet.control-key.v1"
ANCHOR_SCHEMA = "idol.fleet.control-anchor.v1"
TOKEN_SCHEMA = "idol.fleet.attempt-permit.v1"

STATE_FILE = "control-state.json"
KEY_FILE = "control.key"
LOCK_FILE = "control.lock"

MIN_ENABLE_SECONDS = 30
DEFAULT_MAX_ENABLE_SECONDS = 86_400
MAX_CLOCK_SKEW_SECONDS = 5
MAX_RECORD_BYTES = 16_384
BOUNDARIES = frozenset(("claim", "maintenance", "worktree", "process"))


class ControlError(RuntimeError):
    pass


class ControlIntegrityError(ControlError):
    pass


class ControlRefusal(ControlError):
    pass


class _ControlMissing(ControlRefusal):
    pass


@dataclass(frozen=True, slots=True)
class ControlStatus:
    mode: str
    permitted: bool
    reason: str
    generation: int | None
    updated_at: float | None
    enable_expires_at: float | None
    integrity_valid: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "permitted": self.permitted,
            "reason": self.reason,
            "generation": self.generation,
            "updated_at": self.updated_at,
            "enable_expires_at": self.enable_expires_at,
            "integrity_valid": self.integrity_valid,
        }


@dataclass(frozen=True, slots=True)
class AttemptPermit:
    schema: str
    attempt_id: str
    enable_epoch: str
    issued_generation: int
    issued_at: float
    admission_expires_at: float
    mac: str


@dataclass(frozen=True, slots=True)
class _State:
    mode: str
    generation: int
    updated_at: float
    enable_expires_at: float | None
    enable_epoch: str | None
    drain_epoch: str | None


@dataclass(slots=True)
class _Session:
    directory_fd: int
    lock_fd: int
    directory_identity: tuple[int, int]
    lock_identity: tuple[int, int]
    new_lock: bool
    observed_files: dict[str, tuple[int, ...]] = field(default_factory=dict)
    lock_signature: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class _Key:
    secret: bytes
    device: int
    inode: int
    digest: str


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _file_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read_fd(fd: int, *, limit: int = MAX_RECORD_BYTES) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > limit:
        raise ControlIntegrityError("control record exceeds its size bound")
    return value


def _write_all(fd: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        offset += os.write(fd, value[offset:])


class _ClaimGuard:
    def __init__(
        self,
        *,
        state: _State,
        secret: bytes,
        clock: Callable[[], float],
    ) -> None:
        self._state = state
        self._secret = secret
        self._clock = clock
        self._active = True
        self._issued_ids: set[str] = set()

    def close(self) -> None:
        self._active = False

    def issue_attempt_permit(self, attempt_id: str) -> AttemptPermit:
        if not self._active:
            raise ControlRefusal("claim guard is no longer active")
        if not attempt_id or len(attempt_id) > 200 or any(ord(char) < 0x20 for char in attempt_id):
            raise ValueError("attempt id is invalid")
        if attempt_id in self._issued_ids:
            raise ControlRefusal("attempt permit was already issued")
        if self._state.enable_epoch is None or self._state.enable_expires_at is None:
            raise ControlRefusal("enabled control epoch is unavailable")
        now = float(self._clock())
        if (
            not math.isfinite(now)
            or now < 0
            or now < self._state.updated_at - MAX_CLOCK_SKEW_SECONDS
            or now >= self._state.enable_expires_at
        ):
            raise ControlRefusal("control enable lease is unavailable before permit issue")
        payload: dict[str, object] = {
            "schema": TOKEN_SCHEMA,
            "attempt_id": attempt_id,
            "enable_epoch": self._state.enable_epoch,
            "issued_generation": self._state.generation,
            "issued_at": now,
            "admission_expires_at": self._state.enable_expires_at,
        }
        signature = hmac.new(self._secret, _canonical(payload), hashlib.sha256).hexdigest()
        self._issued_ids.add(attempt_id)
        return AttemptPermit(
            schema=TOKEN_SCHEMA,
            attempt_id=attempt_id,
            enable_epoch=self._state.enable_epoch,
            issued_generation=self._state.generation,
            issued_at=now,
            admission_expires_at=self._state.enable_expires_at,
            mac=signature,
        )


class _Transition(AbstractContextManager[ControlStatus | _ClaimGuard]):
    def __init__(
        self,
        control: "LocalControl",
        boundary: str,
        token: AttemptPermit | None,
    ) -> None:
        self._control = control
        self._boundary = boundary
        self._token = token
        self._locked: AbstractContextManager[_Session] | None = None
        self._session: _Session | None = None
        self._guard: _ClaimGuard | None = None

    def __enter__(self) -> ControlStatus | _ClaimGuard:
        if self._boundary not in BOUNDARIES:
            raise ValueError("unknown protected control boundary")
        if self._boundary in ("worktree", "process") and self._token is None:
            raise ValueError("post-claim boundary requires an attempt permit")
        if self._boundary == "maintenance" and self._token is not None:
            raise ValueError("maintenance boundary does not accept an attempt permit")
        self._locked = self._control._locked(exclusive=False, create=False)
        try:
            self._session = self._locked.__enter__()
            state, key = self._control._load_verified(self._session)
            self._control._authorize(state, key.secret, self._token)
            status = self._control._status_for(state)
            if self._boundary == "claim" and self._token is None:
                self._guard = _ClaimGuard(state=state, secret=key.secret, clock=self._control._clock)
                return self._guard
            return status
        except BaseException:
            if self._locked is not None:
                self._locked.__exit__(*sys.exc_info())
            self._locked = None
            self._session = None
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._guard is not None:
            self._guard.close()
        if self._locked is not None:
            self._locked.__exit__(exc_type, exc, traceback)
        self._locked = None
        self._session = None


class LocalControl:
    """Local operational dispatch fence.

    This gate can only refuse work. Passing it does not authorize a provider,
    payment, reset, source change, review, merge, or any other fleet action.
    The files are trusted to the service UID; this is not a boundary against
    that UID or root deliberately replacing every control artifact.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_enable_seconds: int = DEFAULT_MAX_ENABLE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).expanduser()
        if not self.root.is_absolute():
            raise ValueError("control directory must be absolute")
        if (
            not isinstance(max_enable_seconds, int)
            or isinstance(max_enable_seconds, bool)
            or max_enable_seconds < MIN_ENABLE_SECONDS
        ):
            raise ValueError("maximum enable lease is below the supported minimum")
        self.max_enable_seconds = max_enable_seconds
        self._clock = clock

    def status(self, *, now: float | None = None) -> ControlStatus:
        current = self._now(now) if now is not None else None
        try:
            with self._locked(exclusive=False, create=False) as session:
                state, _ = self._load_verified(session)
                return self._status_for(state, now=current)
        except _ControlMissing:
            return ControlStatus("disabled", False, "missing", None, None, None, False)
        except (ControlIntegrityError, OSError) as exc:
            return ControlStatus("invalid", False, str(exc), None, None, None, False)

    def enable(
        self,
        ttl_seconds: int,
        *,
        expected_generation: int | None = None,
        now: float | None = None,
    ) -> ControlStatus:
        return self._mutate("enable", ttl_seconds, expected_generation, now)

    def refresh(
        self,
        ttl_seconds: int,
        *,
        expected_generation: int | None = None,
        now: float | None = None,
    ) -> ControlStatus:
        return self._mutate("refresh", ttl_seconds, expected_generation, now)

    def disable(
        self,
        *,
        expected_generation: int | None = None,
        now: float | None = None,
    ) -> ControlStatus:
        return self._mutate("disable", None, expected_generation, now)

    def drain(
        self,
        *,
        expected_generation: int | None = None,
        now: float | None = None,
    ) -> ControlStatus:
        return self._mutate("drain", None, expected_generation, now)

    def transition_permit(
        self,
        boundary: str,
        token: AttemptPermit | None = None,
    ) -> _Transition:
        return _Transition(self, boundary, token)

    def _now(self, value: float | None = None) -> float:
        current = float(self._clock()) if value is None else float(value)
        if not math.isfinite(current) or current < 0:
            raise ValueError("control time is invalid")
        return current

    def _validate_ttl(self, ttl_seconds: int | None) -> int:
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or ttl_seconds < MIN_ENABLE_SECONDS
            or ttl_seconds > self.max_enable_seconds
        ):
            raise ValueError("control enable TTL outside supported bounds")
        return ttl_seconds

    def _mutate(
        self,
        operation: str,
        ttl_seconds: int | None,
        expected_generation: int | None,
        now: float | None,
    ) -> ControlStatus:
        current = self._now(now)
        ttl = self._validate_ttl(ttl_seconds) if operation in ("enable", "refresh") else None
        if expected_generation is not None and (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 1
        ):
            raise ValueError("expected control generation is invalid")
        with self._locked(exclusive=True, create=True) as session:
            if session.new_lock:
                key = self._create_key(session)
                previous = None
            else:
                previous, key = self._load_verified(session)
            generation = 1 if previous is None else previous.generation + 1
            if expected_generation is not None:
                actual = 0 if previous is None else previous.generation
                if actual != expected_generation:
                    raise ControlRefusal("control generation changed")
            if operation == "enable":
                if previous is not None and previous.updated_at > current + MAX_CLOCK_SKEW_SECONDS:
                    raise ControlRefusal("clock rollback prevents control enable")
                if previous is not None and previous.mode == "enabled":
                    raise ControlRefusal("control is already enabled; use refresh")
                if previous is not None and previous.mode == "draining":
                    raise ControlRefusal("disable the draining control before enabling a new epoch")
                mode = "enabled"
                enable_epoch = secrets.token_hex(32)
                drain_epoch = None
                expires_at = current + int(ttl)
            elif operation == "refresh":
                if previous is None or previous.mode != "enabled" or previous.enable_epoch is None:
                    raise ControlRefusal("only an enabled control can be refreshed")
                if previous.updated_at > current + MAX_CLOCK_SKEW_SECONDS:
                    raise ControlRefusal("clock rollback prevents control refresh")
                mode = "enabled"
                enable_epoch = previous.enable_epoch
                drain_epoch = None
                expires_at = current + int(ttl)
            elif operation == "drain":
                mode = "draining"
                enable_epoch = None
                drain_epoch = previous.enable_epoch if previous is not None and previous.mode == "enabled" else (
                    previous.drain_epoch if previous is not None and previous.mode == "draining" else None
                )
                expires_at = None
            elif operation == "disable":
                mode = "disabled"
                enable_epoch = None
                drain_epoch = None
                expires_at = None
            else:
                raise AssertionError("unsupported control mutation")
            state = _State(mode, generation, current, expires_at, enable_epoch, drain_epoch)
            self._write_state(session, state, key)
            verified, _ = self._load_verified(session)
            return self._status_for(verified, now=current)

    @contextmanager
    def _locked(self, *, exclusive: bool, create: bool) -> Iterator[_Session]:
        directory_fd = -1
        lock_fd = -1
        try:
            new_directory = False
            try:
                directory_fd = os.open(
                    self.root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            except FileNotFoundError:
                if not create:
                    raise _ControlMissing("control directory is missing")
                try:
                    os.mkdir(self.root, 0o700)
                    new_directory = True
                except FileExistsError:
                    pass
                directory_fd = os.open(
                    self.root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            directory_stat = os.fstat(directory_fd)
            self._validate_directory(directory_stat)
            directory_identity = _identity(directory_stat)
            self._assert_directory_path(directory_identity)

            new_lock = False
            flags = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                lock_fd = os.open(LOCK_FILE, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise _ControlMissing("control lock is missing")
                try:
                    lock_fd = os.open(
                        LOCK_FILE,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    new_lock = True
                    os.fsync(lock_fd)
                    os.fsync(directory_fd)
                except FileExistsError:
                    lock_fd = os.open(LOCK_FILE, flags, dir_fd=directory_fd)
            lock_stat = os.fstat(lock_fd)
            self._validate_file(lock_stat, LOCK_FILE)
            lock_identity = _identity(lock_stat)
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            self._assert_directory_path(directory_identity)
            self._assert_path_identity(directory_fd, LOCK_FILE, lock_identity)
            if new_directory and not new_lock:
                raise ControlIntegrityError("new control directory contains an unexpected lock")
            session = _Session(directory_fd, lock_fd, directory_identity, lock_identity, new_lock)
            try:
                yield session
            finally:
                self._assert_directory_path(directory_identity)
                self._assert_path_identity(directory_fd, LOCK_FILE, lock_identity)
                self._assert_observed_unchanged(session)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ControlIntegrityError("control path contains a symlink or non-directory") from exc
            raise
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            if directory_fd >= 0:
                os.close(directory_fd)

    def _validate_directory(self, value: os.stat_result) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise ControlIntegrityError("control directory is not a directory")
        if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o700:
            raise ControlIntegrityError("control directory owner or mode is unsafe")

    def _validate_file(self, value: os.stat_result, name: str) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise ControlIntegrityError(f"{name} is not a regular file")
        if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1:
            raise ControlIntegrityError(f"{name} owner, mode, or link count is unsafe")

    def _assert_directory_path(self, expected: tuple[int, int]) -> None:
        value = os.stat(self.root, follow_symlinks=False)
        self._validate_directory(value)
        if _identity(value) != expected:
            raise ControlIntegrityError("control directory was replaced")

    def _assert_path_identity(self, directory_fd: int, name: str, expected: tuple[int, int]) -> None:
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        self._validate_file(value, name)
        if _identity(value) != expected:
            raise ControlIntegrityError(f"{name} was replaced")

    def _read_named(self, session: _Session, name: str) -> tuple[bytes, os.stat_result]:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=session.directory_fd,
            )
        except FileNotFoundError as exc:
            raise ControlIntegrityError(f"{name} is missing") from exc
        try:
            before = os.fstat(fd)
            self._validate_file(before, name)
            value = _read_fd(fd)
            after = os.fstat(fd)
            if _file_signature(before) != _file_signature(after):
                raise ControlIntegrityError(f"{name} changed while read")
            self._assert_path_identity(session.directory_fd, name, _identity(before))
            session.observed_files[name] = _file_signature(after)
            return value, before
        finally:
            os.close(fd)

    @staticmethod
    def _json_object(raw: bytes, label: str) -> dict[str, object]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlIntegrityError(f"{label} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ControlIntegrityError(f"{label} is not an object")
        return value

    def _create_key(self, session: _Session) -> _Key:
        for name in (KEY_FILE, STATE_FILE):
            try:
                os.stat(name, dir_fd=session.directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ControlIntegrityError("new control lock has unexpected sibling records")
        secret = secrets.token_bytes(32)
        payload: dict[str, object] = {
            "schema": KEY_SCHEMA,
            "lock_device": session.lock_identity[0],
            "lock_inode": session.lock_identity[1],
            "secret": base64.b64encode(secret).decode("ascii"),
        }
        raw = _canonical(payload)
        fd = os.open(
            KEY_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=session.directory_fd,
        )
        try:
            _write_all(fd, raw)
            os.fsync(fd)
            metadata = os.fstat(fd)
            self._validate_file(metadata, KEY_FILE)
        finally:
            os.close(fd)
        os.fsync(session.directory_fd)
        self._assert_path_identity(session.directory_fd, KEY_FILE, _identity(metadata))
        session.observed_files[KEY_FILE] = _file_signature(metadata)
        return _Key(secret, metadata.st_dev, metadata.st_ino, hashlib.sha256(raw).hexdigest())

    def _read_key(self, session: _Session, anchor: Mapping[str, object]) -> _Key:
        raw, metadata = self._read_named(session, KEY_FILE)
        value = self._json_object(raw, "control key")
        expected = {"schema", "lock_device", "lock_inode", "secret"}
        if set(value) != expected or value.get("schema") != KEY_SCHEMA:
            raise ControlIntegrityError("control key has invalid shape")
        if value.get("lock_device") != session.lock_identity[0] or value.get("lock_inode") != session.lock_identity[1]:
            raise ControlIntegrityError("control key is not bound to the stable lock")
        try:
            secret = base64.b64decode(str(value["secret"]), validate=True)
        except (ValueError, TypeError) as exc:
            raise ControlIntegrityError("control key secret is invalid") from exc
        if len(secret) != 32:
            raise ControlIntegrityError("control key secret has invalid length")
        digest = hashlib.sha256(raw).hexdigest()
        if (
            anchor.get("key_device") != metadata.st_dev
            or anchor.get("key_inode") != metadata.st_ino
            or anchor.get("key_sha256") != digest
        ):
            raise ControlIntegrityError("control key does not match its lock anchor")
        return _Key(secret, metadata.st_dev, metadata.st_ino, digest)

    def _read_anchor(self, session: _Session) -> dict[str, object]:
        before = os.fstat(session.lock_fd)
        raw = _read_fd(session.lock_fd)
        after = os.fstat(session.lock_fd)
        if _file_signature(before) != _file_signature(after):
            raise ControlIntegrityError("control lock anchor changed while read")
        session.lock_signature = _file_signature(after)
        value = self._json_object(raw, "control lock anchor")
        expected = {
            "schema", "generation", "key_device", "key_inode", "key_sha256",
            "state_device", "state_inode", "state_sha256",
        }
        if set(value) != expected or value.get("schema") != ANCHOR_SCHEMA:
            raise ControlIntegrityError("control lock anchor has invalid shape")
        integers = ("generation", "key_device", "key_inode", "state_device", "state_inode")
        if any(
            not isinstance(value.get(name), int)
            or isinstance(value.get(name), bool)
            or int(value[name]) < 1
            for name in integers
        ):
            raise ControlIntegrityError("control lock anchor fields are invalid")
        hashes = ("key_sha256", "state_sha256")
        if any(not self._valid_digest(value.get(name)) for name in hashes):
            raise ControlIntegrityError("control lock anchor digest is invalid")
        return value

    def _load_verified(self, session: _Session) -> tuple[_State, _Key]:
        anchor = self._read_anchor(session)
        key = self._read_key(session, anchor)
        raw, metadata = self._read_named(session, STATE_FILE)
        digest = hashlib.sha256(raw).hexdigest()
        if (
            anchor.get("state_device") != metadata.st_dev
            or anchor.get("state_inode") != metadata.st_ino
            or anchor.get("state_sha256") != digest
        ):
            raise ControlIntegrityError("control state does not match its lock anchor")
        value = self._json_object(raw, "control state")
        expected = {
            "schema", "mode", "generation", "updated_at", "enable_expires_at",
            "enable_epoch", "drain_epoch", "state_device", "state_inode", "mac",
        }
        if set(value) != expected or value.get("schema") != STATE_SCHEMA:
            raise ControlIntegrityError("control state has invalid shape")
        payload = {key_name: item for key_name, item in value.items() if key_name != "mac"}
        claimed_mac = value.get("mac")
        actual_mac = hmac.new(key.secret, _canonical(payload), hashlib.sha256).hexdigest()
        if not isinstance(claimed_mac, str) or not hmac.compare_digest(claimed_mac, actual_mac):
            raise ControlIntegrityError("control state authentication failed")
        generation = value.get("generation")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or generation != anchor.get("generation")
        ):
            raise ControlIntegrityError("control state generation is invalid")
        if value.get("state_device") != metadata.st_dev or value.get("state_inode") != metadata.st_ino:
            raise ControlIntegrityError("control state is not bound to its inode")
        mode = value.get("mode")
        if mode not in ("enabled", "disabled", "draining"):
            raise ControlIntegrityError("control state mode is invalid")
        updated_at = value.get("updated_at")
        if not _is_number(updated_at) or float(updated_at) < 0:
            raise ControlIntegrityError("control state timestamp is invalid")
        enable_expires_at = value.get("enable_expires_at")
        enable_epoch = value.get("enable_epoch")
        drain_epoch = value.get("drain_epoch")
        if mode == "enabled":
            if (
                not _is_number(enable_expires_at)
                or float(enable_expires_at) <= float(updated_at)
                or float(enable_expires_at) - float(updated_at) > self.max_enable_seconds
                or not self._valid_epoch(enable_epoch)
                or drain_epoch is not None
            ):
                raise ControlIntegrityError("enabled control fields are invalid")
        elif mode == "disabled":
            if enable_expires_at is not None or enable_epoch is not None or drain_epoch is not None:
                raise ControlIntegrityError("disabled control fields are invalid")
        elif (
            enable_expires_at is not None
            or enable_epoch is not None
            or (drain_epoch is not None and not self._valid_epoch(drain_epoch))
        ):
            raise ControlIntegrityError("draining control fields are invalid")
        state = _State(
            str(mode),
            generation,
            float(updated_at),
            float(enable_expires_at) if enable_expires_at is not None else None,
            str(enable_epoch) if enable_epoch is not None else None,
            str(drain_epoch) if drain_epoch is not None else None,
        )
        return state, key

    @staticmethod
    def _valid_epoch(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _valid_digest(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True

    def _write_state(self, session: _Session, state: _State, key: _Key) -> None:
        temporary = f".control-state.{os.getpid()}.{secrets.token_hex(12)}.tmp"
        fd = -1
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=session.directory_fd,
            )
            metadata = os.fstat(fd)
            self._validate_file(metadata, temporary)
            payload: dict[str, object] = {
                "schema": STATE_SCHEMA,
                "mode": state.mode,
                "generation": state.generation,
                "updated_at": state.updated_at,
                "enable_expires_at": state.enable_expires_at,
                "enable_epoch": state.enable_epoch,
                "drain_epoch": state.drain_epoch,
                "state_device": metadata.st_dev,
                "state_inode": metadata.st_ino,
            }
            payload["mac"] = hmac.new(key.secret, _canonical(payload), hashlib.sha256).hexdigest()
            raw = _canonical(payload)
            _write_all(fd, raw)
            os.fsync(fd)
            after = os.fstat(fd)
            self._validate_file(after, temporary)
            if _identity(after) != _identity(metadata):
                raise ControlIntegrityError("temporary control state was replaced")
            os.close(fd)
            fd = -1
            os.rename(temporary, STATE_FILE, src_dir_fd=session.directory_fd, dst_dir_fd=session.directory_fd)
            os.fsync(session.directory_fd)
            self._assert_path_identity(session.directory_fd, STATE_FILE, _identity(metadata))
            anchor: dict[str, object] = {
                "schema": ANCHOR_SCHEMA,
                "generation": state.generation,
                "key_device": key.device,
                "key_inode": key.inode,
                "key_sha256": key.digest,
                "state_device": metadata.st_dev,
                "state_inode": metadata.st_ino,
                "state_sha256": hashlib.sha256(raw).hexdigest(),
            }
            os.lseek(session.lock_fd, 0, os.SEEK_SET)
            os.ftruncate(session.lock_fd, 0)
            _write_all(session.lock_fd, _canonical(anchor))
            os.fsync(session.lock_fd)
            session.lock_signature = _file_signature(os.fstat(session.lock_fd))
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=session.directory_fd)
            except FileNotFoundError:
                pass

    def _status_for(self, state: _State, *, now: float | None = None) -> ControlStatus:
        current = self._now(now)
        if state.updated_at > current + MAX_CLOCK_SKEW_SECONDS:
            return ControlStatus(state.mode, False, "clock_rollback", state.generation,
                                 state.updated_at, state.enable_expires_at, False)
        if state.mode == "enabled" and state.enable_expires_at is not None:
            if current >= state.enable_expires_at:
                return ControlStatus(state.mode, False, "lease_expired", state.generation,
                                     state.updated_at, state.enable_expires_at, True)
            return ControlStatus(state.mode, True, "enabled", state.generation,
                                 state.updated_at, state.enable_expires_at, True)
        return ControlStatus(state.mode, False, state.mode, state.generation,
                             state.updated_at, state.enable_expires_at, True)

    def _authorize(self, state: _State, secret: bytes, token: AttemptPermit | None) -> None:
        status = self._status_for(state)
        if state.mode == "disabled" or not status.integrity_valid:
            raise ControlRefusal(f"control refused dispatch: {status.reason}")
        if token is None:
            if state.mode != "enabled":
                raise ControlRefusal("control is draining and refuses new claims")
            if not status.permitted:
                raise ControlRefusal(f"control refused dispatch: {status.reason}")
            return
        self._verify_token(token, secret)
        allowed_epoch = state.enable_epoch if state.mode == "enabled" else state.drain_epoch
        if allowed_epoch is None or token.enable_epoch != allowed_epoch:
            raise ControlRefusal("attempt permit does not belong to the active control epoch")
        if token.issued_generation > state.generation:
            raise ControlRefusal("attempt permit generation is ahead of control state")

    def _verify_token(self, token: AttemptPermit, secret: bytes) -> None:
        if not isinstance(token, AttemptPermit) or token.schema != TOKEN_SCHEMA:
            raise ControlRefusal("attempt permit has invalid type")
        if (
            not token.attempt_id
            or not self._valid_epoch(token.enable_epoch)
            or not isinstance(token.issued_generation, int)
            or isinstance(token.issued_generation, bool)
            or token.issued_generation < 1
            or not _is_number(token.issued_at)
            or not _is_number(token.admission_expires_at)
            or token.admission_expires_at <= token.issued_at
        ):
            raise ControlRefusal("attempt permit has invalid fields")
        payload: dict[str, object] = {
            "schema": token.schema,
            "attempt_id": token.attempt_id,
            "enable_epoch": token.enable_epoch,
            "issued_generation": token.issued_generation,
            "issued_at": token.issued_at,
            "admission_expires_at": token.admission_expires_at,
        }
        actual = hmac.new(secret, _canonical(payload), hashlib.sha256).hexdigest()
        if not isinstance(token.mac, str) or not hmac.compare_digest(token.mac, actual):
            raise ControlRefusal("attempt permit authentication failed")

    def _assert_observed_unchanged(self, session: _Session) -> None:
        if session.lock_signature is not None:
            current_lock = os.fstat(session.lock_fd)
            if _file_signature(current_lock) != session.lock_signature:
                raise ControlIntegrityError("control lock anchor changed during protected boundary")
        for name, expected in session.observed_files.items():
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=session.directory_fd,
            )
            try:
                current = os.fstat(fd)
                self._validate_file(current, name)
                if _file_signature(current) != expected:
                    raise ControlIntegrityError(f"{name} changed during protected boundary")
                self._assert_path_identity(session.directory_fd, name, _identity(current))
            finally:
                os.close(fd)


__all__ = [
    "AttemptPermit",
    "ControlError",
    "ControlIntegrityError",
    "ControlRefusal",
    "ControlStatus",
    "LocalControl",
]
