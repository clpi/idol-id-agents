from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from typing import Sequence

from .calibration import is_enabled
from .claims import RepositoryClaimClient, SchedulerLease, SemanticClaimStore
from .coordinator import Dispatcher
from .journal import Journal
from .model import jsonable, Snapshot
from .observe import observe_git_repository
from .policy import Policy
from .runtime import HermesRuntime, OpenClawRuntime
from .scheduler import Scheduler
from .work_order import load_tasks, load_work_orders
from .worktree import WorktreeManager


def _private_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(jsonable(value), sort_keys=True, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.close(fd)


def _parse_repositories(values: Sequence[str]) -> tuple[tuple[str, Path], ...]:
    repositories = []
    for value in values:
        if "=" not in value:
            raise ValueError("repository must be ID=PATH")
        identity, raw_path = value.split("=", 1)
        if "/" not in identity or not raw_path:
            raise ValueError("repository must be owner/name=path")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"repository path does not exist: {path}")
        repositories.append((identity, path))
    return tuple(repositories)


def _terminal_attempt_ids(journal: Journal) -> set[str]:
    terminal_kinds = {"attempt-ready", "attempt-review", "attempt-rejected", "attempt-failed", "attempt-stale", "attempt-preserved"}
    result: set[str] = set()
    for event in journal.read():
        if event.get("kind") not in terminal_kinds:
            continue
        fact = event.get("fact")
        if isinstance(fact, dict) and isinstance(fact.get("attempt"), str):
            result.add(str(fact["attempt"]))
    return result


def _authority_projection(repository: Path) -> tuple[tuple[str, str], ...]:
    paths = ("docs/spec/law.md", "docs/spec/constitution.md", "docs/bootstrap.md")
    result: list[tuple[str, str]] = []
    for relative in paths:
        path = repository / relative
        if not path.is_file():
            raise ValueError(f"required authority file is absent: {relative}")
        text = path.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > 2_000_000:
            raise ValueError(f"authority file exceeds projection limit: {relative}")
        result.append((relative, text))
    return tuple(result)


def _dispatch_assignments(
    *,
    policy: Policy,
    plan,
    orders_path: Path,
    repositories: tuple[tuple[str, Path], ...],
    state: Path,
    calibration: Path | None,
    config_dir: Path | None,
) -> list[dict[str, object]]:
    if policy.mode != "apply":
        return []
    if not is_enabled(calibration, config_dir):
        raise PermissionError("apply policy is present but calibration enablement is absent or stale")
    repository_map = dict(repositories)
    orders = {order.task_id: order for order in load_work_orders(orders_path)}
    journal = Journal(state / "events.jsonl")
    terminal = _terminal_attempt_ids(journal)
    outcomes: list[dict[str, object]] = []
    with SchedulerLease(state / "leases", owner=f"idol-fleet-{os.getpid()}", ttl=600):
        for assignment in plan.assignments:
            template = orders.get(assignment.task_id)
            if template is None or template.id in terminal:
                continue
            route = policy.route(assignment.route_id)
            if route is None:
                raise ValueError(f"planned route is absent from policy: {assignment.route_id}")
            eligibility = policy.route_eligibility(route.id, template.role)
            if not eligibility.eligible:
                raise PermissionError("planned route became ineligible: " + ", ".join(eligibility.reasons))
            repository = repository_map.get(template.repository)
            if repository is None:
                raise ValueError(f"work order repository is not configured: {template.repository}")
            order = replace(template, route_id=route.id)
            executable = route.executable or ("hermes" if route.runtime == "hermes" else "openclaw")
            runtime = HermesRuntime(executable=executable) if route.runtime == "hermes" else OpenClawRuntime(executable=executable)
            dispatcher = Dispatcher(
                journal=journal,
                semantic_claims=SemanticClaimStore(state / "claims"),
                repository_claims=RepositoryClaimClient(repository),
                worktrees=WorktreeManager(state / "worktrees"),
                apply_enabled=True,
            )
            outcome = dispatcher.dispatch(order, route, repository, runtime, authority=_authority_projection(repository))
            outcomes.append({
                "attempt_id": outcome.attempt_id,
                "state": outcome.state,
                "commit_sha": outcome.commit_sha,
                "changed_paths": list(outcome.changed_paths),
                "worktree_hash": hashlib.sha256(str(outcome.worktree).encode()).hexdigest()[:16],
                "usage_total": outcome.run.usage_total,
                "cost_usd": outcome.run.cost_usd,
            })
    return outcomes


