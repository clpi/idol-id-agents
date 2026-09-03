from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
import subprocess
import time
from typing import Any, Mapping, Sequence

from .model import AllowanceWindow, BillingClass, Route


class UsageRefusal(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UsageObservation:
    schema: str
    route_subject: str
    provider: str
    model: str
    billing: str
    observed_at: float
    windows: tuple[AllowanceWindow, ...]
    extra_usage_enabled: bool
    paygo_enabled: bool
    purchased_credits_selected: bool
    topup_selected: bool
    reset_redeemed: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UsageObservation":
        windows_raw = raw.get("windows")
        if isinstance(windows_raw, (str, bytes)) or not isinstance(windows_raw, Sequence):
            raise UsageRefusal("usage windows must be an array")
        windows = tuple(
            AllowanceWindow.from_mapping(item)
            for item in windows_raw
            if isinstance(item, Mapping)
        )
        if len(windows) != len(windows_raw):
            raise UsageRefusal("usage windows contain a non-object")
        return cls(
            schema=str(raw.get("schema", "")),
            route_subject=str(raw.get("route_subject", "")),
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
            billing=str(raw.get("billing", "")),
            observed_at=float(raw.get("observed_at", 0)),
            windows=windows,
            extra_usage_enabled=raw.get("extra_usage_enabled") is True,
            paygo_enabled=raw.get("paygo_enabled") is True,
            purchased_credits_selected=raw.get("purchased_credits_selected") is True,
            topup_selected=raw.get("topup_selected") is True,
            reset_redeemed=raw.get("reset_redeemed") is True,
        )


_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME")


def _environment(route: Route) -> dict[str, str]:
    env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    for name in route.usage_auth_env:
        value = os.environ.get(name)
        if value is None:
            raise UsageRefusal(f"usage adapter requires absent environment {name}")
        env[name] = value
    env["IDOL_FLEET_NO_MODEL_INFERENCE"] = "1"
    env["IDOL_FLEET_NO_PAYGO"] = "1"
    return env


def _parse_json(text: str) -> Mapping[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    candidates = [text.strip(), *reversed(lines)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    raise UsageRefusal("usage adapter produced no JSON object")


def observe_usage(route: Route, *, now: float | None = None) -> UsageObservation:
    if not route.usage_command:
        raise UsageRefusal("route has no usage adapter")
    current = time.time() if now is None else now
    values = {
        "route": route.id,
        "route_subject": route.subject_hash,
        "provider": route.provider,
        "model": route.model,
    }
    try:
        command = [part.format_map(values) for part in route.usage_command]
    except KeyError as exc:
        raise UsageRefusal(f"usage adapter references unknown placeholder {exc.args[0]}") from exc
    try:
        result = subprocess.run(
            command,
            env=_environment(route),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=route.usage_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageRefusal("usage adapter did not complete") from exc
    if result.returncode != 0:
        raise UsageRefusal(f"usage adapter returned {result.returncode}")
    observation = UsageObservation.from_mapping(_parse_json(result.stdout[:1_000_000]))
    if observation.schema != "idol.fleet.usage.v1":
        raise UsageRefusal("usage adapter schema mismatch")
    if observation.route_subject != route.subject_hash:
        raise UsageRefusal("usage observation belongs to another route configuration")
    if observation.provider != route.provider or observation.model != route.model:
        raise UsageRefusal("usage observation provider/model mismatch")
    if observation.billing != route.billing.value:
        raise UsageRefusal("usage observation billing class mismatch")
    if observation.observed_at > current + 60:
        raise UsageRefusal("usage observation is from the future")
    if current - observation.observed_at > route.usage_max_age_seconds:
        raise UsageRefusal("usage observation is stale")
    if any(
        (
            observation.extra_usage_enabled,
            observation.paygo_enabled,
            observation.purchased_credits_selected,
            observation.topup_selected,
            observation.reset_redeemed,
        )
    ):
        raise UsageRefusal("usage observation reports a forbidden spending or reset surface")
    if route.billing is BillingClass.INCLUDED and not observation.windows:
        raise UsageRefusal("included route produced no allowance windows")
    return observation


def refresh_routes(
    routes: Sequence[Route],
    *,
    now: float | None = None,
) -> tuple[tuple[Route, ...], tuple[Mapping[str, Any], ...]]:
    current = time.time() if now is None else now
    refreshed: list[Route] = []
    facts: list[Mapping[str, Any]] = []
    for route in routes:
        if not route.enabled or not route.usage_command:
            refreshed.append(route)
            continue
        try:
            observation = observe_usage(route, now=current)
            refreshed.append(replace(route, allowance=observation.windows))
            facts.append({"route_id": route.id, "ok": True, "observation": asdict(observation)})
        except Exception as exc:
            refreshed.append(replace(route, enabled=False) if route.usage_required else route)
            facts.append(
                {
                    "route_id": route.id,
                    "ok": False,
                    "required": route.usage_required,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return tuple(refreshed), tuple(facts)
