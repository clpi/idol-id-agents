#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fleet_control.util import atomic_write_json, sanitize, utc_now


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "NO_MODEL_INFERENCE": "1", "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY"},
    )


def openclaw_rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    encoded = json.dumps(params or {}, separators=(",", ":"))
    for command in (
        ["openclaw", "gateway", "call", method, "--json", "--params", encoded],
        ["openclaw", "gateway", "call", method, "--json", encoded],
        ["openclaw", "gateway", "call", method, "--json"],
    ):
        proc = run(command)
        if proc.returncode != 0:
            continue
        try:
            value = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def flag(help_text: str, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if re.search(rf"(?:^|[ ,|\[])({re.escape(candidate)})(?:[ =,|\]])", help_text, re.M):
            return candidate
    return None


def claim_mode(tool: Path, repo: Path) -> str | None:
    text = ""
    for args in (["--help"], ["help"], []):
        proc = run([str(tool), *args], cwd=repo, timeout=20)
        text += proc.stdout + "\n" + proc.stderr
    try:
        text += "\n" + tool.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if re.search(r"\bacquire\b", text) and re.search(r"--agent(?:-id)?\b", text):
        return "flagged-acquire"
    if re.search(r"\bclaim\b", text) and re.search(r"--agent(?:-id)?\b", text):
        return "flagged-claim"
    if re.search(r"(?:usage|commands?).{0,160}\bclaim\b", text, re.I | re.S):
        return "positional-claim"
    if re.search(r"(?:usage|commands?).{0,160}\bacquire\b", text, re.I | re.S):
        return "positional-acquire"
    return None


def agent_catalog() -> list[dict[str, Any]]:
    value = openclaw_rpc("agents.list") or {}
    rows = value.get("agents") or value.get("items") or []
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        agent_id = str(row.get("id") or row.get("agentId") or "").strip()
        if agent_id:
            out.append({"id": agent_id, "name": str(row.get("name") or row.get("displayName") or agent_id)})
    return out


def resolve_agents(policy: dict[str, Any], catalog: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    updated = json.loads(json.dumps(policy))
    ambiguous: dict[str, list[str]] = {}
    for provider_id, row in updated.items():
        if not isinstance(row, dict) or row.get("enabled", True) is not True:
            continue
        explicit = str(row.get("control_agent_id") or "").strip()
        ids = {str(agent["id"]) for agent in catalog}
        if explicit and explicit in ids:
            continue
        aliases = {str(value).strip().lower() for value in row.get("agent_aliases", []) if str(value).strip()}
        matches = [
            agent["id"]
            for agent in catalog
            if str(agent["id"]).lower() in aliases or str(agent["name"]).lower() in aliases
        ]
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            row["control_agent_id"] = matches[0]
        elif matches:
            ambiguous[provider_id] = matches
    return updated, ambiguous


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--provider-policy", type=Path, required=True)
    parser.add_argument("--provider-output", type=Path, required=True)
    parser.add_argument("--adapter-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()

    openclaw = shutil.which("openclaw")
    claim = args.repo / "tools/node/dev/claim"
    report: dict[str, Any] = {
        "schema": "idol.fleet.adapter-probe.v1",
        "observed_at": utc_now().isoformat(),
        "no_model_inference": True,
        "no_payg_usage": True,
        "ready": False,
        "checks": {},
    }
    if not openclaw:
        report["checks"]["openclaw"] = "absent"
        atomic_write_json(args.report_output, report)
        return 2
    help_proc = run([openclaw, "agent", "--help"])
    help_text = help_proc.stdout + "\n" + help_proc.stderr
    message_flag = flag(help_text, ["--message", "-m"])
    agent_flag = flag(help_text, ["--agent", "--agent-id"])
    session_flag = flag(help_text, ["--session-id", "--session", "--session-key"])
    json_flag = flag(help_text, ["--json"])
    openclaw_ready = help_proc.returncode == 0 and bool(message_flag and agent_flag and session_flag)
    report["checks"]["openclaw_agent_cli"] = {
        "ready": openclaw_ready,
        "message_flag": message_flag,
        "agent_flag": agent_flag,
        "session_flag": session_flag,
        "json_flag": json_flag,
    }

    mode = claim_mode(claim, args.repo) if claim.is_file() else None
    report["checks"]["claim"] = {"ready": bool(mode), "mode": mode, "tool_present": claim.is_file()}
    health = openclaw_rpc("health")
    usage = openclaw_rpc("usage.status")
    report["checks"]["gateway_health"] = bool(health)
    report["checks"]["usage_telemetry"] = bool(usage)

    policy = json.loads(args.provider_policy.read_text(encoding="utf-8"))
    catalog = agent_catalog()
    providers, ambiguous = resolve_agents(policy, catalog)
    mapped = sorted(
        provider_id
        for provider_id, row in providers.items()
        if isinstance(row, dict)
        and row.get("enabled", True) is True
        and row.get("control_agent_id")
        and row.get("cost_class") in {"included", "local", "free"}
    )
    report["checks"]["provider_agents"] = {
        "catalog_count": len(catalog),
        "mapped": mapped,
        "ambiguous": ambiguous,
    }

    adapter = {
        "schema": "idol.fleet.adapter.v1",
        "state_dir": str(args.state_dir.expanduser()),
        "repositories": {"idol": str(args.repo.resolve())},
        "claim": {"tool": str(claim.resolve()), "mode": mode},
        "openclaw": {
            "executable": openclaw,
            "agent_subcommand": "agent",
            "message_flag": message_flag,
            "agent_flag": agent_flag,
            "session_flag": session_flag,
            "json_flag": json_flag,
        },
        "runner_path": str((args.install_root / "scripts/run_agent.py").resolve()),
        "adapter_config_path": str(args.adapter_output.expanduser().resolve()),
    }
    report["ready"] = bool(openclaw_ready and mode and health and usage and mapped and not ambiguous)
    atomic_write_json(args.provider_output.expanduser(), providers)
    atomic_write_json(args.adapter_output.expanduser(), adapter)
    atomic_write_json(args.report_output.expanduser(), sanitize(report))
    print(json.dumps(sanitize(report), sort_keys=True))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
