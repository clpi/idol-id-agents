from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping


_DESCRIPTOR_FIELDS = frozenset({"path", "sha256", "size_bytes"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_CANDIDATE_EVIDENCE_BYTES = 8_000_000


class EvidenceArtifactError(RuntimeError):
    pass


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


def _evidence_directory(state_dir: Path) -> Path:
    root = Path(state_dir).expanduser()
    if not root.is_absolute():
        raise EvidenceArtifactError("evidence state directory must be absolute")
    return root / "candidate-evidence"


def _open_private_directory(directory: Path) -> tuple[int, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise EvidenceArtifactError("platform lacks required no-follow artifact support")
    descriptor: int | None = None
    try:
        entry = os.stat(directory, follow_symlinks=False)
        descriptor = os.open(
            directory,
            os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise EvidenceArtifactError("candidate evidence directory is unavailable") from exc
    if (
        not stat.S_ISDIR(entry.st_mode)
        or _directory_identity(entry) != _directory_identity(opened)
        or entry.st_uid != os.geteuid()
        or stat.S_IMODE(entry.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise EvidenceArtifactError("candidate evidence directory is not private and stable")
    return descriptor, entry


def validate_candidate_evidence(
    raw: Mapping[str, Any],
    *,
    state_dir: Path,
    attempt_id: str,
) -> dict[str, object]:
    if not isinstance(attempt_id, str) or _NAME_RE.fullmatch(attempt_id) is None:
        raise EvidenceArtifactError("candidate evidence attempt identity is invalid")
    if not isinstance(raw, Mapping) or set(raw) != _DESCRIPTOR_FIELDS:
        raise EvidenceArtifactError("candidate evidence descriptor shape is invalid")
    path_value = raw.get("path")
    digest_value = raw.get("sha256")
    size_value = raw.get("size_bytes")
    if not isinstance(path_value, str) or not path_value or path_value != path_value.strip():
        raise EvidenceArtifactError("candidate evidence path is invalid")
    path = Path(path_value)
    directory = _evidence_directory(state_dir)
    if (
        not path.is_absolute()
        or path.parent != directory
        or path.name != f"{attempt_id}.stdout"
    ):
        raise EvidenceArtifactError("candidate evidence path is outside the private directory")
    if not isinstance(digest_value, str) or re.fullmatch(r"[0-9a-f]{64}", digest_value) is None:
        raise EvidenceArtifactError("candidate evidence digest is invalid")
    if (
        type(size_value) is not int
        or size_value < 1
        or size_value > MAX_CANDIDATE_EVIDENCE_BYTES
    ):
        raise EvidenceArtifactError("candidate evidence size is invalid")

    directory_fd, directory_before = _open_private_directory(directory)
    file_fd: int | None = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        entry_before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(entry_before.st_mode)
            or entry_before.st_uid != os.geteuid()
            or stat.S_IMODE(entry_before.st_mode) != 0o600
            or entry_before.st_size != size_value
        ):
            raise EvidenceArtifactError("candidate evidence file is not private and regular")
        file_fd = os.open(
            path.name,
            os.O_RDONLY
            | os.O_NONBLOCK
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if _identity(entry_before) != _identity(before):
            raise EvidenceArtifactError("candidate evidence changed before reading")
        digest = hashlib.sha256()
        read_size = 0
        while True:
            remaining = size_value - read_size
            block = os.read(file_fd, min(1024 * 1024, remaining + 1))
            if not block:
                break
            digest.update(block)
            read_size += len(block)
            if read_size > size_value:
                raise EvidenceArtifactError("candidate evidence grew while reading")
        after = os.fstat(file_fd)
        entry_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        directory_after = os.fstat(directory_fd)
        directory_entry_after = os.stat(directory, follow_symlinks=False)
    except EvidenceArtifactError:
        raise
    except OSError as exc:
        raise EvidenceArtifactError("candidate evidence artifact is unreadable") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)

    if _identity(before) != _identity(after) or _identity(after) != _identity(entry_after):
        raise EvidenceArtifactError("candidate evidence changed while reading")
    if (
        _directory_identity(directory_before) != _directory_identity(directory_after)
        or _directory_identity(directory_after) != _directory_identity(directory_entry_after)
    ):
        raise EvidenceArtifactError("candidate evidence directory changed while reading")
    if read_size != size_value or after.st_size != size_value or digest.hexdigest() != digest_value:
        raise EvidenceArtifactError("candidate evidence descriptor does not match the artifact")
    return {
        "path": path_value,
        "sha256": digest_value,
        "size_bytes": size_value,
    }


def retain_candidate_evidence(
    *,
    state_dir: Path,
    attempt_id: str,
    content: bytes,
) -> dict[str, object]:
    if not isinstance(attempt_id, str) or _NAME_RE.fullmatch(attempt_id) is None:
        raise EvidenceArtifactError("candidate evidence attempt identity is invalid")
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > MAX_CANDIDATE_EVIDENCE_BYTES
    ):
        raise EvidenceArtifactError("candidate evidence output is empty")
    directory = _evidence_directory(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise EvidenceArtifactError("candidate evidence directory cannot be created") from exc
    directory_fd: int | None = None
    file_fd: int | None = None
    filename = f"{attempt_id}.stdout"
    created = False
    try:
        try:
            entry = os.stat(directory, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid():
                raise EvidenceArtifactError("candidate evidence directory is not owned and regular")
            nofollow = getattr(os, "O_NOFOLLOW", None)
            directory_flag = getattr(os, "O_DIRECTORY", None)
            if nofollow is None or directory_flag is None:
                raise EvidenceArtifactError("platform lacks required no-follow artifact support")
            directory_fd = os.open(
                directory,
                os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
            )
            opened_directory = os.fstat(directory_fd)
            if _directory_identity(entry) != _directory_identity(opened_directory):
                raise EvidenceArtifactError("candidate evidence directory changed before writing")
            os.fchmod(directory_fd, 0o700)
            file_fd = os.open(
                filename,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | os.O_NONBLOCK
                | nofollow
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            created = True
            os.fchmod(file_fd, 0o600)
            with os.fdopen(file_fd, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(file_fd)
            file_fd = None
            os.fsync(directory_fd)
        except EvidenceArtifactError:
            raise
        except OSError as exc:
            raise EvidenceArtifactError("candidate evidence artifact cannot be retained") from exc

        descriptor: dict[str, object] = {
            "path": str(directory / filename),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        return validate_candidate_evidence(
            descriptor,
            state_dir=state_dir,
            attempt_id=attempt_id,
        )
    except Exception:
        if created and directory_fd is not None:
            try:
                os.unlink(filename, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
