#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from fleet_control.util import atomic_write_json, sanitize, utc_now


class AdapterRefused(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AdapterRefused(f"{path} must contain one JSON object")
    return value


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "NO_MODEL_INFERENCE": "1", "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY"},
    )


def _head(repo: Path) -> str:
    proc = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    if proc.returncode != 0:
        raise AdapterRefused("cannot read repository HEAD")
    return proc.stdout.strip()


def _claim_text(tool: Path, repo: Path) -> str:
    outputs: list[str] = []
    for args in (["list"], ["status"], []):
        proc = _run([str(tool), *args], cwd=repo, timeout=30)
        if proc.returncode == 0:
            outputs.append(proc.stdout)
    return "\n".join(outputs)


def _claim_modes(tool: Path, repo: Path, configured: str | None) -> list[str]:
    if configured:
        return [configured]
    help_text = ""
    for args in (["--help"], ["help"], []):
        proc = _run([str(tool), *args], cwd=repo, timeout=20)
        help_text += proc.stdout + "\n" + proc.stderr
    try:
        source = tool.read_text(encoding="utf-8", errors="replace")
    except OSError:
        source = ""
    text = f"{help_text}\n{source}"
    modes: list[str] = []
    if re.search(r"\bacquire\b", text) and re.search(r"--agent(?:-id)?\b", text):
        modes.append("flagged-acquire")
    if re.search(r"\bclaim\b", text) and re.search(r"--agent(?:-id)?\b", text):
        modes.append("flagged-claim")
    if re.search(r"(?:usage|commands?).{0,120}\bclaim\b", text, re.I | re.S):
        modes.append("positional-claim")
    if re.search(r"(?:usage|commands?).{0,120}\bacquire\b", text, re.I | re.S):
        modes.append("positional-acquire")
    return list(dict.fromkeys(modes))


def _claim_command(mode: str, tool: Path, payload: dict[str, Any]) -> list[str]:
    agent_id = str(payload["agent_id"])
    task_id = str(payload["task_id"])
    boundaries = ",".join(str(v) for v in payload.get("semantic_boundaries", []))
    detail = f"{task_id}; boundaries={boundaries}; base={payload['base_sha']}"
    paths = [str(path) for path in payload.get("paths", [])]
    if mode == "positional-claim":
        return [str(tool), "claim", agent_id, detail, *paths]
    if mode == "positional-acquire":
        return [str(tool), "acquire", agent_id, detail, *paths]
    verb = "acquire" if mode == "flagged-acquire" else "claim"
    return [str(tool), verb, "--agent", agent_id, "--detail", detail, "--files", *paths]


def _release_commands(mode: str, tool: Path, agent_id: str) -> list[list[str]]:
    commands = [[str(tool), "release", agent_id]]
    if mode.startswith("flagged"):
        commands.insert(0, [str(tool), "release", "--agent", agent_id])
    commands.append([str(tool), "unclaim", agent_id])
    return commands


