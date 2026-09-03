from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .journal import Journal


class OutcomeRefusal(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OutcomeReceipt:
    schema: str
    attempt_id: str
    order_id: str
    task_id: str
    route_id: str
    verdict: str
    accepted_commit: str | None
    reviewer_families: tuple[str, ...]
    semantic_increment: float
    accepted_tokens: int
    defects: int
    observed_at: float
    evidence: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OutcomeReceipt":
        reviewers = raw.get("reviewer_families")
        if isinstance(reviewers, (str, bytes)) or not isinstance(reviewers, Sequence):
            raise OutcomeRefusal("reviewer_families must be an array")
        receipt = cls(
            schema=str(raw.get("schema", "")),
            attempt_id=str(raw.get("attempt_id", "")).strip(),
            order_id=str(raw.get("order_id", "")).strip(),
            task_id=str(raw.get("task_id", "")).strip(),
            route_id=str(raw.get("route_id", "")).strip(),
            verdict=str(raw.get("verdict", "")).strip().lower(),
            accepted_commit=str(raw.get("accepted_commit", "")).strip() or None,
            reviewer_families=tuple(str(item).strip() for item in reviewers),
            semantic_increment=float(raw.get("semantic_increment", 0)),
            accepted_tokens=int(raw.get("accepted_tokens", 0)),
            defects=int(raw.get("defects", 0)),
            observed_at=float(raw.get("observed_at", 0)),
            evidence=str(raw.get("evidence", "")).strip(),
        )
        if receipt.schema != "idol.fleet.outcome.v1":
            raise OutcomeRefusal("outcome schema mismatch")
        if not all((receipt.attempt_id, receipt.order_id, receipt.task_id, receipt.route_id, receipt.evidence)):
            raise OutcomeRefusal("outcome lacks identity or evidence")
        if receipt.verdict not in {"admitted", "rejected", "reverted"}:
            raise OutcomeRefusal("outcome verdict is not closed")
        if receipt.semantic_increment < 0 or receipt.semantic_increment > 100:
            raise OutcomeRefusal("semantic_increment outside supported bounds")
        if receipt.accepted_tokens < 0 or receipt.defects < 0:
            raise OutcomeRefusal("outcome tokens/defects cannot be negative")
        if receipt.verdict == "admitted":
            if receipt.accepted_commit is None or len(receipt.accepted_commit) != 40:
                raise OutcomeRefusal("admitted outcome lacks exact commit")
            if not receipt.reviewer_families:
                raise OutcomeRefusal("admitted outcome lacks independent reviewer evidence")
            if receipt.semantic_increment <= 0 or receipt.accepted_tokens <= 0:
                raise OutcomeRefusal("admitted outcome lacks measured progress/tokens")
        return receipt


def load_receipt(path: Path) -> OutcomeReceipt:
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutcomeRefusal("outcome receipt is unreadable") from exc
    if not isinstance(raw, Mapping):
        raise OutcomeRefusal("outcome receipt is not an object")
    return OutcomeReceipt.from_mapping(raw)


def record_outcome(
    journal: Journal,
    receipt: OutcomeReceipt,
    *,
    route_families: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    attempt: Mapping[str, Any] | None = None
    implementer_family: str | None = None
    existing_verdict: str | None = None
    for row in journal.events():
        fact = row.get("fact")
        if not isinstance(fact, Mapping):
            continue
        if fact.get("attempt_id") != receipt.attempt_id:
            continue
        if row.get("kind") == "attempt.started":
            attempt = fact
        if row.get("kind") == "attempt.executed":
            implementer_family = str(fact.get("provider_family") or "") or None
        if row.get("kind") in {"attempt.admitted", "attempt.rejected", "attempt.reverted"}:
            existing_verdict = str(row.get("kind")).split(".")[-1]
    if attempt is None:
        raise OutcomeRefusal("outcome references an unknown attempt")
    for key, expected in (
        ("order_id", receipt.order_id),
        ("task_id", receipt.task_id),
        ("route_id", receipt.route_id),
    ):
        if attempt.get(key) != expected:
            raise OutcomeRefusal(f"outcome {key} does not match the attempt")
    if existing_verdict is not None:
        raise OutcomeRefusal(f"attempt already has terminal outcome {existing_verdict}")
    configured_family = (route_families or {}).get(receipt.route_id)
    if implementer_family and configured_family and implementer_family != configured_family:
        raise OutcomeRefusal("executed provider family differs from configured route family")
    implementer_family = implementer_family or configured_family
    if receipt.verdict == "admitted" and implementer_family:
        if all(family == implementer_family for family in receipt.reviewer_families):
            raise OutcomeRefusal("admitted outcome has no independent reviewer family")
    fact = {
        "attempt_id": receipt.attempt_id,
        "order_id": receipt.order_id,
        "task_id": receipt.task_id,
        "route_id": receipt.route_id,
        "accepted_commit": receipt.accepted_commit,
        "reviewer_families": receipt.reviewer_families,
        "semantic_increment": receipt.semantic_increment,
        "accepted_tokens": receipt.accepted_tokens,
        "defects": receipt.defects,
        "evidence": receipt.evidence,
    }
    return journal.append(f"attempt.{receipt.verdict}", fact, at=receipt.observed_at)


def route_factors(journal: Journal) -> Mapping[str, float]:
    totals: dict[str, dict[str, float]] = {}
    for row in journal.events({"attempt.admitted", "attempt.rejected", "attempt.reverted"}):
        fact = row.get("fact")
        if not isinstance(fact, Mapping):
            continue
        route_id = fact.get("route_id")
        if not isinstance(route_id, str):
            continue
        stats = totals.setdefault(
            route_id,
            {"admitted": 0.0, "failed": 0.0, "increment": 0.0, "tokens": 0.0, "defects": 0.0},
        )
        if row.get("kind") == "attempt.admitted":
            stats["admitted"] += 1
            stats["increment"] += float(fact.get("semantic_increment", 0))
            stats["tokens"] += float(fact.get("accepted_tokens", 0))
            stats["defects"] += float(fact.get("defects", 0))
        else:
            stats["failed"] += 1
            if row.get("kind") == "attempt.reverted":
                stats["defects"] += max(1.0, float(fact.get("defects", 0)))
    result: dict[str, float] = {}
    for route_id, stats in totals.items():
        observations = stats["admitted"] + stats["failed"]
        admission_rate = (stats["admitted"] + 1.0) / (observations + 2.0)
        efficiency = 0.0
        if stats["tokens"] > 0:
            efficiency = stats["increment"] * 100_000.0 / stats["tokens"]
        efficiency_factor = 1.0 if efficiency <= 0 else min(1.4, max(0.7, 1.0 + math.log10(efficiency) * 0.12))
        defect_factor = 1.0 / (1.0 + stats["defects"] * 0.2)
        factor = (0.65 + admission_rate * 0.7) * efficiency_factor * defect_factor
        result[route_id] = min(1.6, max(0.4, factor))
    return result