def _run_cycle(
    policy_path: Path,
    tasks_path: Path,
    state: Path,
    repository_values: Sequence[str],
    *,
    dispatch: bool = False,
    orders_path: Path | None = None,
    calibration: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, object]:
    policy = Policy.load(policy_path)
    repositories = _parse_repositories(repository_values)
    observations = [observe_git_repository(path, identity) for identity, path in repositories]
    semantic_store = SemanticClaimStore(state / "claims")
    semantic_claims = {
        str(row.get("target")): str(row.get("owner"))
        for row in semantic_store.list()
    }
    snapshot_model = Snapshot(
        repository_heads={str(row["identity"]): str(row["head"]) for row in observations},
        active_semantic_claims=semantic_claims,
        active_path_claims={},
        route_status={route.id: "configured" for route in policy.routes},
        observed_at=time.time(),
    )
    snapshot = {
        "schema": "idol.fleet.snapshot.v1",
        "observed_at": snapshot_model.observed_at,
        "mode": policy.mode,
        "repositories": observations,
        "semantic_claims": list(semantic_store.list()),
        "routes": [
            {
                "id": route.id,
                "provider": route.provider,
                "model": route.model,
                "runtime": route.runtime,
                "billing": route.billing.value,
                "proof": route.proof,
                "roles": list(route.roles),
                "max_concurrency": route.max_concurrency,
            }
            for route in policy.routes
        ],
    }
    tasks = load_tasks(tasks_path)
    plan = Scheduler().plan(tasks=tasks, routes=policy.routes, active_attempts=())
    effective_apply = dispatch and policy.mode == "apply" and orders_path is not None and is_enabled(calibration, config_dir)
    plan_payload = {
        "schema": "idol.fleet.plan.v1",
        "observed_at": snapshot_model.observed_at,
        "assignments": [asdict(value) for value in plan.assignments],
        "refusals": [asdict(value) for value in plan.refusals],
        "automatic_dispatch": effective_apply,
    }
    _private_write(state / "snapshots/latest.json", snapshot)
    _private_write(state / "plans/latest.json", plan_payload)
    journal = Journal(state / "events.jsonl")
    journal.append({
        "id": str(uuid.uuid4()),
        "kind": "fleet-observed",
        "at": snapshot_model.observed_at,
        "fact": {
            "repository_count": len(observations),
            "route_count": len(policy.routes),
            "semantic_claim_count": len(semantic_claims),
            "snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        },
    })
    journal.append({
        "id": str(uuid.uuid4()),
        "kind": "fleet-planned",
        "at": time.time(),
        "fact": {
            "assignment_count": len(plan.assignments),
            "refusal_count": len(plan.refusals),
            "automatic_dispatch": effective_apply,
            "plan_sha256": hashlib.sha256(json.dumps(plan_payload, sort_keys=True).encode()).hexdigest(),
        },
    })
    outcomes: list[dict[str, object]] = []
    if dispatch and policy.mode == "apply":
        if orders_path is None:
            raise ValueError("apply mode requires --orders")
        outcomes = _dispatch_assignments(
            policy=policy,
            plan=plan,
            orders_path=orders_path,
            repositories=repositories,
            state=state,
            calibration=calibration,
            config_dir=config_dir,
        )
        _private_write(state / "dispatch/latest.json", {"schema": "idol.fleet.dispatch.v1", "outcomes": outcomes})
    return {"snapshot": snapshot, "plan": plan_payload, "outcomes": outcomes}