def claim_acquire(action: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    payload = {**(action.get("payload") or {}), "agent_id": action["agent_id"]}
    repo = Path(config["repositories"][payload["repo_id"]]).expanduser()
    if _head(repo) != payload["base_sha"]:
        raise AdapterRefused("repository moved after planning")
    tool = Path((config.get("claim") or {}).get("tool") or repo / "tools/node/dev/claim")
    if not tool.is_file():
        raise AdapterRefused("canonical claim tool is absent")
    before = _claim_text(tool, repo)
    if str(payload["agent_id"]) in before:
        return {"ok": True, "already_held": True, "agent_id": payload["agent_id"]}
    modes = _claim_modes(tool, repo, (config.get("claim") or {}).get("mode"))
    errors: list[str] = []
    for mode in modes:
        proc = _run(_claim_command(mode, tool, payload), cwd=repo, timeout=30)
        after = _claim_text(tool, repo)
        if proc.returncode == 0 and str(payload["agent_id"]) in after:
            record = {
                "schema": "idol.claim.adapter.v1",
                "agent_id": payload["agent_id"],
                "task_id": payload["task_id"],
                "repo_id": payload["repo_id"],
                "base_sha": payload["base_sha"],
                "mode": mode,
                "paths": payload.get("paths", []),
                "semantic_boundaries": payload.get("semantic_boundaries", []),
                "acquired_at": utc_now().isoformat(),
            }
            state_dir = Path(config["state_dir"]).expanduser()
            atomic_write_json(state_dir / "claims" / f"{payload['agent_id']}.json", record)
            return {"ok": True, "agent_id": payload["agent_id"], "mode": mode}
        errors.append(f"{mode}:{proc.returncode}")
    raise AdapterRefused("claim acquisition did not produce an observable exact holder: " + ",".join(errors))


def claim_release(action: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    agent_id = str(action["agent_id"])
    payload = action.get("payload") or {}
    repo_id = str(payload.get("repo_id") or "idol")
    repo = Path(config["repositories"][repo_id]).expanduser()
    claim_record = Path(config["state_dir"]).expanduser() / "claims" / f"{agent_id}.json"
    mode = (config.get("claim") or {}).get("mode")
    if claim_record.is_file():
        try:
            mode = _read(claim_record).get("mode") or mode
        except Exception:
            pass
    tool = Path((config.get("claim") or {}).get("tool") or repo / "tools/node/dev/claim")
    for command in _release_commands(str(mode or "positional-claim"), tool, agent_id):
        proc = _run(command, cwd=repo, timeout=30)
        if proc.returncode == 0 and agent_id not in _claim_text(tool, repo):
            try:
                claim_record.unlink()
            except FileNotFoundError:
                pass
            return {"ok": True, "agent_id": agent_id}
    if agent_id not in _claim_text(tool, repo):
        return {"ok": True, "agent_id": agent_id, "already_released": True}
    raise AdapterRefused("claim release was not observable")


def _agent_state(state_dir: Path, agent_id: str) -> dict[str, Any] | None:
    path = state_dir / "agents" / f"{agent_id}.json"
    if not path.is_file():
        return None
    try:
        return _read(path)
    except Exception:
        return None


def agent_start(action: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    payload = action.get("payload") or {}
    if not payload.get("control_agent_id"):
        raise AdapterRefused("provider has no exact preconfigured OpenClaw control agent")
    state_dir = Path(config["state_dir"]).expanduser()
    agent_id = str(action["agent_id"])
    existing = _agent_state(state_dir, agent_id)
    if existing and existing.get("status") in {"running", "starting"}:
        return {"ok": True, "agent_id": agent_id, "already_running": True, "pid": existing.get("pid")}
    start_action = state_dir / "actions" / f"start-{agent_id}.json"
    atomic_write_json(start_action, action)
    runner = Path(config["runner_path"]).expanduser()
    adapter_path = Path(config["adapter_config_path"]).expanduser()
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"{agent_id}.log").open("ab", buffering=0)
    command = [
        sys.executable,
        str(runner),
        "--action", str(start_action),
        "--config", str(adapter_path),
        "--state-dir", str(state_dir),
    ]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
    )
    record = {
        "schema": "idol.agent.run.v1",
        "id": agent_id,
        "task_id": payload.get("task_id"),
        "role": payload.get("role"),
        "provider_id": payload.get("provider_id"),
        "provider_family": payload.get("provider_family"),
        "base_sha": payload.get("base_sha"),
        "status": "starting",
        "started_at": utc_now().isoformat(),
        "last_activity_at": utc_now().isoformat(),
        "pid": proc.pid,
        "claims": {
            "paths": payload.get("paths", []),
            "semantic_boundaries": payload.get("semantic_boundaries", []),
        },
    }
    atomic_write_json(state_dir / "agents" / f"{agent_id}.json", record)
    return {"ok": True, "agent_id": agent_id, "pid": proc.pid}


def agent_checkpoint(action: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    state_dir = Path(config["state_dir"]).expanduser()
    agent_id = str(action["agent_id"])
    original = state_dir / "actions" / f"start-{agent_id}.json"
    if not original.is_file():
        return {"ok": False, "agent_id": agent_id, "reason": "start-action-absent"}
    runner = Path(config["runner_path"]).expanduser()
    proc = _run(
        [
            sys.executable,
            str(runner),
            "--action", str(original),
            "--config", str(Path(config["adapter_config_path"]).expanduser()),
            "--state-dir", str(state_dir),
            "--checkpoint",
        ],
        timeout=240,
    )
    return {"ok": proc.returncode == 0, "agent_id": agent_id, "returncode": proc.returncode}


def agent_suspend(action: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    state_dir = Path(config["state_dir"]).expanduser()
    agent_id = str(action["agent_id"])
    state = _agent_state(state_dir, agent_id)
    if not state:
        return {"ok": True, "agent_id": agent_id, "already_absent": True}
    pid = state.get("pid")
    if isinstance(pid, int) and pid > 1:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    state["status"] = "suspended"
    state["suspended_at"] = utc_now().isoformat()
    state["suspend_reason"] = (action.get("payload") or {}).get("reason")
    atomic_write_json(state_dir / "agents" / f"{agent_id}.json", state)
    return {"ok": True, "agent_id": agent_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["claim-acquire", "claim-release", "start", "checkpoint", "suspend", "stop"])
    parser.add_argument("action_file", type=Path)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("IDOL_FLEET_ADAPTER_CONFIG", "~/.config/idol/fleet-adapter.json")).expanduser())
    args = parser.parse_args()
    try:
        action = _read(args.action_file)
        config = _read(args.config)
        operations = {
            "claim-acquire": claim_acquire,
            "claim-release": claim_release,
            "start": agent_start,
            "checkpoint": agent_checkpoint,
            "suspend": agent_suspend,
            "stop": agent_suspend,
        }
        result = operations[args.operation](action, config)
        print(json.dumps(sanitize(result), sort_keys=True))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": type(exc).__name__, "detail": str(exc)[:300]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
