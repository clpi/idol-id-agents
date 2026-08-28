from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Iterable, Mapping, Sequence

from .model import BillingClass, RepositoryPath, Route, Task


@dataclass(frozen=True, slots=True)
class Assignment:
    task_id: str
    route_id: str
    score: tuple[int, ...]
    explanation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Refusal:
    task_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan:
    assignments: tuple[Assignment, ...]
    refusals: tuple[Refusal, ...]


def _path_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


class Scheduler:
    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.time

    def plan(
        self,
        *,
        tasks: Sequence[Task],
        routes: Sequence[Route],
        active_attempts: Sequence[Mapping[str, object]],
    ) -> Plan:
        now = self._now()
        route_load: dict[str, int] = {}
        for attempt in active_attempts:
            route_id = attempt.get("route_id")
            if isinstance(route_id, str):
                route_load[route_id] = route_load.get(route_id, 0) + 1
        active_semantic = {
            str(target)
            for attempt in active_attempts
            for target in (attempt.get("semantic_targets") or [])  # type: ignore[union-attr]
        }
        active_paths = {
            str(target)
            for attempt in active_attempts
            for target in (attempt.get("path_targets") or [])  # type: ignore[union-attr]
        }
        assignments: list[Assignment] = []
        refusals: list[Refusal] = []
        reserved_semantic = set(active_semantic)
        reserved_paths = set(active_paths)

        ordered_tasks = sorted(tasks, key=lambda task: (-task.criticality, -task.priority, task.id))
        for task in ordered_tasks:
            reasons: list[str] = []
            if not task.ready:
                reasons.append("task-not-ready")
            if set(task.semantic_targets) & reserved_semantic:
                reasons.append("semantic-overlap")
            if any(
                _path_overlap(str(candidate), active)
                for candidate in task.path_targets
                for active in reserved_paths
            ):
                reasons.append("path-overlap")
            candidates: list[tuple[tuple[int, ...], Route, tuple[str, ...]]] = []
            if not reasons:
                for route in routes:
                    route_reasons: list[str] = []
                    if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
                        route_reasons.append(f"billing-{route.billing.value}")
                    elif not route.billing_proven:
                        route_reasons.append("billing-proof-untrusted")
                    if task.role not in route.roles:
                        route_reasons.append("role-not-supported")
                    if route_load.get(route.id, 0) >= route.max_concurrency:
                        route_reasons.append("route-at-capacity")
                    if route.windows and not any(
                        window.can_finish(now=now, estimated_seconds=task.estimated_seconds)
                        for window in route.windows
                    ):
                        route_reasons.append("allowance-window-insufficient")
                    if route_reasons:
                        continue
                    resident = int(route.id in task.resident_routes)
                    local_match = int(route.billing == BillingClass.LOCAL and task.role in {"observer", "evidence", "mechanic"})
                    premium_needed = int(task.role in {"architect", "reviewer", "implementer"})
                    premium_match = int(route.billing == BillingClass.INCLUDED and premium_needed)
                    reset_pressure = 0
                    for window in route.windows:
                        if window.reset_at is not None:
                            seconds_left = max(0.0, window.reset_at - now)
                            if seconds_left <= 3600:
                                reset_pressure = max(reset_pressure, 2)
                            elif seconds_left <= 86400:
                                reset_pressure = max(reset_pressure, 1)
                    score = (
                        task.criticality,
                        task.priority,
                        resident * 20,
                        local_match * 10,
                        premium_match * 10,
                        reset_pressure,
                        -route_load.get(route.id, 0),
                    )
                    explanation = (
                        f"criticality={task.criticality}",
                        f"priority={task.priority}",
                        f"resident={resident}",
                        f"billing={route.billing.value}",
                        f"reset-pressure={reset_pressure}",
                    )
                    candidates.append((score, route, explanation))
            if reasons:
                refusals.append(Refusal(task.id, tuple(sorted(set(reasons)))))
                continue
            if not candidates:
                refusals.append(Refusal(task.id, ("no-eligible-route",)))
                continue
            score, route, explanation = max(candidates, key=lambda row: (row[0], row[1].id))
            assignments.append(Assignment(task.id, route.id, score, explanation))
            route_load[route.id] = route_load.get(route.id, 0) + 1
            reserved_semantic.update(task.semantic_targets)
            reserved_paths.update(str(path) for path in task.path_targets)
        return Plan(tuple(assignments), tuple(refusals))
