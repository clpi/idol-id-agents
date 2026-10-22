#!/usr/bin/env python3
"""Fail if a signed-up provider has no fleet route, or if a metered one is enabled.

This does not call a provider and it does not enable one. The fleet law still
refuses pay-go, purchased, top-up, and unknown billing on any route that is
turned on. A welcome email is not a session.
"""

from __future__ import annotations

import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    roster = json.loads((ROOT / "config" / "provider-roster.json").read_text())
    fleet = json.loads((ROOT / "config" / "fleet.example.json").read_text())
    routes = {route["id"]: route for route in fleet["routes"]}
    problems: list[str] = []
    for item in roster["providers"]:
        route = routes.get(item["route_id"])
        if route is None:
            problems.append(f"{item['id']}: no route {item['route_id']}")
            continue
        if route.get("provider") != item["id"]:
            problems.append(f"{item['id']}: route provider is {route.get('provider')!r}")
        if route.get("billing") != item["billing"]:
            problems.append(
                f"{item['id']}: billing {route.get('billing')!r} != roster {item['billing']!r}"
            )
        if item["must_stay_disabled"] and route.get("enabled") is True:
            problems.append(f"{item['id']}: enabled without a witnessed allowance")
        if route.get("billing") in {"paygo", "purchased", "topup", "unknown"} and route.get("enabled") is True:
            problems.append(f"{item['id']}: metered or unproven route is enabled")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"provider coverage holds for {len(roster['providers'])} signed-up providers; none are enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
