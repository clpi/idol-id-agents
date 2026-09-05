from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Any, Mapping, Sequence

from .journal import Journal
from .model import Route


_BACKOFF_SECONDS = (300, 900, 3600, 21600)


@dataclass(frozen=True, slots=True)
class CircuitState:
    route_id: str
    route_subject: str
    consecutive_failures: int
    opened_until: float

    def open(self, now: float) -> bool:
        return self.consecutive_failures > 0 and now < self.opened_until


def circuit_state(journal: Journal, route: Route) -> CircuitState:
    failures = 0
    opened_until = 0.0
    route_subject = route.subject_hash
    for row in journal.events({"route.failed", "route.succeeded"}):
        fact = row.get("fact")
        if not isinstance(fact, Mapping):
            continue
        if fact.get("route_id") != route.id or fact.get("route_subject") != route_subject:
            continue
        if row.get("kind") == "route.succeeded":
            failures = 0
            opened_until = 0.0
            continue
        failures += 1
        delay = _BACKOFF_SECONDS[min(failures - 1, len(_BACKOFF_SECONDS) - 1)]
        opened_until = max(opened_until, float(row.get("at", 0)) + delay)
    return CircuitState(route.id, route_subject, failures, opened_until)


def apply_circuits(
    routes: Sequence[Route],
    journal: Journal,
    *,
    now: float | None = None,
) -> tuple[tuple[Route, ...], tuple[CircuitState, ...]]:
    current = time.time() if now is None else now
    states = tuple(circuit_state(journal, route) for route in routes)
    by_id = {state.route_id: state for state in states}
    available = tuple(
        replace(route, enabled=False)
        if by_id[route.id].open(current)
        else route
        for route in routes
    )
    return available, states


def record_failure(
    journal: Journal,
    route: Route,
    *,
    error_type: str,
    error: str,
    at: float | None = None,
) -> Mapping[str, Any]:
    observed_at = time.time() if at is None else at
    previous = circuit_state(journal, route)
    failures = previous.consecutive_failures + 1
    delay = _BACKOFF_SECONDS[min(failures - 1, len(_BACKOFF_SECONDS) - 1)]
    return journal.append(
        "route.failed",
        {
            "route_id": route.id,
            "route_subject": previous.route_subject,
            "error_type": error_type,
            "error": error,
            "consecutive_failures": failures,
            "retry_after": observed_at + delay,
        },
        at=observed_at,
    )


def record_success(journal: Journal, route: Route, *, at: float | None = None) -> Mapping[str, Any]:
    return journal.append(
        "route.succeeded",
        {"route_id": route.id, "route_subject": route.subject_hash},
        at=at,
    )
