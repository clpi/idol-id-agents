#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fleet_control.journal import AppendOnlyJournal, project_live
from fleet_control.util import parse_time, sanitize, utc_now

_SAFE_COSTS = frozenset({"local", "free", "included"})


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--maximum-age-seconds", type=int, default=660)
    args = parser.parse_args()
    state = args.state_dir.expanduser()
    findings: list[str] = []

    try:
        events = AppendOnlyJournal(state / "history.ndjson").read()
        live = load(state / "live.json")
        snapshot = load(state / "snapshot.json")
        plan = load(state / "plan.json")
    except Exception as exc:
        print(json.dumps({"ok": False, "findings": [f"state-unavailable:{type(exc).__name__}"]}))
        return 2

    projected = project_live(events)
    if projected.get("history_head") != live.get("history_head"):
        findings.append("persisted Live projection is not at the verified journal head")
    expected_frontier = [event["id"] for event in events if event.get("accepted") is True]
    if live.get("accepted_frontier") != expected_frontier:
        findings.append("accepted frontier is not the exact accepted event projection")

    if plan.get("paygo_allowed") is not False:
        findings.append("latest plan does not explicitly refuse pay-go")
    if plan.get("automatic_merge") is not False:
        findings.append("latest plan does not explicitly refuse automatic merge")
    for provider in snapshot.get("providers", []):
        cost = str(provider.get("cost_class") or "unknown").lower()
        if cost not in _SAFE_COSTS:
            findings.append(f"unsafe provider reached the executable snapshot:{provider.get('id')}:{cost}")
        if not provider.get("control_agent_id"):
            findings.append(f"provider lacks exact local control agent:{provider.get('id')}")

    runtime = snapshot.get("runtime") or {}
    if runtime.get("external_agents_managed") is not False:
        findings.append("external OpenClaw sessions are not explicitly marked non-managed")

    claims: set[str] = set()
    failed_starts: set[str] = set()
    for event in events:
        kind = str(event.get("kind") or "")
        subject = str(event.get("subject") or "")
        payload = event.get("payload") or {}
        result = payload.get("result") or {}
        accepted = event.get("accepted") is True and result.get("ok") is True
        if kind == "claim.acquire.result" and accepted:
            claims.add(subject)
        elif kind == "claim.release.result" and accepted:
            claims.discard(subject)
            failed_starts.discard(subject)
        elif kind == "agent.start.result":
            if result.get("ok") is True:
                if subject not in claims:
                    findings.append(f"agent start lacks a preceding accepted claim:{subject}")
            else:
                failed_starts.add(subject)
    for subject in sorted(failed_starts):
        findings.append(f"failed agent start has no later accepted claim release:{subject}")

    claim_dir = state / "claims"
    for agent in snapshot.get("agents", []):
        if agent.get("status") not in {"starting", "running", "waiting", "reviewing", "integrating"}:
            continue
        agent_id = str(agent.get("id") or "")
        if not (claim_dir / f"{agent_id}.json").is_file():
            findings.append(f"active managed agent has no local claim receipt:{agent_id}")

    reconciled = parse_time(str(live.get("last_reconciled_at") or ""))
    if reconciled is None:
        findings.append("Live projection has no reconciliation time")
    else:
        age = (datetime.now(timezone.utc) - reconciled).total_seconds()
        if age < 0 or age > args.maximum_age_seconds:
            findings.append(f"reconciliation is stale:{int(age)}s")

    result = {
        "schema": "idol.fleet.runtime-verification.v1",
        "checked_at": utc_now().isoformat(),
        "ok": not findings,
        "history_count": len(events),
        "history_head": live.get("history_head"),
        "managed_agent_count": len(snapshot.get("agents", [])),
        "external_agent_count": len(runtime.get("external_agents", [])),
        "provider_count": len(snapshot.get("providers", [])),
        "task_count": len(snapshot.get("tasks", [])),
        "findings": findings,
    }
    print(json.dumps(sanitize(result), sort_keys=True))
    return 0 if not findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
