from __future__ import annotations

import fcntl
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .controller import FleetController, FleetPolicy
from .journal import AppendOnlyJournal, project_live
from .util import atomic_write_json, sanitize, utc_now


class ApplyRefused(RuntimeError):
    pass


_ALLOWED_ACTIONS = frozenset(
    {"agent.checkpoint", "agent.suspend", "agent.stop", "claim.acquire", "claim.release", "agent.start"}
)


@dataclass(frozen=True)
class RuntimeConfig:
    state_dir: Path
    snapshot_command: tuple[str, ...]
    action_commands: dict[str, tuple[str, ...]]
    actor: str = "idol-fleet-controller"
    apply_enabled: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeConfig":
        commands = {
            str(kind): tuple(str(part) for part in parts)
            for kind, parts in (value.get("action_commands") or {}).items()
        }
        return cls(
            state_dir=Path(value["state_dir"]).expanduser(),
            snapshot_command=tuple(str(part) for part in value["snapshot_command"]),
            action_commands=commands,
            actor=str(value.get("actor") or "idol-fleet-controller"),
            apply_enabled=bool(value.get("apply_enabled", False)),
        )


class FleetRuntime:
    def __init__(self, runtime: RuntimeConfig, policy: FleetPolicy):
        self.runtime = runtime
        self.policy = policy
        self.journal = AppendOnlyJournal(runtime.state_dir / "history.ndjson")

    def tick(self, *, apply: bool = False) -> dict[str, Any]:
        self.runtime.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime.state_dir / "fleet.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ApplyRefused("another fleet reconciliation is active") from exc
            return self._tick_locked(apply=apply)

    def _tick_locked(self, *, apply: bool) -> dict[str, Any]:
        snapshot = sanitize(self._run_json(self.runtime.snapshot_command, timeout=120))
        atomic_write_json(self.runtime.state_dir / "snapshot.json", snapshot)
        plan = FleetController(self.policy).plan(snapshot)
        atomic_write_json(self.runtime.state_dir / "plan.json", plan)
        self.journal.append(
            kind="fleet.plan",
            subject="fleet",
            actor=self.runtime.actor,
            payload=plan,
            authority=self._authority(snapshot),
            accepted=False,
        )

        results: list[dict[str, Any]] = []
        if apply:
            self._assert_apply_authorized()
            for action in plan["actions"]:
                results.append(self._execute_action(action, snapshot))
        projection = project_live(self.journal.read())
        projection["last_reconciled_at"] = utc_now().isoformat()
        projection["last_results"] = results
        atomic_write_json(self.runtime.state_dir / "live.json", projection, mode=0o640)
        return {"plan": plan, "results": results, "projection": projection}

    def _assert_apply_authorized(self) -> None:
        if not self.runtime.apply_enabled:
            raise ApplyRefused("runtime config does not enable apply")
        if os.environ.get("IDOL_FLEET_APPLY") != "1":
            raise ApplyRefused("IDOL_FLEET_APPLY=1 is required")
        if os.environ.get("IDOL_FLEET_COST_BOUNDARY") != "INCLUDED_ONLY":
            raise ApplyRefused("IDOL_FLEET_COST_BOUNDARY=INCLUDED_ONLY is required")
        if self.policy.allow_paygo or self.policy.automatic_merge:
            raise ApplyRefused("unsafe policy cannot be applied")

    def _execute_action(self, action: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        kind = str(action["kind"])
        if kind not in _ALLOWED_ACTIONS:
            raise ApplyRefused(f"action is not allowlisted: {kind}")
        template = self.runtime.action_commands.get(kind)
        if not template:
            result = {"action_id": action["id"], "kind": kind, "ok": False, "reason": "adapter-command-not-configured"}
            self._journal_result(action, result, snapshot)
            return result
        substitutions = {
            "action_json": json.dumps(action, separators=(",", ":")),
            "payload_json": json.dumps(action.get("payload", {}), separators=(",", ":")),
            "agent_id": str(action.get("agent_id", "")),
        }
        command = tuple(part.format_map(substitutions) for part in template)
        try:
            output = self._run_json(command, timeout=300)
            result = {
                "action_id": action["id"],
                "kind": kind,
                "ok": bool(output.get("ok", False)),
                "result": sanitize(output),
            }
        except Exception as exc:
            result = {
                "action_id": action["id"],
                "kind": kind,
                "ok": False,
                "reason": type(exc).__name__,
                "detail": str(exc)[:300],
            }
        self._journal_result(action, result, snapshot)
        if kind == "claim.acquire" and not result["ok"]:
            raise ApplyRefused("claim acquisition failed; dependent agent start is refused")
        return result

    def _journal_result(self, action: dict[str, Any], result: dict[str, Any], snapshot: dict[str, Any]) -> None:
        self.journal.append(
            kind=f"{action['kind']}.result",
            subject=str(action.get("agent_id") or action["id"]),
            actor=self.runtime.actor,
            payload={"action": action, "result": result},
            authority=self._authority(snapshot),
            accepted=bool(result.get("ok")),
        )

    @staticmethod
    def _run_json(command: tuple[str, ...], timeout: int) -> dict[str, Any]:
        if not command:
            raise ApplyRefused("empty command")
        proc = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "NO_MODEL_INFERENCE": "1"},
        )
        if proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): tool={Path(command[0]).name}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("command did not return one JSON object") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("command JSON must be an object")
        return payload

    @staticmethod
    def _authority(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "repositories": [
                {
                    "id": row.get("id"),
                    "head_sha": row.get("head_sha"),
                    "law_sha256": row.get("law_sha256"),
                    "constitution_sha256": row.get("constitution_sha256"),
                    "bootstrap_sha256": row.get("bootstrap_sha256"),
                }
                for row in snapshot.get("repositories", [])
            ]
        }
