from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .calibration import calibrate
from .controller import FleetController, load_config


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, indent=2, default=str)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="idol-fleet", description="Fail-closed Idol fleet controller")
    root.add_argument("--config", required=True, type=Path, help="absolute controller JSON configuration")
    commands = root.add_subparsers(dest="command", required=True)

    once = commands.add_parser("run-once", help="observe, plan, and optionally apply one bounded cycle")
    once.add_argument("--mode", choices=("observe-plan", "apply"), default=None)

    serve = commands.add_parser("serve", help="run the continuous local control loop")
    serve.add_argument("--mode", choices=("observe-plan", "apply"), default=None)

    commands.add_parser("status", help="verify and print private controller state")

    calibration = commands.add_parser("calibrate", help="run no-inference route and containment proofs")
    calibration.add_argument("--ttl-seconds", type=int, default=3600)

    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "calibrate":
            raw, config = load_config(args.config)
            record = calibrate(
                raw_config=raw,
                routes=config.routes,
                output=config.calibration_file,
                ttl_seconds=args.ttl_seconds,
            )
            print(_json(asdict(record)))
            return 0

        mode = getattr(args, "mode", None)
        controller = FleetController(config_path=args.config, mode_override=mode)
        if args.command == "run-once":
            result = controller.run_once()
            print(_json(asdict(result)))
            return 0
        if args.command == "serve":
            controller.serve()
            return 0
        if args.command == "status":
            print(_json(controller.status()))
            return 0
        raise AssertionError("unreachable command")
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"idol-fleet: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
