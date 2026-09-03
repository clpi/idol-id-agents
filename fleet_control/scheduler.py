from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Iterable, Mapping, Sequence

from .model import Assignment, Route, WorkOrder
from .policy import route_verdict


@dataclass(frozen=True, slots=True)
class Rejection:
    order_id: str
    route_id: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan:
    observed_at: float
    base_sha: str
    assignments: tuple[Assignment, ...]
    rejections: tuple[Rejection, ...]


def semantic_overlap(left: str, right: str) -> bool:
    a = tuple(part for part in left.strip("/").split("/") if part)
    b = tuple(part for part in right.strip("/").split("/") if part)
    shortest = min(len(a), len(b))
    return a[:shortest] == b[:shortest]


def path_overlap(left: str, right: str) -> bool:
    a = tuple(part for part in left.strip("/").split("/") if part)
    b = tuple(part for part in right.strip("/").split("/") if part)
    shortest = min(len(a), len(b))
    return a[:shortest] == b[:shortest]


def orders_conflict(left: WorkOrder, right: WorkOrder) -> bool:
    return any(path_overlap(a, b) for a in left.path_claims for b in right.path_claims) or any(
        semantic_overlap(a, b) for a in left.semantic_claims for b in right.semantic_claims
    )


def reset_urgency(route: Route, order: WorkOrder, now: float) -> tuple[float, list[str]]:
    if not route.allowance:
        return 1.0, ["allowance-window-unmeasured"]
    best = 1.0
    notes: list[str] = []
    for window in route.allowance:
        seconds = max(0.0, window.resets_at - now)
        can_finish = order.estimated_seconds <= seconds
        if not can_finish:
            notes.append(f"cannot-finish-before-{window.label}-reset")
            continue
        if seconds <= 900:
            time_factor = 3.0
        elif seconds <= 3600:
            time_factor = 2.3
        elif seconds <= 6 * 3600:
            time_factor = 1.7
        elif seconds <= 24 * 3600:
            time_factor = 1.3
        else:
            time_factor = 1.0
        unused = max(0.0, min(1.0, window.remaining_fraction))
        factor = 1.0 + unused * (time_factor - 1.0)
        best = max(best, factor)
        notes.append(
            f"{window.label}:remaining={unused:.3f}:reset-in={int(seconds)}s:factor={factor:.3f}"
        )
    return best, notes


def score_pair(
    order: WorkOrder,
    route: Route,
    now: float,
    *,
    performance_factor: float = 1.0,
) -> tuple[float, tuple[str, ...]]:
    urgency, notes = reset_urgency(route, order, now)
    risk_weight = {"low": 0.8, "medium": 1.0, "high": 1.25, "critical": 1.5}[order.risk]
    role_weight = {
        "observer": 0.85,
        "counterexample": 1.0,
        "evidence": 1.05,
        "mechanic": 1.0,
        "implementer": 1.2,
        "reviewer": 1.35,
        "architect": 1.5,
        "integrator": 1.2,
    }.get(order.role, 1.0)
    duration_penalty = max(1.0, math.sqrt(order.estimated_seconds / 300.0))
    token_penalty = max(1.0, math.log10(max(order.estimated_tokens, 10)))
    premium_penalty = 1.0
    if route.premium and order.risk in {"low", "medium"} and order.role in {
        "observer",
        "counterexample",
        "evidence",
        "mechanic",
    }:
        premium_penalty = 1.8
        notes.append("premium-capacity-conservation")
    score = (
        float(order.priority)
        * urgency
        * risk_weight
        * role_weight
        * performance_factor
        / duration_penalty
        / token_penalty
        / premium_penalty
    )
    notes.extend(
        (
            f"priority={order.priority}",
            f"risk-weight={risk_weight:.2f}",
            f"role-weight={role_weight:.2f}",
            f"duration-penalty={duration_penalty:.3f}",
            f"token-penalty={token_penalty:.3f}",
            f"performance-factor={performance_factor:.3f}",
        )
    )
    return score, tuple(notes)


def build_plan(
    *,
    base_sha: str,
    orders: Sequence[WorkOrder],
    routes: Sequence[Route],
    active_orders: Iterable[WorkOrder] = (),
    route_active: Mapping[str, int] | None = None,
    route_performance: Mapping[str, float] | None = None,
    max_assignments: int = 1,
    now: float | None = None,
) -> Plan:
    current = time.time() if now is None else now
    counts = dict(route_active or {})
    active = list(active_orders)
    candidates: list[Assignment] = []
    rejected: list[Rejection] = []
    route_by_id = {route.id: route for route in routes}

    for order in sorted(orders, key=lambda item: item.id):
        order_reasons: list[str] = []
        if order.base_sha != base_sha:
            order_reasons.append("stale-base-sha")
        if any(orders_conflict(order, held) for held in active):
            order_reasons.append("active-claim-conflict")
        if order_reasons:
            rejected.append(Rejection(order.id, None, tuple(order_reasons)))
            continue
        produced = False
        for route_id in order.route_ids:
            route = route_by_id.get(route_id)
            if route is None:
                rejected.append(Rejection(order.id, route_id, ("route-missing",)))
                continue
            reasons: list[str] = []
            verdict = route_verdict(route, now=current)
            reasons.extend(verdict.reasons)
            if order.role not in route.roles:
                reasons.append("role-not-admitted")
            if order.reviewer_family and order.role == "reviewer" and route.provider_family == order.reviewer_family:
                reasons.append("reviewer-family-not-independent")
            if counts.get(route.id, 0) >= route.max_parallel:
                reasons.append("route-parallel-capacity-full")
            if route.allowance and all(window.remaining_fraction <= 0 for window in route.allowance):
                reasons.append("allowance-exhausted")
            if route.allowance and all(order.estimated_seconds > max(0, window.resets_at - current) for window in route.allowance):
                reasons.append("cannot-finish-before-any-reset")
            if reasons:
                rejected.append(Rejection(order.id, route.id, tuple(dict.fromkeys(reasons))))
                continue
            performance_factor = max(0.4, min(1.6, (route_performance or {}).get(route.id, 1.0)))
            score, score_notes = score_pair(
                order,
                route,
                current,
                performance_factor=performance_factor,
            )
            candidates.append(Assignment(order=order, route=route, score=score, reason=score_notes))
            produced = True
        if not produced and not any(item.order_id == order.id for item in rejected):
            rejected.append(Rejection(order.id, None, ("no-eligible-route",)))

    chosen: list[Assignment] = []
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.order.id, item.route.id)):
        if len(chosen) >= max_assignments:
            break
        if any(orders_conflict(candidate.order, existing.order) for existing in chosen):
            rejected.append(Rejection(candidate.order.id, candidate.route.id, ("selected-claim-conflict",)))
            continue
        if counts.get(candidate.route.id, 0) >= candidate.route.max_parallel:
            continue
        chosen.append(candidate)
        counts[candidate.route.id] = counts.get(candidate.route.id, 0) + 1

    return Plan(
        observed_at=current,
        base_sha=base_sha,
        assignments=tuple(chosen),
        rejections=tuple(rejected),
    )
