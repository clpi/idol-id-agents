from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping, Sequence

from .model import mapping, sequence, string_tuple


class InventoryRefusal(RuntimeError):
    pass


_ATOM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ENV = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TERMINAL_STATUS = frozenset({"completed", "done", "failed", "cancelled", "rejected", "superseded", "stopped"})
_FORBIDDEN_KEYS = frozenset({
    "message",
    "messages",
    "content",
    "text",
    "prompt",
    "prompts",
    "transcript",
    "history",
    "reasoning",
    "tool_output",
    "tool_outputs",
    "token",
    "password",
    "secret",
    "cookie",
    "authorization",
    "api_key",
    "private_key",
})


@dataclass(frozen=True, slots=True)
class InventoryConfig:
    enabled: bool
    command: tuple[str, ...]
    cancel_command: tuple[str, ...]
    auth_env: tuple[str, ...]
    timeout_seconds: int
    max_age_seconds: int
    cancel_owned_sessions: bool
    adoptions_file: Path

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None, *, state_dir: Path) -> "InventoryConfig":
        if raw is None:
            return cls(False, (), (), (), 30, 300, False, state_dir / "session-adoptions.json")
        enabled = raw.get("enabled") is True
        command = string_tuple(raw.get("command", ()), "inventory command")
        cancel = string_tuple(raw.get("cancel_command", ()), "cancel command")
        auth_env = string_tuple(raw.get("auth_env", ()), "inventory auth_env")
        for name in auth_env:
            if not _SAFE_ENV.fullmatch(name):
                raise ValueError(f"invalid inventory auth environment {name!r}")
        timeout = int(raw.get("timeout_seconds", 30))
        max_age = int(raw.get("max_age_seconds", 300))
        if timeout < 1 or timeout > 300:
            raise ValueError("inventory timeout outside supported bounds")
        if max_age < 30 or max_age > 86400:
            raise ValueError("inventory max age outside supported bounds")
        path = Path(str(raw.get("adoptions_file") or state_dir / "session-adoptions.json")).expanduser()
        if not path.is_absolute():
            raise ValueError("inventory adoptions file must be absolute")
        if enabled and not command:
            raise ValueError("enabled inventory has no command")
        if raw.get("cancel_owned_sessions") is True and not cancel:
            raise ValueError("session cancellation enabled without a cancel command")
        return cls(
            enabled=enabled,
            command=command,
            cancel_command=cancel,
            auth_env=auth_env,
            timeout_seconds=timeout,
            max_age_seconds=max_age,
            cancel_owned_sessions=raw.get("cancel_owned_sessions") is True,
            adoptions_file=path,
        )


