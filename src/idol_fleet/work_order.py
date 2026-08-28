from __future__ import annotations

from dataclasses import dataclass

from .model import RepositoryPath, Snapshot, WorkOrder


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    reasons: tuple[str, ...]


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def validate_work_order(order: WorkOrder, snapshot: Snapshot) -> ValidationResult:
    reasons: list[str] = []
    observed = snapshot.repository_heads.get(order.repository)
    if observed is None:
        reasons.append("repository-unobserved")
    elif observed != order.base_sha:
        reasons.append("stale-base-sha")
    for semantic in order.semantic_claims:
        holder = snapshot.active_semantic_claims.get(semantic)
        if holder and holder != order.id:
            reasons.append("semantic-claim-conflict")
            break
    for claimed in order.path_claims:
        for active, holder in snapshot.active_path_claims.items():
            if holder != order.id and _paths_overlap(str(claimed), active):
                reasons.append("path-claim-conflict")
                break
        if "path-claim-conflict" in reasons:
            break
    if not order.goal.strip():
        reasons.append("goal-absent")
    if not order.required_outcome.strip():
        reasons.append("required-outcome-absent")
    if not order.witnesses:
        reasons.append("witnesses-absent")
    if not order.stop_conditions:
        reasons.append("stop-conditions-absent")
    return ValidationResult(not reasons, tuple(sorted(set(reasons))))


def _load_json_list(path):
    import json
    from pathlib import Path
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a JSON list")
    return value


def load_work_orders(path):
    orders = []
    for row in _load_json_list(path):
        if not isinstance(row, dict):
            raise ValueError("work order row must be an object")
        orders.append(WorkOrder(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            repository=str(row["repository"]),
            base_sha=str(row["base_sha"]),
            branch=str(row["branch"]),
            role=str(row["role"]),
            route_id=str(row["route_id"]),
            semantic_claims=tuple(str(v) for v in row.get("semantic_claims", [])),
            path_claims=tuple(RepositoryPath(str(v)) for v in row.get("path_claims", [])),
            goal=str(row["goal"]),
            required_outcome=str(row["required_outcome"]),
            constraints=tuple(str(v) for v in row.get("constraints", [])),
            forbidden_repairs=tuple(str(v) for v in row.get("forbidden_repairs", [])),
            witnesses=tuple(str(v) for v in row.get("witnesses", [])),
            stop_conditions=tuple(str(v) for v in row.get("stop_conditions", [])),
            estimated_seconds=int(row["estimated_seconds"]),
            max_tokens=int(row["max_tokens"]),
            risk=str(row["risk"]),
            reviewer_family=(str(row["reviewer_family"]) if row.get("reviewer_family") else None),
        ))
    return tuple(orders)


def load_tasks(path):
    from .model import Task
    tasks = []
    for row in _load_json_list(path):
        if not isinstance(row, dict):
            raise ValueError("task row must be an object")
        tasks.append(Task(
            id=str(row["id"]),
            role=str(row["role"]),
            priority=int(row.get("priority", 0)),
            criticality=int(row.get("criticality", 0)),
            estimated_seconds=int(row["estimated_seconds"]),
            ready=bool(row.get("ready", False)),
            semantic_targets=tuple(str(v) for v in row.get("semantic_targets", [])),
            path_targets=tuple(RepositoryPath(str(v)) for v in row.get("path_targets", [])),
            resident_routes=tuple(str(v) for v in row.get("resident_routes", [])),
            risk=str(row.get("risk", "unknown")),
            review_required=bool(row.get("review_required", False)),
            repository=(str(row["repository"]) if row.get("repository") else None),
            base_sha=(str(row["base_sha"]) if row.get("base_sha") else None),
            dependencies=tuple(str(v) for v in row.get("dependencies", [])),
        ))
    return tuple(tasks)


def materialize_prompt(order: WorkOrder, *, authority: tuple[tuple[str, str], ...]) -> str:
    forbidden = ("api_key", "password", "private_key", "authorization", "bearer ")
    chunks = [
        "IDOL FLEET WORK ORDER",
        "",
        "AUTHORITY ORDER",
        "1. Exact work-order constraints and stop conditions.",
        "2. Current Idol law, constitution, bootstrap frontier, and named gap evidence.",
        "3. Current base SHA and claimed semantic/source targets.",
        "4. Role-specific execution instructions.",
        "",
        f"ATTEMPT: {order.id}",
        f"TASK: {order.task_id}",
        f"REPOSITORY: {order.repository}",
        f"BASE SHA: {order.base_sha}",
        f"BRANCH: {order.branch}",
        f"ROLE: {order.role}",
        f"GOAL: {order.goal}",
        f"REQUIRED OUTCOME: {order.required_outcome}",
        f"SEMANTIC CLAIMS: {', '.join(order.semantic_claims) or '<none>'}",
        f"PATH CLAIMS: {', '.join(map(str, order.path_claims)) or '<none>'}",
        f"TIME BUDGET SECONDS: {order.estimated_seconds}",
        f"TOKEN BUDGET: {order.max_tokens}",
        "",
        "CONSTRAINTS",
        *(f"- {value}" for value in order.constraints),
        "",
        "FORBIDDEN REPAIRS",
        *(f"- {value}" for value in order.forbidden_repairs),
        "",
        "REQUIRED WITNESSES",
        *(f"- {value}" for value in order.witnesses),
        "",
        "STOP CONDITIONS",
        *(f"- {value}" for value in order.stop_conditions),
        "",
        "AUTHORITY EXCERPTS",
    ]
    for path, text in authority:
        if not isinstance(path, str) or not isinstance(text, str):
            raise TypeError("authority entries must be text")
        lowered = (path + "\n" + text).lower()
        if any(term in lowered for term in forbidden):
            raise ValueError("authority projection contains a credential-shaped value")
        chunks.extend((f"--- {path} ---", text.strip(), ""))
    chunks.extend((
        "RESULT CONTRACT",
        "Return a concise outcome containing: observed base SHA; changed paths; commands run; inner outcomes; unresolved uncertainty; and exact stop reason if held or refused.",
        "Do not include hidden reasoning, credentials, or unrelated file contents.",
        "Do not continue after a stop condition.",
        "",
    ))
    return "\n".join(chunks)
