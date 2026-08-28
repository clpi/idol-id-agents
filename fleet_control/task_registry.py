from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .util import sanitize, stable_id, utc_now


def _json_command(command: list[str], timeout: int = 30) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _issue_is_open(repository: str, number: int) -> tuple[bool, dict[str, Any]]:
    if shutil.which("gh") is None:
        return False, {}
    value = _json_command(["gh", "api", f"repos/{repository}/issues/{number}"])
    if not value:
        return False, {}
    trusted_owner = str((value.get("user") or {}).get("login") or "") == repository.split("/", 1)[0]
    return value.get("state") == "open" and trusted_owner, {
        "number": number,
        "updated_at": value.get("updated_at"),
        "html_url": value.get("html_url"),
    }


def _handoffs(state_dir: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    directory = state_dir / "handoffs"
    if not directory.is_dir():
        return grouped
    for path in directory.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("schema") != "idol.agent.handoff.v1":
            continue
        task_id = str(value.get("task_id") or "")
        if task_id:
            grouped.setdefault(task_id, []).append(sanitize(value))
    for rows in grouped.values():
        rows.sort(key=lambda row: str(row.get("completed_at") or ""))
    return grouped


def _latest(rows: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    matching = [row for row in rows if row.get("role") == role]
    return matching[-1] if matching else None


def _progress(task: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(task)
    architecture = _latest(rows, "architect")
    counterexample = _latest(rows, "counterexample")
    implementation = _latest(rows, "implementer")
    review_rows = [row for row in rows if row.get("role") == "reviewer"]
    evidence = _latest(rows, "evidence")
    integration = _latest(rows, "integrator")

    if any(row.get("verdict") in {"blocked", "refused", "counterexample-found"} for row in rows):
        out["state"] = "blocked"
        out["blocker"] = next(
            (row.get("summary") for row in reversed(rows) if row.get("verdict") in {"blocked", "refused", "counterexample-found"}),
            "structured handoff blocked the task",
        )
        return out

    if out.get("architecture_required") and not architecture:
        out["state"] = "architecture_ready"
        return out
    if architecture:
        out["architecture_accepted"] = architecture.get("verdict") == "accepted"
        out["architecture_handoff"] = architecture.get("id")
        if not out["architecture_accepted"]:
            out["state"] = "blocked"
            return out
        if out.get("counterexample_required", True) and not counterexample:
            out["state"] = "counterexample_ready"
            return out
        if counterexample and counterexample.get("verdict") not in {"accepted", "no-counterexample"}:
            out["state"] = "blocked"
            return out

    if not implementation:
        out["state"] = "implementation_ready"
        return out
    if implementation.get("verdict") not in {"ready-for-review", "accepted"}:
        out["state"] = "blocked"
        return out
    out["state"] = "implemented"
    out["implementer_family"] = implementation.get("provider_family")
    out["implementation_handoff"] = implementation.get("id")

    accepted_reviews = [row for row in review_rows if row.get("verdict") == "accepted"]
    out["reviews"] = [
        {
            "id": row.get("id"),
            "provider_family": row.get("provider_family"),
            "verdict": row.get("verdict"),
        }
        for row in review_rows
    ]
    if not accepted_reviews:
        out["state"] = "review_ready"
        return out
    if not evidence:
        out["state"] = "evidence_ready"
        return out
    out["evidence_status"] = evidence.get("verdict")
    if evidence.get("verdict") != "pass":
        out["state"] = "blocked"
        return out
    if not integration:
        out["state"] = "integration_ready"
        return out
    if integration.get("verdict") == "ready-for-admission":
        out["state"] = "admission_ready"
        out["integration_handoff"] = integration.get("id")
    else:
        out["state"] = "blocked"
    return out


def materialize_tasks(
    *,
    repositories: list[dict[str, Any]],
    roster_path: Path,
    state_dir: Path,
) -> list[dict[str, Any]]:
    try:
        roster = json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    repository_name = str(roster.get("repository") or "")
    repo_id = repository_name.split("/")[-1]
    repo = next((row for row in repositories if row.get("id") == repo_id), None)
    if not repo:
        return []
    handoffs = _handoffs(state_dir)
    tasks: list[dict[str, Any]] = []
    for authored in roster.get("tasks", []):
        if not isinstance(authored, dict):
            continue
        issue = int(authored.get("issue", 0))
        open_now, issue_fact = _issue_is_open(repository_name, issue)
        if not open_now:
            continue
        task = {
            **authored,
            "repo_id": repo_id,
            "base_sha": repo["head_sha"],
            "work_order": f"registry://{repository_name}/{authored['id']}@{repo['head_sha']}",
            "review_required": True,
            "lease_seconds": int(authored.get("lease_seconds", 1800)),
            "authority": {
                "repository": repository_name,
                "issue": issue_fact,
                "head_sha": repo["head_sha"],
                "law_sha256": repo.get("law_sha256"),
                "constitution_sha256": repo.get("constitution_sha256"),
                "bootstrap_sha256": repo.get("bootstrap_sha256"),
                "materialized_at": utc_now().isoformat(),
            },
        }
        task["work_order_id"] = stable_id("work", task)
        tasks.append(_progress(task, handoffs.get(str(task["id"]), [])))
    return sanitize(tasks)
