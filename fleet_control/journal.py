from __future__ import annotations

import fcntl
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable, Iterator, Mapping


class JournalError(RuntimeError):
    pass


class JournalTransaction:
    """A verified snapshot and append operations sharing one exclusive lock."""

    def __init__(self, journal: "Journal", handle, rows: list[dict[str, Any]]) -> None:
        self._journal = journal
        self._handle = handle
        self._rows = rows
        self._closed = False

    def _open(self) -> None:
        if self._closed:
            raise JournalError("journal transaction is closed")

    def events(self, kinds: Iterable[str] | None = None) -> tuple[dict[str, Any], ...]:
        self._open()
        allowed = None if kinds is None else frozenset(kinds)
        return tuple(deepcopy(row) for row in self._rows if allowed is None or row.get("kind") in allowed)

    def append(self, kind: str, fact: Mapping[str, Any], *, at: float | None = None) -> dict[str, Any]:
        self._open()
        if not kind or not isinstance(kind, str):
            raise ValueError("journal kind is required")
        if not isinstance(fact, Mapping):
            raise ValueError("journal fact must be a mapping")
        record: dict[str, Any] = {
            "sequence": len(self._rows) + 1,
            "at": time.time() if at is None else float(at),
            "kind": kind,
            "previous": self._rows[-1]["hash"] if self._rows else "0" * 64,
            "fact": deepcopy(dict(fact)),
        }
        record["hash"] = self._journal._digest(record)
        self._handle.seek(0, os.SEEK_END)
        self._handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._rows.append(record)
        self._journal._rows = tuple(self._rows)
        self._journal._signature = self._journal._file_signature(self._handle)
        return deepcopy(record)


class Journal:
    """Append-only JSONL journal with an explicit hash chain and file lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self._rows: tuple[dict[str, Any], ...] = ()
        self._signature: tuple[int, int, int] | None = None

    @staticmethod
    def _file_signature(handle) -> tuple[int, int, int]:
        stat = os.fstat(handle.fileno())
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

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
                rows = tuple(self._read_locked(handle))
                self._rows = rows
                self._signature = self._file_signature(handle)
                return rows
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _snapshot(self) -> tuple[dict[str, Any], ...]:
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                signature = self._file_signature(handle)
                if self._signature == signature:
                    return self._rows
                rows = tuple(self._read_locked(handle))
                self._rows = rows
                self._signature = signature
                return rows
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def transaction(self) -> Iterator[JournalTransaction]:
        """Serialize evidence read, native decision, and durable reservation.

        Callers holding the local control guard acquire it before this lock.
        Use the yielded transaction for appends; opening another journal lock
        from the same critical section would deadlock.
        """
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            transaction = None
            try:
                signature = self._file_signature(handle)
                if self._signature == signature:
                    rows = list(self._rows)
                else:
                    rows = self._read_locked(handle)
                transaction = JournalTransaction(self, handle, rows)
                yield transaction
            finally:
                if transaction is not None:
                    transaction._closed = True
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append(self, kind: str, fact: Mapping[str, Any], *, at: float | None = None) -> dict[str, Any]:
        with self.transaction() as transaction:
            return transaction.append(kind, fact, at=at)

    def events(self, kinds: Iterable[str] | None = None) -> tuple[dict[str, Any], ...]:
        rows = self._snapshot()
        if kinds is None:
            return rows
        allowed = frozenset(kinds)
        return tuple(row for row in rows if row.get("kind") in allowed)
