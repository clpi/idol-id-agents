from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable

from .model import BillingClass, Route, WorkOrder


class PolicyRefusal(RuntimeError):
    pass


_ALLOWED_PROOF_KINDS = frozenset({
    "local-process",
    "subscription-oauth",
    "subscription-plan",
    "zero-cost-model",
})


@dataclass(frozen=True, slots=True)
class RouteVerdict:
    allowed: bool
    reasons: tuple[str, ...]


def route_verdict(route: Route, *, now: float | None = None) -> RouteVerdict:
    current = time.time() if now is None else now
    reasons: list[str] = []
    if not route.enabled:
        reasons.append("route-disabled")
    if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
        reasons.append(f"billing-{route.billing.value}-forbidden")
    if route.proof.kind not in _ALLOWED_PROOF_KINDS:
        reasons.append("untrusted-proof-kind")
    if not route.proof.valid(current):
        reasons.append("billing-proof-invalid-or-expired")
    if route.proof.subject_hash != route.subject_hash:
        reasons.append("billing-proof-subject-mismatch")
    return RouteVerdict(allowed=not reasons, reasons=tuple(reasons))


def assert_route_allowed(route: Route, *, now: float | None = None) -> None:
    verdict = route_verdict(route, now=now)
    if not verdict.allowed:
        raise PolicyRefusal(f"route {route.id} refused: {', '.join(verdict.reasons)}")


def assert_order_route(order: WorkOrder, route: Route, *, now: float | None = None) -> None:
    assert_route_allowed(route, now=now)
    if route.id not in order.route_ids:
        raise PolicyRefusal(f"route {route.id} is not listed by work order {order.id}")
    if order.role not in route.roles:
        raise PolicyRefusal(f"route {route.id} does not admit role {order.role}")
    if order.reviewer_family and order.role == "reviewer" and route.provider_family == order.reviewer_family:
        raise PolicyRefusal("reviewer provider family matches the implementer family")


def assert_zero_paygo(routes: Iterable[Route], *, now: float | None = None) -> None:
    for route in routes:
        if route.enabled and route.billing in {
            BillingClass.PAYGO,
            BillingClass.PURCHASED,
            BillingClass.TOPUP,
            BillingClass.UNKNOWN,
        }:
            verdict = route_verdict(route, now=now)
            if verdict.allowed:
                raise AssertionError("forbidden billing route was admitted")
