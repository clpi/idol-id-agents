from __future__ import annotations

from typing import Any


class ProviderPolicyError(RuntimeError):
    pass


_ALLOWED_SOURCE_COSTS = frozenset(
    {"local", "free", "included", "paygo", "purchased_credit"}
)


def merge_provider_policy(
    source: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """Refresh source-owned provider law without retaining injected providers.

    Source owns cost class, family, roles, quality, aliases, and telemetry law.
    The local installation may preserve only:
    - an exact discovered control-agent id;
    - an explicit disable;
    - a concrete local model selection;
    - a lower concurrency ceiling.
    """
    current = existing or {}
    merged: dict[str, Any] = {}
    for provider_id, source_any in source.items():
        if not isinstance(source_any, dict):
            raise ProviderPolicyError(f"provider {provider_id!r} is not an object")
        row = dict(source_any)
        cost = str(row.get("cost_class") or "unknown").lower()
        if cost not in _ALLOWED_SOURCE_COSTS:
            raise ProviderPolicyError(
                f"provider {provider_id!r} has unrecognized source cost class {cost!r}"
            )
        old_any = current.get(provider_id)
        old = old_any if isinstance(old_any, dict) else {}
        control_agent_id = str(old.get("control_agent_id") or "").strip()
        if control_agent_id:
            row["control_agent_id"] = control_agent_id
        if old.get("enabled") is False:
            row["enabled"] = False
        model = str(old.get("model") or "").strip()
        if model:
            row["model"] = model
        try:
            old_concurrency = int(old.get("max_concurrency"))
            source_concurrency = int(row.get("max_concurrency", 1))
        except (TypeError, ValueError):
            pass
        else:
            if old_concurrency >= 0:
                row["max_concurrency"] = min(source_concurrency, old_concurrency)
        merged[str(provider_id)] = row
    return merged
