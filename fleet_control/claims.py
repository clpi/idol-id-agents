from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Iterable, Sequence

from .scheduler import semantic_overlap


class ClaimConflict(RuntimeError):
    pass


class ClaimCommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Claim:
    owner: str
    task_id: str
    target: str
    acquired_at: float
    expires_at: float


class ControllerLease(AbstractContextManager["ControllerLease"]):
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handle = None

    def __enter__(self) -> "ControllerLease":
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise ClaimConflict("another fleet controller holds the scheduler lease") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps({"pid": os.getpid(), "acquired_at": time.time()}))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class SemanticClaimStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "semantic-claims.json"
        self.lock_path = self.root / "semantic-claims.lock"
        if not self.path.exists():
            self._write([])

    def _write(self, rows: list[dict]) -> None:
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _read(self) -> list[dict]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ClaimCommandError("semantic claim store is unreadable") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ClaimCommandError("semantic claim store has invalid shape")
        return value

    def _locked(self):
        handle = self.lock_path.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _live(rows: list[dict], now: float) -> list[dict]:
        return [row for row in rows if float(row.get("expires_at", 0)) > now]

    def list(self, *, now: float | None = None) -> tuple[Claim, ...]:
        current = time.time() if now is None else now
        lock = self._locked()
        try:
            rows = self._live(self._read(), current)
            self._write(rows)
            return tuple(Claim(**row) for row in rows)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def acquire(
        self,
        *,
        owner: str,
        task_id: str,
        targets: Sequence[str],
        ttl_seconds: int,
        now: float | None = None,
    ) -> tuple[Claim, ...]:
        if ttl_seconds < 30 or ttl_seconds > 86400:
            raise ValueError("semantic claim TTL outside supported bounds")
        if not targets:
            raise ValueError("semantic claims are required")
        if len(set(targets)) != len(targets):
            raise ValueError("duplicate semantic target")
        current = time.time() if now is None else now
        lock = self._locked()
        try:
            rows = self._live(self._read(), current)
            for target in targets:
                for row in rows:
                    if row.get("owner") == owner and row.get("task_id") == task_id:
                        continue
                    if semantic_overlap(target, str(row.get("target", ""))):
                        raise ClaimConflict(
                            f"semantic target {target!r} overlaps live claim {row.get('target')!r}"
                        )
            created = [
                Claim(
                    owner=owner,
                    task_id=task_id,
                    target=target,
                    acquired_at=current,
                    expires_at=current + ttl_seconds,
                )
                for target in targets
            ]
            rows.extend(asdict(claim) for claim in created)
            self._write(rows)
            return tuple(created)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def renew(
        self,
        *,
        owner: str,
        task_id: str,
        ttl_seconds: int,
        now: float | None = None,
    ) -> tuple[Claim, ...]:
        current = time.time() if now is None else now
        lock = self._locked()
        try:
            rows = self._live(self._read(), current)
            updated: list[dict] = []
            found = False
            for row in rows:
                if row.get("owner") == owner and row.get("task_id") == task_id:
                    row = {**row, "expires_at": current + ttl_seconds}
                    found = True
                updated.append(row)
            if not found:
                raise ClaimConflict("semantic claim lease is no longer live")
            self._write(updated)
            return tuple(Claim(**row) for row in updated if row.get("owner") == owner and row.get("task_id") == task_id)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def release(self, *, owner: str, task_id: str) -> None:
        lock = self._locked()
        try:
            rows = [
                row
                for row in self._read()
                if not (row.get("owner") == owner and row.get("task_id") == task_id)
            ]
            self._write(rows)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


class RepositoryClaimTransaction(AbstractContextManager["RepositoryClaimTransaction"]):
    """Transactional adapter over the repository-owned claim command."""

    def __init__(
        self,
        *,
        repository: Path,
        owner: str,
        task_id: str,
        paths: Sequence[str],
        ttl_seconds: int,
        required: bool = True,
    ) -> None:
        self.repository = Path(repository)
        self.owner = owner
        self.task_id = task_id
        self.paths = tuple(paths)
        self.ttl_seconds = ttl_seconds
        self.required = required
        self.command = self.repository / "tools/node/dev/claim"
        self.acquired: list[str] = []

    def _run(self, arguments: Iterable[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        if not self.command.is_file():
            if not self.required:
                return subprocess.CompletedProcess([], 0, "")
            raise ClaimCommandError(f"repository claim command is absent: {self.command}")
        result = subprocess.run(
            [str(self.command), *arguments],
            cwd=self.repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 and not allow_failure:
            raise ClaimConflict(result.stdout.strip() or "repository claim command refused")
        return result

    def __enter__(self) -> "RepositoryClaimTransaction":
        if not self.command.is_file() and not self.required:
            return self
        try:
            for path in self.paths:
                self._run(("acquire", self.owner, path, self.task_id))
                self.acquired.append(path)
        except Exception:
            self.release()
            raise
        return self

    def renew(self) -> None:
        for path in self.acquired:
            # Current IDOL claims renew by a reentrant acquire from the same
            # owner; the repository authority updates its timestamp.
            self._run(("acquire", self.owner, path, self.task_id))

    def release(self) -> None:
        for path in reversed(self.acquired):
            self._run(("release", self.owner, path), allow_failure=True)
        self.acquired.clear()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
