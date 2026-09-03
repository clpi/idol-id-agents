from __future__ import annotations

import time
from typing import Mapping

from .controller import FleetController


_TERMINAL = frozenset({
    "attempt.ready",
    "attempt.refused",
    "attempt.failed",
    "attempt.cancelled",
    "attempt.no-change",
})


def reconcile_expired_attempts(controller: FleetController, *, now: float | None = None) -> tuple[str, ...]:
    """Expire orphaned attempts while preserving history and worktrees.

    A controller crash can leave `attempt.started` as the latest fact. That is
    coordination state, not an immortal lock. Once the configured claim TTL has
    elapsed, append an explicit cancellation; never delete or rewrite history.
    """

    current = time.time() if now is None else now
    latest: dict[str, tuple[str, float, Mapping]] = {}
    for row in controller.journal.events(
        {
            "attempt.started",
            "attempt.ready",
            "attempt.refused",
            "attempt.failed",
            "attempt.cancelled",
            "attempt.no-change",
        }
    ):
        fact = row.get("fact")
        if not isinstance(fact, Mapping):
            continue
        order_id = fact.get("order_id")
        if isinstance(order_id, str):
            latest[order_id] = (str(row.get("kind")), float(row.get("at", 0)), fact)
    expired: list[str] = []
    for order_id, (kind, observed_at, fact) in latest.items():
        if kind in _TERMINAL or kind != "attempt.started":
            continue
        if observed_at + controller.config.claim_ttl_seconds > current:
            continue
        controller.journal.append(
            "attempt.cancelled",
            {
                "order_id": order_id,
                "task_id": fact.get("task_id"),
                "attempt_id": fact.get("attempt_id"),
                "reason": "controller-lease-expired",
                "worktree": fact.get("worktree"),
                "worktree_preserved": True,
            },
            at=current,
        )
        expired.append(order_id)
    return tuple(sorted(expired))
