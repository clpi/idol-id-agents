#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

from codex_inventory import observe_codex, scan_processes


FORBIDDEN = {
    "message",
    "messages",
    "content",
    "text",
    "prompt",
    "transcript",
    "history",
    "reasoning",
    "tooloutput",
    "tooloutputs",
    "token",
    "password",
    "secret",
    "cookie",
    "authorization",
    "apikey",
    "privatekey",
}


def normalize_key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def reject_content(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if normalize_key(str(key)) in FORBIDDEN:
                raise RuntimeError(f"gateway result contains forbidden key {path}.{key}")
            reject_content(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            reject_content(child, f"{path}[{index}]")


def json_object(text: str) -> Mapping[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    for candidate in (text.strip(), *reversed(lines)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    raise RuntimeError("gateway call returned no JSON object")


def openclaw_command() -> list[str]:
    executable = shutil.which("openclaw")
    if executable:
        return [executable]
    home = Path.home()
    candidates = (
        (
            home / ".local/share/mise/installs/node/24/bin/node",
            home / "opt/gw/pkg/dist/entry.js",
        ),
        (
            home / ".local/share/mise/installs/node/24.20.0/bin/node",
            home / "opt/gw/pkg/dist/entry.js",
        ),
    )
    for node, entry in candidates:
        if node.is_file() and entry.is_file():
            return [str(node), str(entry)]
    raise RuntimeError("OpenClaw CLI entrypoint was not found")


def call(method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    executable = openclaw_command()
    command = [*executable, "gateway", "call", method]
    if params is not None:
        command.extend(("--params", json.dumps(params, sort_keys=True, separators=(",", ":"))))
    command.extend(("--json", "--timeout", "5000"))
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"read-only gateway call did not complete for {method}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"read-only gateway call returned {result.returncode} for {method}")
    raw = json_object(result.stdout[:4_000_000])
    for key in ("payload", "result", "data"):
        candidate = raw.get(key)
        if isinstance(candidate, Mapping):
            raw = candidate
            break
    reject_content(raw)
    return raw


def pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def metadata(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("metadata")
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "idolAttemptId",
        "idol_attempt_id",
        "idolOrderId",
        "idol_order_id",
        "idolTaskId",
        "idol_task_id",
        "idolBaseSha",
        "idol_base_sha",
    }
    return {key: value.get(key) for key in allowed if value.get(key) not in (None, "")}


def session_row(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    meta = metadata(value)
    session_id = pick(value, "id", "sessionId", "session_id", "key")
    status = pick(value, "status", "state", "phase")
    if value.get("hasActiveRun") is True or value.get("hasActiveSubagentRun") is True:
        status = "running"
    if session_id is None or status is None:
        return None
    usage = value.get("usage") if isinstance(value.get("usage"), Mapping) else {}
    model = pick(value, "model", "modelId", "model_id") or pick(usage, "model", "modelId")
    provider = pick(value, "provider", "providerId", "provider_id", "modelProvider") or pick(usage, "provider", "providerId")
    row = {
        "id": str(session_id),
        "status": str(status).lower(),
        "last_activity": timestamp(pick(value, "updatedAt", "updated_at", "lastActivityAt", "last_activity")),
        "provider": text(provider),
        "model": text(model),
        "attempt_id": str(pick(meta, "idolAttemptId", "idol_attempt_id")) if pick(meta, "idolAttemptId", "idol_attempt_id") else None,
        "order_id": str(pick(meta, "idolOrderId", "idol_order_id")) if pick(meta, "idolOrderId", "idol_order_id") else None,
        "task_id": str(pick(meta, "idolTaskId", "idol_task_id")) if pick(meta, "idolTaskId", "idol_task_id") else None,
        "base_sha": str(pick(meta, "idolBaseSha", "idol_base_sha")) if pick(meta, "idolBaseSha", "idol_base_sha") else None,
        "host": text(pick(value, "host", "node", "placement")),
        "actor": text(pick(value, "agentId", "agent_id", "actor")),
    }
    return {key: item for key, item in row.items() if item is not None}


def argument(arguments: Sequence[str], *names: str) -> str | None:
    for name in names:
        try:
            index = arguments.index(name)
        except ValueError:
            continue
        if index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def selected_environment(path: Path) -> Mapping[str, str]:
    try:
        entries = path.read_bytes().split(b"\0")
    except OSError:
        return {}
    allowed = {"IDOL_FLEET_ORDER", "IDOL_FLEET_TASK", "IDOL_FLEET_BASE_SHA", "IDOL_FLEET_ROUTE"}
    result = {}
    for entry in entries:
        key, separator, value = entry.partition(b"=")
        name = key.decode("ascii", "ignore")
        if separator and name in allowed:
            result[name] = value.decode("utf-8", "replace")[:240]
    return result


def process_actor(arguments: Sequence[str]) -> str | None:
    names = {Path(part).name for part in arguments[:3]}
    if "hermes" in names and "chat" in arguments:
        return "hermes-cli"
    if names & {"claude", "claude-code"}:
        return "claude-cli"
    if names & {"kiro-cli", "kiro-cli-chat"}:
        return "kiro-cli"
    if names & {"kimi", "kimi-code"}:
        return "kimi-cli"
    if "codex" in names:
        return "codex-cli"
    if names & {"opencode", "opencode-cli"}:
        return "opencode-cli"
    return None


def process_sessions(observed_at: float) -> list[Mapping[str, Any]]:
    observation = observe_codex(scan_processes(), observed_at=observed_at)
    result = list(observation.sessions)
    hostname = socket.gethostname()
    for process in observation.processes:
        if process.identity in observation.covered_processes:
            continue
        arguments = process.arguments
        actor = process_actor(arguments)
        if actor is None:
            continue
        environment = selected_environment(process.directory / "environ")
        row = {
            "id": f"process-{process.pid}-{process.start_time}",
            "status": "running",
            "last_activity": observed_at,
            "provider": argument(arguments, "--provider"),
            "model": argument(arguments, "-m", "--model"),
            "order_id": environment.get("IDOL_FLEET_ORDER"),
            "task_id": environment.get("IDOL_FLEET_TASK"),
            "base_sha": environment.get("IDOL_FLEET_BASE_SHA"),
            "host": hostname,
            "actor": actor,
        }
        result.append({key: value for key, value in row.items() if value not in (None, "")})
    return result


def visible_gateway_sessions() -> list[Mapping[str, Any]]:
    """Read every visible session page; this does not cover cron executions."""
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    deadline = time.monotonic() + 20
    for _ in range(50):
        if time.monotonic() >= deadline:
            raise RuntimeError("gateway session pagination exceeded its time bound")
        page = call("sessions.list", {
            "limit": 200, "offset": offset,
            "includeGlobal": True, "includeUnknown": True,
        })
        items = page.get("sessions")
        count, total, more = page.get("count"), page.get("totalCount"), page.get("hasMore")
        if (
            not isinstance(items, list)
            or type(count) is not int or count != len(items)
            or type(total) is not int or total < offset + count
            or type(more) is not bool
            or page.get("limitApplied") != 200
            or type(page.get("limitApplied")) is not int
            or (offset > 0 and "offset" not in page)
            or ("offset" in page and (type(page["offset"]) is not int or page["offset"] != offset))
            or "nextOffset" not in page
            or len(items) > 200
        ):
            raise RuntimeError("gateway session pagination metadata is incomplete")
        for item in items:
            if not isinstance(item, Mapping):
                raise RuntimeError("gateway session row is malformed")
            key = item.get("key")
            if not isinstance(key, str) or not key or key in seen:
                raise RuntimeError("gateway session keys are missing or repeated")
            if type(item.get("hasActiveRun")) is not bool or (
                "hasActiveSubagentRun" in item and type(item["hasActiveSubagentRun"]) is not bool
            ):
                raise RuntimeError("gateway session activity metadata is incomplete")
            seen.add(key)
            row = session_row(item)
            if row is not None:
                result.append(row)
        next_offset = page.get("nextOffset")
        if not more:
            if next_offset is not None or offset + count != total:
                raise RuntimeError("gateway session pagination did not finish")
            return result
        if (
            not items or type(next_offset) is not int
            or next_offset != offset + count or next_offset >= total
        ):
            raise RuntimeError("gateway session pagination did not advance")
        offset = next_offset
    raise RuntimeError("gateway session pagination exceeded its page bound")


def unidentified_work(sessions: Sequence[Mapping[str, Any]]) -> bool:
    terminal = {"completed", "done", "failed", "cancelled", "rejected", "superseded", "stopped"}
    return any(
        row.get("status") not in terminal
        and not row.get("order_id") and not row.get("task_id")
        for row in sessions
    )


def emit_inventory(
    *,
    observed_at: float,
    source: str,
    sessions: Sequence[Mapping[str, Any]],
    agents: Sequence[Mapping[str, Any]],
) -> None:
    print(
        json.dumps(
            {
                "schema": "idol.fleet.inventory.v1",
                "observed_at": observed_at,
                "source": source,
                "sessions": sessions,
                "agents": agents,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main() -> int:
    try:
        observed_at = time.time()
        processes = process_sessions(observed_at)
        if unidentified_work(processes):
            emit_inventory(
                observed_at=observed_at,
                source="local-process-fast-fence",
                sessions=processes,
                agents=(),
            )
            return 0
        sessions = [*visible_gateway_sessions(), *processes]
        if unidentified_work(sessions):
            emit_inventory(
                observed_at=observed_at,
                source="openclaw-visible-work-fence",
                sessions=sessions,
                agents=(),
            )
            return 0
        # The gateway omits cron runs, including removed jobs that are still settling.
        raise RuntimeError("OpenClaw cron execution coverage is unavailable; idleness is unproven")
    except Exception as exc:
        print(f"openclaw inventory refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
