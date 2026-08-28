from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(
    r"(?:^|_)(?:token|password|secret|credential|cookie|authorization|api_?key|private_?key)(?:$|_)",
    re.IGNORECASE,
)
_CONTENT_KEY = re.compile(
    r"(?:message|messages|content|prompt|transcript|reasoning|chat_history)",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_id(namespace: str, value: Any) -> str:
    digest = hashlib.sha256(f"{namespace}\0{canonical_json(value)}".encode("utf-8")).hexdigest()
    return f"{namespace}_{digest[:24]}"


def sanitize(value: Any, key: str = "", depth: int = 0) -> Any:
    """Remove secrets and private conversation material from persisted fleet state."""
    if depth > 12:
        return "[depth-omitted]"
    if _SECRET_KEY.search(key):
        return "[redacted]"
    if _CONTENT_KEY.search(key):
        return "[content-omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 2_000 else f"{value[:2_000]}…"
    if isinstance(value, list):
        return [sanitize(item, key, depth + 1) for item in value[:2_000]]
    if isinstance(value, dict):
        return {
            str(child_key): sanitize(child_value, str(child_key), depth + 1)
            for child_key, child_value in value.items()
        }
    return sanitize(str(value), key, depth + 1)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
