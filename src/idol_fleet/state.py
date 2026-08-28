from __future__ import annotations

from enum import Enum
import time
import uuid
from typing import Any, Mapping

from .journal import Journal


class AttemptState(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    CLAIMED = "claimed"
    RUNNING = "running"
    EVIDENCE = "evidence"
    REVIEW = "review"
    READY = "ready"
    HELD = "held"
    FAILED = "failed"
    STALE = "stale"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


_ALLOWED: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.PROPOSED: frozenset({AttemptState.VALIDATED, AttemptState.HELD, AttemptState.REJECTED}),
    AttemptState.VALIDATED: frozenset({AttemptState.CLAIMED, AttemptState.HELD, AttemptState.STALE}),
    AttemptState.CLAIMED: frozenset({AttemptState.RUNNING, AttemptState.HELD, AttemptState.STALE, AttemptState.FAILED}),
    AttemptState.RUNNING: frozenset({AttemptState.EVIDENCE, AttemptState.FAILED, AttemptState.STALE, AttemptState.HELD}),
    AttemptState.EVIDENCE: frozenset({AttemptState.REVIEW, AttemptState.READY, AttemptState.REJECTED, AttemptState.HELD}),
    AttemptState.REVIEW: frozenset({AttemptState.READY, AttemptState.REJECTED, AttemptState.HELD}),
    AttemptState.READY: frozenset({AttemptState.SUPERSEDED}),
    AttemptState.HELD: frozenset({AttemptState.VALIDATED, AttemptState.REJECTED, AttemptState.SUPERSEDED}),
    AttemptState.FAILED: frozenset({AttemptState.SUPERSEDED}),
    AttemptState.STALE: frozenset({AttemptState.SUPERSEDED}),
    AttemptState.REJECTED: frozenset({AttemptState.SUPERSEDED}),
    AttemptState.SUPERSEDED: frozenset(),
}


class Coordinator:
    def __init__(self, *, journal: Journal, mode: str) -> None:
        if mode not in {"observe-plan", "apply"}:
            raise ValueError("invalid coordinator mode")
        self.journal = journal
        self.mode = mode

    def assert_dispatch_allowed(self) -> None:
        if self.mode != "apply":
            raise PermissionError("fleet is in observe-plan mode")

    def transition(
        self,
        attempt_id: str,
        current: AttemptState,
        target: AttemptState,
        facts: Mapping[str, Any],
    ) -> None:
        if target not in _ALLOWED[current]:
            raise ValueError(f"illegal attempt transition: {current.value} -> {target.value}")
        event = {
            "id": str(uuid.uuid4()),
            "kind": "attempt-transition",
            "at": time.time(),
            "fact": {
                "attempt": attempt_id,
                "from": current.value,
                "to": target.value,
                **dict(facts),
            },
        }
        self.journal.append(event)