@dataclass(frozen=True, slots=True)
class SessionFact:
    id: str
    status: str
    observed_at: float
    last_activity: float | None
    provider: str | None
    model: str | None
    attempt_id: str | None
    order_id: str | None
    task_id: str | None
    base_sha: str | None
    host: str | None
    actor: str | None

    @property
    def terminal(self) -> bool:
        return self.status.lower() in _TERMINAL_STATUS

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, observed_at: float) -> "SessionFact":
        allowed = {
            "id",
            "status",
            "last_activity",
            "provider",
            "model",
            "attempt_id",
            "order_id",
            "task_id",
            "base_sha",
            "host",
            "actor",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise InventoryRefusal("session fact contains non-contract keys: " + ", ".join(sorted(unknown)))
        session_id = str(raw.get("id", "")).strip()
        status = str(raw.get("status", "")).strip().lower()
        if not _ATOM.fullmatch(session_id) or not _ATOM.fullmatch(status):
            raise InventoryRefusal("session id/status is invalid")
        base = str(raw.get("base_sha", "")).strip() or None
        if base is not None and not _SHA.fullmatch(base):
            raise InventoryRefusal("session base_sha is not exact")
        last = raw.get("last_activity")
        last_activity = float(last) if isinstance(last, (int, float)) else None
        values: dict[str, str | None] = {}
        for key in ("provider", "model", "attempt_id", "order_id", "task_id", "host", "actor"):
            value = str(raw.get(key, "")).strip() or None
            if value is not None and (len(value) > 240 or any(ord(char) < 32 for char in value)):
                raise InventoryRefusal(f"session {key} is invalid")
            values[key] = value
        return cls(
            id=session_id,
            status=status,
            observed_at=observed_at,
            last_activity=last_activity,
            provider=values["provider"],
            model=values["model"],
            attempt_id=values["attempt_id"],
            order_id=values["order_id"],
            task_id=values["task_id"],
            base_sha=base,
            host=values["host"],
            actor=values["actor"],
        )


@dataclass(frozen=True, slots=True)
class InventoryObservation:
    schema: str
    observed_at: float
    source: str
    sessions: tuple[SessionFact, ...]
    agents: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class Adoption:
    session_id: str
    approved: bool
    task_id: str
    order_id: str | None
    base_sha: str
    approved_by: str
    observed_at: float


_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME")


def _environment(config: InventoryConfig) -> dict[str, str]:
    env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    for name in config.auth_env:
        value = os.environ.get(name)
        if value is None:
            raise InventoryRefusal(f"inventory adapter requires absent environment {name}")
        env[name] = value
    env["IDOL_FLEET_NO_MODEL_INFERENCE"] = "1"
    env["IDOL_FLEET_NO_PAYGO"] = "1"
    return env


def _json_object(text: str) -> Mapping[str, Any]:
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
    raise InventoryRefusal("inventory adapter produced no JSON object")


def _reject_forbidden_keys(value: Any, *, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise InventoryRefusal(f"inventory payload contains forbidden content/secret key at {path}.{key}")
            _reject_forbidden_keys(child, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, path=f"{path}[{index}]")


def observe_inventory(config: InventoryConfig, *, now: float | None = None) -> InventoryObservation | None:
    if not config.enabled:
        return None
    current = time.time() if now is None else now
    try:
        result = subprocess.run(
            list(config.command),
            env=_environment(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryRefusal("inventory adapter did not complete") from exc
    if result.returncode != 0:
        raise InventoryRefusal(f"inventory adapter returned {result.returncode}")
    raw = _json_object(result.stdout[:2_000_000])
    _reject_forbidden_keys(raw)
    if str(raw.get("schema", "")) != "idol.fleet.inventory.v1":
        raise InventoryRefusal("inventory schema mismatch")
    observed_at = float(raw.get("observed_at", 0))
    if observed_at > current + 60 or current - observed_at > config.max_age_seconds:
        raise InventoryRefusal("inventory observation is stale or from the future")
    source = str(raw.get("source", "")).strip()
    if not source or len(source) > 240:
        raise InventoryRefusal("inventory source is absent or invalid")
    sessions_raw = sequence(raw.get("sessions", ()), "inventory sessions")
    agents_raw = sequence(raw.get("agents", ()), "inventory agents")
    if len(sessions_raw) > 2000 or len(agents_raw) > 1000:
        raise InventoryRefusal("inventory exceeds bounded cardinality")
    sessions = tuple(
        SessionFact.from_mapping(mapping(item, "session fact"), observed_at=observed_at)
        for item in sessions_raw
    )
    # Agent facts are deliberately tiny and closed: identity, status, provider,
    # model, host and role only. No workspace path or conversational content.
    agents: list[Mapping[str, Any]] = []
    allowed_agent = {"id", "status", "provider", "model", "host", "role"}
    for item in agents_raw:
        row = mapping(item, "agent fact")
        if set(row) - allowed_agent:
            raise InventoryRefusal("agent fact contains non-contract keys")
        agents.append({key: row.get(key) for key in sorted(allowed_agent) if row.get(key) is not None})
    return InventoryObservation(
        schema="idol.fleet.inventory.v1",
        observed_at=observed_at,
        source=source,
        sessions=sessions,
        agents=tuple(agents),
    )


def load_adoptions(path: Path) -> Mapping[str, Adoption]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryRefusal("session adoption file is unreadable") from exc
    rows = sequence(raw, "session adoptions")
    result: dict[str, Adoption] = {}
    for item in rows:
        row = mapping(item, "session adoption")
        allowed = {"session_id", "approved", "task_id", "order_id", "base_sha", "approved_by", "observed_at"}
        if set(row) - allowed:
            raise InventoryRefusal("session adoption contains unknown keys")
        session_id = str(row.get("session_id", "")).strip()
        task_id = str(row.get("task_id", "")).strip()
        base = str(row.get("base_sha", "")).strip()
        approved_by = str(row.get("approved_by", "")).strip()
        if not _ATOM.fullmatch(session_id) or not _ATOM.fullmatch(task_id) or not _SHA.fullmatch(base):
            raise InventoryRefusal("session adoption identity/base is invalid")
        if not approved_by:
            raise InventoryRefusal("session adoption lacks approver provenance")
        adoption = Adoption(
            session_id=session_id,
            approved=row.get("approved") is True,
            task_id=task_id,
            order_id=str(row.get("order_id", "")).strip() or None,
            base_sha=base,
            approved_by=approved_by,
            observed_at=float(row.get("observed_at", 0)),
        )
        if session_id in result:
            raise InventoryRefusal("duplicate session adoption")
        result[session_id] = adoption
    return result


def cancel_session(
    config: InventoryConfig,
    session: SessionFact,
    *,
    attempt_id: str | None,
) -> Mapping[str, Any]:
    if not config.cancel_owned_sessions or not config.cancel_command:
        raise InventoryRefusal("session cancellation is not enabled")
    values = {
        "session_id": session.id,
        "attempt_id": attempt_id or "",
        "task_id": session.task_id or "",
        "order_id": session.order_id or "",
    }
    try:
        command = [part.format_map(values) for part in config.cancel_command]
    except KeyError as exc:
        raise InventoryRefusal(f"cancel command references unknown placeholder {exc.args[0]}") from exc
    try:
        result = subprocess.run(
            command,
            env=_environment(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryRefusal("session cancel command did not complete") from exc
    if result.returncode != 0:
        raise InventoryRefusal(f"session cancel command returned {result.returncode}")
    raw = _json_object(result.stdout[:1_000_000])
    _reject_forbidden_keys(raw)
    status = str(raw.get("status", "")).lower()
    returned_id = str(raw.get("session_id", ""))
    if returned_id != session.id or status not in {"cancelled", "already-terminal", "not-found"}:
        raise InventoryRefusal("session cancel result did not witness the requested bounded outcome")
    return {"session_id": session.id, "status": status, "command": command}


def inventory_fact(observation: InventoryObservation) -> Mapping[str, Any]:
    return {
        "schema": observation.schema,
        "observed_at": observation.observed_at,
        "source": observation.source,
        "sessions": [asdict(session) for session in observation.sessions],
        "agents": list(observation.agents),
    }
