#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fleet_control.controller import FleetController, FleetPolicy, PlanError
from fleet_control.journal import AppendOnlyJournal, project_live
from fleet_control.runtime import ApplyRefused, FleetRuntime, RuntimeConfig


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Idol no-pay-go fleet controller")
    sub = root.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="produce a deterministic no-dispatch plan")
    plan.add_argument("snapshot", type=Path)
    plan.add_argument("--policy", type=Path, required=True)
    tick = sub.add_parser("tick", help="collect, plan, journal, and optionally apply one reconciliation")
    tick.add_argument("--runtime", type=Path, required=True)
    tick.add_argument("--policy", type=Path, required=True)
    tick.add_argument("--apply", action="store_true")
    live = sub.add_parser("project-live", help="materialize Live fleet state from local history")
    live.add_argument("journal", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = FleetController(FleetPolicy.from_dict(_read_json(args.policy))).plan(_read_json(args.snapshot))
        elif args.command == "tick":
            result = FleetRuntime(
                RuntimeConfig.from_dict(_read_json(args.runtime)),
                FleetPolicy.from_dict(_read_json(args.policy)),
            ).tick(apply=args.apply)
        else:
            result = project_live(AppendOnlyJournal(args.journal).read())
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (OSError, ValueError, PlanError, ApplyRefused, RuntimeError) as exc:
        print(f"fleetctl: REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
