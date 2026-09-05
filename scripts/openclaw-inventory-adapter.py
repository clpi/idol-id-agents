#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

from codex_inventory import observe_codex, scan_processes
from openclaw_transport import observe_local_gateway


GATEWAY_PORT = 18789


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
    command = [*executable, "gateway", "call", method, "--port", str(GATEWAY_PORT)]
    if params is not None:
        command.extend(("--params", json.dumps(params, sort_keys=True, separators=(",", ":"))))
    command.extend(("--json", "--timeout", "5000"))
    before = observe_local_gateway(port=GATEWAY_PORT)
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
    if observe_local_gateway(port=GATEWAY_PORT) != before:
        raise RuntimeError("local OpenClaw gateway identity changed during observation")
    raw = json_object(result.stdout[:4_000_000])
    for key in ("payload", "result", "data"):
        candidate = raw.get(key)
        if isinstance(candidate, Mapping):
            raw = candidate
            break
    reject_content(raw)
    return raw


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


ACTIVE_WORK_METHOD = "idol.fleet.activeWork.snapshot"
ACTIVE_WORK_VERSION = "2026.8.1-beta.3"
ACTIVE_WORK_COUNTERS = frozenset({
    "queueSize", "pendingReplies", "embeddedRuns", "backgroundExecSessions",
    "cronRuns", "activeTasks", "rootRequests", "sessionAdmissions",
    "sessionMutations", "chatRuns", "queuedTurns", "terminalPersistence",
    "terminalSessions",
})
MAX_SAFE_INTEGER = 2**53 - 1


def require_gateway_idle() -> None:
    """Require a fresh complete observation from the pinned gateway extension."""
    started = time.time()
    raw = call(ACTIVE_WORK_METHOD, {})
    finished = time.time()
    expected = {"schema", "version", "openclawVersion", "observedAt", "idle", "counts"}
    if (
        set(raw) != expected
        or raw.get("schema") != "idol.openclaw.active-work"
        or type(raw.get("version")) is not int or raw["version"] != 1
        or raw.get("openclawVersion") != ACTIVE_WORK_VERSION
        or type(raw.get("idle")) is not bool
    ):
        raise RuntimeError("OpenClaw execution snapshot contract is unavailable")
    counts = raw.get("counts")
    if (
        not isinstance(counts, Mapping)
        or set(counts) != ACTIVE_WORK_COUNTERS | {"totalActive"}
        or any(type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER for value in counts.values())
        or counts["totalActive"] != sum(counts[key] for key in ACTIVE_WORK_COUNTERS)
        or raw["idle"] != (counts["totalActive"] == 0)
    ):
        raise RuntimeError("OpenClaw execution counters are incomplete or inconsistent")
    observed = raw.get("observedAt")
    if not isinstance(observed, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", observed
    ):
        raise RuntimeError("OpenClaw execution snapshot time is invalid")
    try:
        observed_at = datetime.fromisoformat(observed[:-1] + "+00:00").timestamp()
    except (ValueError, OverflowError):
        raise RuntimeError("OpenClaw execution snapshot time is invalid") from None
    if finished < started or not started - 1 <= observed_at <= finished + 1:
        raise RuntimeError("OpenClaw execution snapshot is outside the observation window")
    if not raw["idle"]:
        raise RuntimeError("OpenClaw execution is active; additional admission is held")


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
        require_gateway_idle()
        emit_inventory(
            observed_at=observed_at,
            source="openclaw-active-work-snapshot",
            sessions=processes,
            agents=(),
        )
        return 0
    except Exception as exc:
        print(f"openclaw inventory refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
