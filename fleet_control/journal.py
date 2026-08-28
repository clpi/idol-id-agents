from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable, Mapping


class JournalError(RuntimeError):
    pass


class Journal:
    """Append-only JSONL journal with an explicit hash chain and file lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _digest(record_without_hash: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            record_without_hash,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _decode(line: str, number: int) -> dict[str, Any]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalError(f"journal line {number} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise JournalError(f"journal line {number} is not an object")
        return value

    def _read_locked(self, handle) -> list[dict[str, Any]]:
        handle.seek(0)
        rows: list[dict[str, Any]] = []
        previous = "0" * 64
        expected_sequence = 1
        for number, raw in enumerate(handle, 1):
            line = raw.rstrip("\n")
            if not line:
                raise JournalError(f"journal line {number} is empty")
            row = self._decode(line, number)
            if row.get("sequence") != expected_sequence:
                raise JournalError(f"journal line {number} has a broken sequence")
            if row.get("previous") != previous:
                raise JournalError(f"journal line {number} has a broken predecessor")
            claimed = row.get("hash")
            without_hash = {key: value for key, value in row.items() if key != "hash"}
            actual = self._digest(without_hash)
            if claimed != actual:
                raise JournalError(f"journal line {number} has a broken hash")
            rows.append(row)
            previous = actual
            expected_sequence += 1
        return rows

    def verify(self) -> tuple[dict[str, Any], ...]:
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return tuple(self._read_locked(handle))
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append(self, kind: str, fact: Mapping[str, Any], *, at: float | None = None) -> dict[str, Any]:
        if not kind or not isinstance(kind, str):
            raise ValueError("journal kind is required")
        if not isinstance(fact, Mapping):
            raise ValueError("journal fact must be a mapping")
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                rows = self._read_locked(handle)
                previous = rows[-1]["hash"] if rows else "0" * 64
                record: dict[str, Any] = {
                    "sequence": len(rows) + 1,
                    "at": time.time() if at is None else float(at),
                    "kind": kind,
                    "previous": previous,
                    "fact": dict(fact),
                }
                record["hash"] = self._digest(record)
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return record
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def events(self, kinds: Iterable[str] | None = None) -> tuple[dict[str, Any], ...]:
        rows = self.verify()
        if kinds is None:
            return rows
        allowed = frozenset(kinds)
        return tuple(row for row in rows if row.get("kind") in allowed)
