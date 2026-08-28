from __future__ import annotations

import hashlib
import ipaddress
import json
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .journal import AppendOnlyJournal, JournalIntegrityError, project_live
from .util import canonical_json, sanitize, utc_now


class LiveApiError(RuntimeError):
    pass


def _loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


def _json_file(path: Path, maximum_bytes: int = 32 * 1024 * 1024) -> dict[str, Any]:
    if not path.is_file():
        return {}
    if path.stat().st_size > maximum_bytes:
        raise LiveApiError(f"state file exceeds {maximum_bytes} bytes")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LiveApiError("state file must contain one JSON object")
    return sanitize(value)


class LiveObservatory:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.journal = AppendOnlyJournal(state_dir / "history.ndjson")

    def live(self) -> dict[str, Any]:
        persisted = _json_file(self.state_dir / "live.json")
        if persisted:
            return persisted
        return project_live(self.journal.read())

    def history(self, *, limit: int, after: int | None = None) -> dict[str, Any]:
        events = self.journal.read()
        if after is not None:
            events = [event for event in events if int(event.get("sequence", 0)) > after]
        bounded = events[-limit:]
        return {
            "schema": "idol.live.history.v1",
            "count": len(bounded),
            "history_count": len(events),
            "events": bounded,
        }

    def health(self) -> dict[str, Any]:
        try:
            live = self.live()
            self.journal.read()
        except (OSError, ValueError, json.JSONDecodeError, JournalIntegrityError, LiveApiError) as exc:
            return {
                "schema": "idol.live.health.v1",
                "ok": False,
                "checked_at": utc_now().isoformat(),
                "reason": type(exc).__name__,
            }
        return {
            "schema": "idol.live.health.v1",
            "ok": True,
            "checked_at": utc_now().isoformat(),
            "history_head": live.get("history_head"),
            "history_count": live.get("history_count", 0),
            "last_reconciled_at": live.get("last_reconciled_at"),
            "last_snapshot_at": live.get("last_snapshot_at"),
        }


class LiveHandler(BaseHTTPRequestHandler):
    server_version = "IdolLiveObservatory/1"
    observatory: LiveObservatory

    def do_GET(self) -> None:  # noqa: N802
        if not _loopback(self.client_address[0]):
            self._write(HTTPStatus.FORBIDDEN, {"error": "loopback-only"})
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._write(HTTPStatus.OK, self.observatory.health())
                return
            live = self.observatory.live()
            if parsed.path == "/v1/live":
                self._write(HTTPStatus.OK, live)
                return
            if parsed.path == "/v1/history":
                limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
                after_raw = query.get("after", [None])[0]
                after = int(after_raw) if after_raw not in {None, ""} else None
                self._write(HTTPStatus.OK, self.observatory.history(limit=limit, after=after))
                return
            projection = {
                "/v1/repositories": "repositories",
                "/v1/agents": "agents",
                "/v1/tasks": "tasks",
                "/v1/providers": "providers",
                "/v1/frontier": "accepted_frontier",
            }
            key = projection.get(parsed.path)
            if key:
                self._write(
                    HTTPStatus.OK,
                    {
                        "schema": f"idol.live.{key}.v1",
                        "history_head": live.get("history_head"),
                        key: live.get(key, {} if key != "accepted_frontier" else []),
                    },
                )
                return
            self._write(HTTPStatus.NOT_FOUND, {"error": "not-found"})
        except (ValueError, OSError, json.JSONDecodeError, JournalIntegrityError, LiveApiError) as exc:
            self._write(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "live-state-unavailable", "reason": type(exc).__name__},
            )

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._write(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write(self, status: HTTPStatus, payload: Any) -> None:
        body = canonical_json(sanitize(payload)).encode("utf-8")
        etag = hashlib.sha256(body).hexdigest()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", f'"{etag}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def serve(state_dir: Path, host: str = "127.0.0.1", port: int = 18991) -> None:
    if not _loopback(host):
        raise LiveApiError("the observatory may bind only to loopback")
    observatory = LiveObservatory(state_dir)
    handler = type("BoundLiveHandler", (LiveHandler,), {"observatory": observatory})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)
