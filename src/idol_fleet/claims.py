from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import time
from typing import Iterable


class ClaimConflict(RuntimeError):
    pass


def _overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + ".")
        or right.startswith(left + ".")
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


class SchedulerLease:
    def __init__(self, root: Path, *, owner: str, ttl: int = 300) -> None:
        if not owner:
            raise ValueError("lease owner is required")
        self.root = Path(root)
        self.owner = owner
        self.ttl = ttl
        self.path = self.root / "scheduler.lease"
        self._held = False

    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                expires = float(data.get("expires", 0))
                pid = int(data.get("pid", -1))
            except Exception:
                raise ClaimConflict("scheduler lease exists and is unreadable")
            live = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    live = True
                except OSError:
                    live = False
            if expires > now and live:
                raise ClaimConflict(f"scheduler lease held by {data.get('owner', 'unknown')}")
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return self.acquire()
        payload = {
            "owner": self.owner,
            "pid": os.getpid(),
            "acquired": now,
            "expires": now + self.ttl,
        }
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        self._held = True

    def renew(self) -> None:
        if not self._held:
            raise ClaimConflict("scheduler lease is not held")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("owner") != self.owner or int(data.get("pid", -1)) != os.getpid():
            raise ClaimConflict("scheduler lease ownership changed")
        data["expires"] = time.time() + self.ttl
        temporary = self.path.with_suffix(".next")
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def release(self) -> None:
        if not self._held:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("owner") != self.owner:
                raise ClaimConflict("cannot release another scheduler owner")
            self.path.unlink()
        finally:
            self._held = False

    def __enter__(self) -> "SchedulerLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class SemanticClaimStore:
    def __init__(self, root: Path, *, ttl: int = 14_400) -> None:
        self.root = Path(root)
        self.ttl = ttl
        self.path = self.root / "semantic-claims.json"
        self.lock_path = self.root / "semantic-claims.lock"

    def _with_lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        lock = os.fdopen(fd, "r+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return lock

    def _read(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ClaimConflict("semantic claim store is corrupt") from exc
        if not isinstance(value, list):
            raise ClaimConflict("semantic claim store is not a list")
        return [row for row in value if isinstance(row, dict)]

    def _write(self, rows: list[dict[str, object]]) -> None:
        temporary = self.path.with_suffix(".next")
        temporary.write_text(json.dumps(rows, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def acquire(self, *, owner: str, task: str, targets: Iterable[str]) -> None:
        requested = tuple(dict.fromkeys(str(value) for value in targets))
        if not owner or not task or not requested or any(not target for target in requested):
            raise ValueError("owner, task and semantic targets are required")
        now = time.time()
        lock = self._with_lock()
        try:
            rows = [row for row in self._read() if float(row.get("expires", 0)) > now]
            for row in rows:
                if row.get("owner") == owner:
                    continue
                active = str(row.get("target", ""))
                if any(_overlap(candidate, active) for candidate in requested):
                    raise ClaimConflict(f"semantic target overlaps {active!r} held by {row.get('owner')}")
            existing = {(str(row.get("owner")), str(row.get("target"))) for row in rows}
            for target in requested:
                key = (owner, target)
                if key in existing:
                    for row in rows:
                        if (str(row.get("owner")), str(row.get("target"))) == key:
                            row["expires"] = now + self.ttl
                            row["task"] = task
                    continue
                rows.append({
                    "owner": owner,
                    "task": task,
                    "target": target,
                    "acquired": now,
                    "expires": now + self.ttl,
                })
            self._write(rows)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def release(self, *, owner: str, targets: Iterable[str]) -> None:
        released = set(str(value) for value in targets)
        lock = self._with_lock()
        try:
            rows = self._read()
            for row in rows:
                if str(row.get("target")) in released and row.get("owner") != owner:
                    raise ClaimConflict(f"semantic claim {row.get('target')} held by another owner")
            self._write([
                row for row in rows
                if not (row.get("owner") == owner and str(row.get("target")) in released)
            ])
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def list(self) -> tuple[dict[str, object], ...]:
        lock = self._with_lock()
        try:
            now = time.time()
            rows = [row for row in self._read() if float(row.get("expires", 0)) > now]
            return tuple(rows)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

class RepositoryClaimClient:
    def __init__(self, repository: Path, *, executable: Path | None = None) -> None:
        self.repository = Path(repository)
        self.executable = Path(executable) if executable is not None else self.repository / "tools/node/dev/claim"

    def _call(self, argv: list[str]) -> dict[str, object]:
        from .process import run_command
        result = run_command([str(self.executable), *argv], cwd=self.repository, timeout=30)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ClaimConflict("repository claim tool did not return JSON") from exc
        if not isinstance(payload, dict):
            raise ClaimConflict("repository claim result is not an object")
        if result.returncode != 0:
            raise ClaimConflict(str(payload.get("reason") or payload))
        return payload

    def acquire(self, owner: str, paths: tuple[object, ...], work: str) -> None:
        acquired: list[str] = []
        try:
            for path in paths:
                text = str(path)
                payload = self._call(["acquire", owner, text, work])
                if payload.get("granted") is not True:
                    raise ClaimConflict(str(payload.get("reason") or "repository path claim refused"))
                acquired.append(text)
        except Exception:
            for text in reversed(acquired):
                try:
                    self._call(["release", owner, text])
                except Exception:
                    pass
            raise

    def release(self, owner: str, paths: tuple[object, ...]) -> None:
        failures: list[str] = []
        for path in reversed(paths):
            text = str(path)
            try:
                payload = self._call(["release", owner, text])
                if payload.get("released") is not True and payload.get("granted") is not True:
                    failures.append(text)
            except Exception:
                failures.append(text)
        if failures:
            raise ClaimConflict("failed to release repository claims: " + ", ".join(failures))
