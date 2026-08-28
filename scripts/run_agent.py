#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fleet_control.agent_protocol import build_prompt, parse_handoff
from fleet_control.util import atomic_write_json, sanitize, utc_now


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _command(config: dict[str, Any], payload: dict[str, Any], prompt: str) -> list[str]:
    openclaw = config.get("openclaw") or {}
    executable = str(openclaw.get("executable") or "")
    subcommand = str(openclaw.get("agent_subcommand") or "")
    message_flag = str(openclaw.get("message_flag") or "")
    session_flag = str(openclaw.get("session_flag") or "")
    agent_flag = str(openclaw.get("agent_flag") or "")
    json_flag = str(openclaw.get("json_flag") or "")
    if not executable or not subcommand or not message_flag:
        raise ValueError("OpenClaw one-shot agent adapter is not configured")
    command = [executable, subcommand, message_flag, prompt]
    control_agent_id = payload.get("control_agent_id")
    if control_agent_id and agent_flag:
        command.extend([agent_flag, str(control_agent_id)])
    if session_flag:
        command.extend([session_flag, str(payload["agent_id"])])
    if json_flag:
        command.append(json_flag)
    return command


def _usage(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if any(part in key.lower() for part in ("usage", "token", "limit", "reset")):
                out[key] = sanitize(child, key)
            elif isinstance(child, (dict, list)):
                nested = _usage(child, depth + 1)
                if nested:
                    out[key] = nested
        return out or None
    if isinstance(value, list):
        rows = [nested for child in value if (nested := _usage(child, depth + 1))]
        return rows or None
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--checkpoint", action="store_true")
    args = parser.parse_args()

    action = _read(args.action)
    config = _read(args.config)
    payload = dict(action.get("payload") or {})
    payload.setdefault("agent_id", action.get("agent_id"))
    agent_id = str(payload["agent_id"])
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.state_dir / "agents" / f"{agent_id}.json"
    state = {
        "schema": "idol.agent.run.v1",
        "id": agent_id,
        "task_id": payload.get("task_id"),
        "role": payload.get("role"),
        "provider_id": payload.get("provider_id"),
        "provider_family": payload.get("provider_family"),
        "base_sha": payload.get("base_sha"),
        "status": "running",
        "started_at": utc_now().isoformat(),
        "last_activity_at": utc_now().isoformat(),
        "claims": {
            "paths": payload.get("paths", []),
            "semantic_boundaries": payload.get("semantic_boundaries", []),
        },
        "checkpoint": args.checkpoint,
        "pid": os.getpid(),
    }
    atomic_write_json(state_path, state)

    timeout_minutes = int(payload.get("estimate_minutes") or 180) + 30
    try:
        command = _command(config, payload, build_prompt(payload, checkpoint=args.checkpoint))
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=max(300, timeout_minutes * 60),
            check=False,
            env={
                **os.environ,
                "IDOL_FLEET_TASK_ID": str(payload.get("task_id") or ""),
                "IDOL_FLEET_AGENT_ID": agent_id,
                "IDOL_FLEET_BASE_SHA": str(payload.get("base_sha") or ""),
                "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY",
            },
        )
        state["returncode"] = proc.returncode
        state["last_activity_at"] = utc_now().isoformat()
        raw = proc.stdout
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        usage = _usage(parsed) if parsed is not None else None
        if usage:
            state["usage"] = usage
        if proc.returncode != 0:
            state["status"] = "failed"
            state["failure"] = "agent-command-nonzero"
            state["stderr_bytes"] = len(proc.stderr.encode("utf-8"))
            atomic_write_json(state_path, state)
            return 2
        handoff = parse_handoff(raw, expected=payload)
        handoff["agent_id"] = agent_id
        handoff["provider_id"] = payload.get("provider_id")
        handoff["source"] = "local-openclaw-agent"
        handoff_path = args.state_dir / "handoffs" / f"{agent_id}.json"
        atomic_write_json(handoff_path, handoff)
        state["status"] = "completed"
        state["handoff_id"] = handoff["id"]
        state["completed_at"] = utc_now().isoformat()
        atomic_write_json(state_path, state)
        print(json.dumps({"ok": True, "agent_id": agent_id, "handoff_id": handoff["id"]}))
        return 0
    except subprocess.TimeoutExpired:
        state["status"] = "timed_out"
        state["failure"] = "agent-timeout"
    except Exception as exc:
        state["status"] = "failed"
        state["failure"] = type(exc).__name__
        state["detail"] = str(exc)[:300]
    state["completed_at"] = utc_now().isoformat()
    atomic_write_json(state_path, state)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
