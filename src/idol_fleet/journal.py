from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator


_FORBIDDEN = re.compile(
    r"(?:token|secret|password|passwd|credential|cookie|authorization|api.?key|private.?key|client.?secret|refresh.?token|bearer|prompt|transcript|reasoning|chain.?of.?thought|message|content)",
    re.IGNORECASE,
)


class JournalSecurityError(ValueError):
    pass


def _check_safe(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _FORBIDDEN.search(key_text):
                raise JournalSecurityError(f"forbidden journal field: {path + key_text}")
            _check_safe(child, f"{path}{key_text}.")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_safe(child, f"{path}{index}.")
    elif value is None or isinstance(value, (bool, int, float, str)):
        return
    else:
        raise TypeError(f"journal value is not JSON-safe: {type(value).__name__}")


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        events: list[dict[str, Any]] = []
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            complete = line.endswith(b"\n")
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not complete:
                    break
                raise
            if not isinstance(decoded, dict):
                raise ValueError("journal event must be an object")
            events.append(decoded)
        return events

    def append(self, event: dict[str, Any]) -> None:
        _check_safe(event)
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("journal event id is required")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r+b", closefd=False) as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                fh.seek(0)
                existing = fh.read()
                for line in existing.splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("id") == event_id:
                        raise ValueError(f"duplicate journal event id: {event_id}")
                fh.seek(0, os.SEEK_END)
                payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                fh.write(payload.encode("utf-8") + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(fd)
