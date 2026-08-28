#!/usr/bin/env python3
"""Plan high-value use of model allowances without invoking any model.

The planner is deliberately pure: JSON in, JSON out. It never authenticates to
providers, spends credits, redeems resets, dispatches agents, acquires claims,
or mutates repositories. Its job is to make later operations reviewable and
fail closed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

UTC = dt.timezone.utc
FREE = {"local", "free"}
PURCHASED = {"paygo", "purchased_credit", "topup"}
TRUSTED_TELEMETRY = {"live_provider", "local_telemetry"}


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def strict_bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a JSON boolean")
    return value


def task_tokens(task: dict[str, Any]) -> int:
    explicit = as_int(task.get("estimated_tokens"))
    if explicit > 0:
        return explicit
    total = as_int(task.get("estimated_input_tokens")) + as_int(
        task.get("estimated_output_tokens")
    )
    return total if total > 0 else 0


def provider_family(provider: dict[str, Any]) -> str:
    return str(
        provider.get("family")
        or provider.get("provider")
        or provider.get("id")
        or "unknown"
    )


def normalized_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/")


def paths_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def validate_unique(items: list[dict[str, Any]], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        identity = str(item.get("id") or "")
        if not identity:
            raise ValueError(f"{label} id is required")
        if identity in seen:
            raise ValueError(f"duplicate {label} id: {identity}")
        seen.add(identity)


def validate_payload(payload: dict[str, Any]) -> None:
    if payload.get("schema") != "idol.allowance.input.v1":
        raise ValueError("schema must be idol.allowance.input.v1")
    if not str(payload.get("current_sha") or ""):
        raise ValueError("current_sha is required")
    strict_bool(payload, "paygo_approved", False)

    providers = payload.get("providers")
    tasks = payload.get("tasks")
    if not isinstance(providers, list) or not all(isinstance(item, dict) for item in providers):
        raise ValueError("providers must be a list of objects")
    if not isinstance(tasks, list) or not all(isinstance(item, dict) for item in tasks):
        raise ValueError("tasks must be a list of objects")

    validate_unique(providers, "provider")
    validate_unique(tasks, "task")

    for provider in providers:
        windows = provider.get("windows", [])
        if not isinstance(windows, list) or not all(isinstance(item, dict) for item in windows):
            raise ValueError(f"provider {provider['id']} windows must be a list of objects")
        seen_windows: set[str] = set()
        for window in windows:
            name = str(window.get("name") or "")
            if not name:
                raise ValueError(f"provider {provider['id']} window name is required")
            if name in seen_windows:
                raise ValueError(f"provider {provider['id']} has duplicate window: {name}")
            seen_windows.add(name)


@dataclass
class Window:
    name: str
    remaining_tokens: int
    reset_at: dt.datetime | None
    period_seconds: int | None
    remaining_percent: float | None
    source: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Window":
        remaining_percent = raw.get("remaining_percent")
        period = as_int(raw.get("period_seconds"))
        return cls(
            name=str(raw["name"]),
            remaining_tokens=max(0, as_int(raw.get("remaining_tokens"))),
            reset_at=parse_time(raw.get("reset_at")),
            period_seconds=period if period > 0 else None,
            remaining_percent=(
                clamp(as_float(remaining_percent), 0.0, 100.0)
                if remaining_percent is not None
                else None
            ),
            source=str(raw.get("source") or "unsupported"),
        )

    def seconds_left(self, now: dt.datetime) -> float | None:
        if self.reset_at is None:
            return None
        return max(0.0, (self.reset_at - now).total_seconds())

    def elapsed_fraction(self, now: dt.datetime) -> float | None:
        seconds_left = self.seconds_left(now)
        if seconds_left is None or not self.period_seconds:
            return None
        return clamp(1.0 - seconds_left / self.period_seconds, 0.0, 1.0)

    def urgency(self, now: dt.datetime) -> float:
        """Increase only near reset and only when substantial allowance remains."""
        elapsed = self.elapsed_fraction(now)
        if elapsed is None:
            return 1.0
        final_quarter = clamp((elapsed - 0.75) / 0.25, 0.0, 1.0)
        remaining_fraction = (
            self.remaining_percent / 100.0
            if self.remaining_percent is not None
            else 0.5
        )
        waste_pressure = clamp(remaining_fraction - (1.0 - elapsed), 0.0, 1.0)
        return 1.0 + 1.5 * final_quarter + 1.5 * waste_pressure


@dataclass
class ProviderState:
    raw: dict[str, Any]
    id: str
    family: str
    cost_class: str
    windows: list[Window]
    remaining_by_window: dict[str, int] = field(default_factory=dict)
    assigned: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ProviderState":
        windows = [Window.from_json(item) for item in raw.get("windows", [])]
        state = cls(
            raw=raw,
            id=str(raw["id"]),
            family=provider_family(raw),
            cost_class=str(raw.get("cost_class") or "unknown"),
            windows=windows,
        )
        state.remaining_by_window = {
            window.name: window.remaining_tokens for window in windows
        }
        return state

    def role_fit(self, role: str) -> float:
        quality = self.raw.get("quality") or {}
        if not isinstance(quality, dict):
            return 0.0
        return clamp(
            as_float(quality.get(role), as_float(quality.get("default"), 0.0)),
            0.0,
            1.0,
        )

    def best_window(self, tokens: int, minutes: int, now: dt.datetime) -> Window | None:
        candidates: list[Window] = []
        for window in self.windows:
            remaining = self.remaining_by_window.get(window.name, 0)
            if remaining < tokens:
                continue
            seconds_left = window.seconds_left(now)
            if seconds_left is not None and seconds_left < minutes * 60:
                continue
            candidates.append(window)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: item.reset_at or dt.datetime.max.replace(tzinfo=UTC),
        )

    def telemetry_source(self, window: Window | None) -> str:
        if window is not None:
            return window.source
        return str(self.raw.get("telemetry_source") or "unsupported")


@dataclass
class Candidate:
    task: dict[str, Any]
    provider: ProviderState
    window: Window | None
    score: float
    tokens: int
    minutes: int
    role_fit: float
    urgency: float


def reject_task(task: dict[str, Any], current_sha: str) -> str | None:
    if task.get("state") != "productive_ready":
        return "state-not-productive-ready"
    if task.get("blocked"):
        return "blocked"
    base_sha = str(task.get("base_sha") or "")
    if not base_sha or base_sha != current_sha:
        return "base-sha-mismatch"
    if as_float(task.get("value"), 0.0) <= 0:
        return "nonpositive-value"
    if task_tokens(task) <= 0:
        return "missing-token-estimate"
    if as_int(task.get("estimated_minutes"), 0) <= 0:
        return "missing-time-estimate"
    if not task.get("evidence_path"):
        return "missing-evidence-path"
    if not isinstance(task.get("stop_conditions"), list) or not task["stop_conditions"]:
        return "missing-stop-conditions"
    if not isinstance(task.get("paths"), list) or not task["paths"]:
        return "missing-path-boundary"
    if not isinstance(task.get("semantic_boundaries"), list) or not task["semantic_boundaries"]:
        return "missing-semantic-boundary"
    return None


def provider_rejection(
    task: dict[str, Any],
    provider: ProviderState,
    now: dt.datetime,
    paygo_approved: bool,
) -> str | None:
    if not provider.raw.get("enabled", True):
        return "provider-disabled"
    if not str(provider.raw.get("model") or ""):
        return "missing-exact-model"
    allowed = task.get("allowed_provider_ids")
    if allowed and provider.id not in allowed:
        return "provider-not-allowed"
    denied = set(task.get("denied_provider_ids") or [])
    if provider.id in denied:
        return "provider-denied"
    required_family = task.get("required_provider_family")
    if required_family and provider.family != required_family:
        return "provider-family-mismatch"
    implementer_family = task.get("implementer_family")
    if (
        task.get("requires_different_family")
        and implementer_family
        and provider.family == implementer_family
    ):
        return "reviewer-family-not-independent"
    if provider.cost_class in PURCHASED and not paygo_approved:
        return "paygo-forbidden"
    role = str(task.get("role") or "default")
    if provider.role_fit(role) < as_float(task.get("minimum_role_fit"), 0.55):
        return "role-fit-below-floor"
    tokens = task_tokens(task)
    minutes = as_int(task.get("estimated_minutes"), 1)
    if provider.cost_class not in FREE and provider.best_window(tokens, minutes, now) is None:
        return "no-window-can-finish"
    return None


def candidate_score(
    task: dict[str, Any],
    provider: ProviderState,
    window: Window | None,
    now: dt.datetime,
) -> Candidate:
    tokens = task_tokens(task)
    minutes = as_int(task.get("estimated_minutes"), 1)
    fit = provider.role_fit(str(task.get("role") or "default"))
    completion = clamp(
        as_float(task.get("completion_probability"), 0.8), 0.05, 1.0
    )
    evidence = clamp(as_float(task.get("evidence_factor"), 1.0), 0.1, 1.5)
    urgency = window.urgency(now) if window else 1.0
    priority = clamp(as_float(task.get("priority_weight"), 1.0), 0.1, 5.0)
    premium_penalty = 1.0
    if provider.raw.get("premium") and not task.get("premium_required"):
        premium_penalty = 0.6
    local_bonus = (
        1.15
        if provider.cost_class in FREE and not task.get("premium_required")
        else 1.0
    )
    value = as_float(task.get("value"), 0.0)
    score = (
        value
        * fit
        * completion
        * evidence
        * urgency
        * priority
        * premium_penalty
        * local_bonus
    ) / tokens
    return Candidate(task, provider, window, score, tokens, minutes, fit, urgency)


def conflicts(task: dict[str, Any], selected: list[Candidate]) -> str | None:
    task_paths = [normalized_path(path) for path in task.get("paths") or []]
    task_boundaries = set(task.get("semantic_boundaries") or [])
    task_group = task.get("exclusive_group")
    for item in selected:
        other = item.task
        if task_group and task_group == other.get("exclusive_group"):
            return f"exclusive-group:{task_group}"
        other_paths = [normalized_path(path) for path in other.get("paths") or []]
        if any(paths_overlap(left, right) for left in task_paths for right in other_paths):
            return f"path-conflict:{other.get('id')}"
        if task_boundaries.intersection(other.get("semantic_boundaries") or []):
            return f"semantic-boundary-conflict:{other.get('id')}"
    return None


def assignment_execution_blockers(candidate: Candidate, current_sha: str) -> list[str]:
    blockers: list[str] = []
    source = candidate.provider.telemetry_source(candidate.window)
    if source not in TRUSTED_TELEMETRY:
        blockers.append(f"telemetry-not-live:{source}")
    if candidate.task.get("live_claim_verified") is not True:
        blockers.append("live-claim-unverified")
    if candidate.task.get("work_order_sha") != current_sha:
        blockers.append("work-order-sha-unverified")
    if candidate.provider.raw.get("model_verified") is not True:
        blockers.append("model-identity-unverified")
    return blockers


def plan(payload: dict[str, Any]) -> dict[str, Any]:
    validate_payload(payload)
    now = parse_time(payload.get("observed_at")) or dt.datetime.now(UTC)
    current_sha = str(payload["current_sha"])
    paygo_approved = strict_bool(payload, "paygo_approved", False)
    providers = [ProviderState.from_json(raw) for raw in payload["providers"]]
    tasks = list(payload["tasks"])

    rejected: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    for task in tasks:
        reason = reject_task(task, current_sha)
        if reason:
            rejected.append({"task_id": task.get("id"), "reason": reason})
            continue
        matched = 0
        provider_reasons: dict[str, str] = {}
        for provider in providers:
            reason = provider_rejection(task, provider, now, paygo_approved)
            if reason:
                provider_reasons[provider.id] = reason
                continue
            tokens = task_tokens(task)
            minutes = as_int(task.get("estimated_minutes"), 1)
            window = (
                None
                if provider.cost_class in FREE
                else provider.best_window(tokens, minutes, now)
            )
            candidates.append(candidate_score(task, provider, window, now))
            matched += 1
        if matched == 0:
            rejected.append(
                {
                    "task_id": task.get("id"),
                    "reason": "no-eligible-provider",
                    "provider_reasons": provider_reasons,
                }
            )

    candidates.sort(
        key=lambda item: (
            -item.score,
            item.minutes,
            item.tokens,
            str(item.task.get("id")),
        )
    )
    selected: list[Candidate] = []
    selected_task_ids: set[str] = set()
    provider_parallel: dict[str, int] = {}
    selection_rejections: set[tuple[str, str]] = set()

    for candidate in candidates:
        task_id = str(candidate.task["id"])
        if task_id in selected_task_ids:
            continue
        max_parallel = max(
            1, as_int(candidate.provider.raw.get("max_parallel"), 1)
        )
        if provider_parallel.get(candidate.provider.id, 0) >= max_parallel:
            continue
        reason = conflicts(candidate.task, selected)
        if reason:
            marker = (task_id, reason)
            if marker not in selection_rejections:
                rejected.append({"task_id": task_id, "reason": reason})
                selection_rejections.add(marker)
            continue
        if candidate.window:
            remaining = candidate.provider.remaining_by_window[candidate.window.name]
            if remaining < candidate.tokens:
                continue
            candidate.provider.remaining_by_window[candidate.window.name] = (
                remaining - candidate.tokens
            )
        candidate.provider.assigned.append(task_id)
        provider_parallel[candidate.provider.id] = (
            provider_parallel.get(candidate.provider.id, 0) + 1
        )
        selected.append(candidate)
        selected_task_ids.add(task_id)

    assignments: list[dict[str, Any]] = []
    for order, candidate in enumerate(selected, 1):
        blockers = assignment_execution_blockers(candidate, current_sha)
        assignments.append(
            {
                "order": order,
                "task_id": candidate.task["id"],
                "provider_id": candidate.provider.id,
                "provider_family": candidate.provider.family,
                "model": candidate.provider.raw.get("model"),
                "role": candidate.task.get("role"),
                "estimated_tokens": candidate.tokens,
                "estimated_minutes": candidate.minutes,
                "window": candidate.window.name if candidate.window else None,
                "window_reset_at": (
                    candidate.window.reset_at.isoformat()
                    if candidate.window and candidate.window.reset_at
                    else None
                ),
                "telemetry_source": candidate.provider.telemetry_source(
                    candidate.window
                ),
                "role_fit": round(candidate.role_fit, 4),
                "urgency_multiplier": round(candidate.urgency, 4),
                "score_per_token": round(candidate.score, 10),
                "base_sha": candidate.task.get("base_sha"),
                "evidence_path": candidate.task.get("evidence_path"),
                "review_required": bool(candidate.task.get("review_required")),
                "execution_ready": not blockers,
                "execution_blockers": blockers,
            }
        )

    execution_ready = bool(assignments) and all(
        assignment["execution_ready"] for assignment in assignments
    )
    return {
        "schema": "idol.allowance.plan.v1",
        "observed_at": now.isoformat(),
        "current_sha": current_sha,
        "paygo_approved": paygo_approved,
        "automatic_dispatch": False,
        "execution_ready": execution_ready,
        "assignments": assignments,
        "rejected": rejected,
        "providers": [
            {
                "id": provider.id,
                "family": provider.family,
                "cost_class": provider.cost_class,
                "assigned": provider.assigned,
                "remaining_by_window": provider.remaining_by_window,
            }
            for provider in providers
        ],
        "warnings": [
            "This planner does not invoke models or dispatch work.",
            "Estimated, cached, unsupported, and example telemetry cannot mark an assignment execution-ready.",
            "Every assignment requires a live claim and work order at the exact current SHA.",
            "Purchased/pay-go credits remain forbidden unless paygo_approved is the JSON boolean true.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = plan(payload)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "idol.allowance.plan.v1",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
