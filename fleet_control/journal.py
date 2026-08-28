from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .util import canonical_json, sanitize, stable_id, utc_now


class JournalIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class JournalHead:
    sequence: int
    event_id: str | None


class AppendOnlyJournal:
    """Hash-chained local history for the Idol Live operational projection.

    The journal owns collaboration and execution history only. It never owns or
    reconstructs compiler semantics.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise JournalIntegrityError(f"invalid JSON at line {line_number}: {exc}") from exc
                events.append(event)
        self._verify(events)
        return events

    def head(self) -> JournalHead:
        events = self.read()
        if not events:
            return JournalHead(sequence=0, event_id=None)
        return JournalHead(sequence=int(events[-1]["sequence"]), event_id=str(events[-1]["id"]))

    def append(
        self,
        *,
        kind: str,
        subject: str,
        actor: str,
        payload: dict[str, Any],
        authority: dict[str, Any] | None = None,
        accepted: bool = False,
    ) -> dict[str, Any]:
        head = self.head()
        core = {
            "sequence": head.sequence + 1,
            "parent": head.event_id,
            "observed_at": utc_now().isoformat(),
            "actor": actor,
            "kind": kind,
            "subject": subject,
            "payload": sanitize(payload),
            "authority": sanitize(authority or {}),
            "accepted": bool(accepted),
        }
        event = {"id": stable_id("live", core), **core}
        encoded = f"{canonical_json(event)}\n".encode("utf-8")
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    @staticmethod
    def _verify(events: Iterable[dict[str, Any]]) -> None:
        previous: str | None = None
        expected_sequence = 1
        for event in events:
            if event.get("sequence") != expected_sequence:
                raise JournalIntegrityError(
                    f"sequence break: expected {expected_sequence}, got {event.get('sequence')!r}"
                )
            if event.get("parent") != previous:
                raise JournalIntegrityError(
                    f"parent break at sequence {expected_sequence}: expected {previous!r}"
                )
            core = {key: value for key, value in event.items() if key != "id"}
            expected_id = stable_id("live", core)
            if event.get("id") != expected_id:
                raise JournalIntegrityError(
                    f"hash break at sequence {expected_sequence}: expected {expected_id}"
                )
            previous = expected_id
            expected_sequence += 1


def project_live(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Materialize current operational state from immutable history and frontier."""
    agents: dict[str, dict[str, Any]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    accepted_frontier: list[str] = []

    for event in events:
        payload = event.get("payload") or {}
        kind = event.get("kind")
        subject = str(event.get("subject") or "")
        if event.get("accepted"):
            accepted_frontier.append(str(event["id"]))
        if kind in {"agent.observed", "agent.started", "agent.stopped", "agent.suspended"}:
            agents[subject] = {**agents.get(subject, {}), **payload, "last_event": event["id"]}
        elif kind in {"task.observed", "task.blocked", "task.ready", "task.accepted"}:
            tasks[subject] = {**tasks.get(subject, {}), **payload, "last_event": event["id"]}
        elif kind == "provider.observed":
            providers[subject] = {
                **providers.get(subject, {}),
                **payload,
                "last_event": event["id"],
            }

    return {
        "schema": "idol.live.fleet.v1",
        "history_count": len(events),
        "history_head": events[-1]["id"] if events else None,
        "accepted_frontier": accepted_frontier,
        "agents": agents,
        "tasks": tasks,
        "providers": providers,
    }
