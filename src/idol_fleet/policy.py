from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .model import AllowanceWindow, BillingClass, Route


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Policy:
    version: int
    mode: str
    trusted_billing_proofs: frozenset[str]
    routes: tuple[Route, ...]
    limits: dict[str, int]

    @classmethod
    def load(cls, path: Path) -> "Policy":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("policy must be an object")
        version = int(data.get("version", 0))
        if version != 1:
            raise ValueError("unsupported policy version")
        mode = str(data.get("mode", ""))
        if mode not in {"observe-plan", "apply"}:
            raise ValueError("policy mode must be observe-plan or apply")
        trusted = frozenset(str(v) for v in data.get("trusted_billing_proofs", []))
        routes: list[Route] = []
        for row in data.get("routes", []):
            windows = tuple(
                AllowanceWindow(
                    label=str(window["label"]),
                    remaining_fraction=(
                        None
                        if window.get("remaining_fraction") is None
                        else float(window["remaining_fraction"])
                    ),
                    reset_at=(None if window.get("reset_at") is None else float(window["reset_at"])),
                )
                for window in row.get("windows", [])
            )
            routes.append(
                Route(
                    id=str(row["id"]),
                    provider=str(row["provider"]),
                    model=str(row["model"]),
                    runtime=str(row["runtime"]),
                    billing=BillingClass(str(row.get("billing", "unknown"))),
                    proof=str(row.get("proof", "")),
                    roles=tuple(str(v) for v in row.get("roles", [])),
                    max_concurrency=int(row.get("max_concurrency", 1)),
                    windows=windows,
                    fallbacks=tuple(str(v) for v in row.get("fallbacks", [])),
                    provider_family=(str(row["provider_family"]) if row.get("provider_family") else None),
                    config_path=(str(row["config_path"]) if row.get("config_path") else None),
                    billing_proven=(str(row.get("proof", "")) in trusted),
                    executable=(str(row["executable"]) if row.get("executable") else None),
                )
            )
        limits = {str(k): int(v) for k, v in dict(data.get("limits", {})).items()}
        return cls(version=version, mode=mode, trusted_billing_proofs=trusted, routes=tuple(routes), limits=limits)

    def route(self, route_id: str) -> Route | None:
        return next((route for route in self.routes if route.id == route_id), None)

    def route_eligibility(self, route_id: str, role: str) -> Eligibility:
        route = self.route(route_id)
        if route is None:
            return Eligibility(False, ("route-unknown",))
        reasons: list[str] = []
        if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
            reasons.append(f"billing-{route.billing.value}")
        elif route.proof not in self.trusted_billing_proofs:
            reasons.append("billing-proof-untrusted")
        if role not in route.roles:
            reasons.append("role-not-supported")
        if route.fallbacks:
            for fallback_id in route.fallbacks:
                fallback = self.route(fallback_id)
                if fallback is None:
                    reasons.append("fallback-unknown")
                    continue
                if fallback.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
                    reasons.append("fallback-not-included")
                elif fallback.proof not in self.trusted_billing_proofs:
                    reasons.append("fallback-proof-untrusted")
        return Eligibility(not reasons, tuple(sorted(set(reasons))))
