from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time
from typing import Sequence

from .calibration import disable, enable
from .cycle import _run_cycle
from .journal import Journal


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="idol-fleet")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("audit", "plan", "run-once", "daemon"):
        command = sub.add_parser(name)
        command.add_argument("--policy", required=True, type=Path)
        command.add_argument("--tasks", required=True, type=Path)
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--repository", action="append", default=[], required=True)
        command.add_argument("--orders", type=Path)
        command.add_argument("--calibration", type=Path)
        command.add_argument("--config-dir", type=Path)
        if name == "daemon":
            command.add_argument("--interval", type=float, default=300.0)

    enable_command = sub.add_parser("enable")
    enable_command.add_argument("--calibration", required=True, type=Path)
    enable_command.add_argument("--state", required=True, type=Path)
    enable_command.add_argument("--config-dir", required=True, type=Path)

    disable_command = sub.add_parser("disable")
    disable_command.add_argument("--config-dir", required=True, type=Path)

    status = sub.add_parser("status")
    status.add_argument("--state", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command in {"audit", "plan", "run-once"}:
            result = _run_cycle(
                args.policy,
                args.tasks,
                args.state,
                args.repository,
                dispatch=(args.command == "run-once"),
                orders_path=args.orders,
                calibration=args.calibration,
                config_dir=args.config_dir,
            )
            selected = result["snapshot"] if args.command == "audit" else result["plan"]
            print(json.dumps(selected, sort_keys=True))
            return 0
        if args.command == "daemon":
            stop = False

            def request_stop(_signum: int, _frame: object) -> None:
                nonlocal stop
                stop = True

            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            while not stop:
                _run_cycle(
                    args.policy,
                    args.tasks,
                    args.state,
                    args.repository,
                    dispatch=True,
                    orders_path=args.orders,
                    calibration=args.calibration,
                    config_dir=args.config_dir,
                )
                deadline = time.monotonic() + max(1.0, args.interval)
                while not stop and time.monotonic() < deadline:
                    time.sleep(min(0.25, deadline - time.monotonic()))
            return 0
        if args.command == "enable":
            enable(args.calibration, args.state, args.config_dir)
            return 0
        if args.command == "disable":
            disable(args.config_dir)
            return 0
        if args.command == "status":
            events = Journal(args.state / "events.jsonl").read()
            status = {
                "schema": "idol.fleet.status.v1",
                "event_count": len(events),
                "last_kind": (events[-1].get("kind") if events else None),
                "snapshot_present": (args.state / "snapshots/latest.json").is_file(),
                "plan_present": (args.state / "plans/latest.json").is_file(),
            }
            print(json.dumps(status, sort_keys=True))
            return 0
        return 2
    except Exception as exc:
        print(f"idol-fleet: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
