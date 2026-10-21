from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .calibration import apply_calibration, calibrate, load_calibration, require_current_route_proof
from .coordination import acknowledge, delegate, load_order, read_regular, recover_delegation, verify_recipe
from .controller import load_config
from .control import LocalControl
from .manager import ManagedFleetController
from .performance import load_receipt, record_outcome
from .policy import assert_order_route


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, indent=2, default=str)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="idol-fleet", description="Fail-closed IDOL and LIVE fleet controller")
    root.add_argument("--config", required=True, type=Path, help="absolute controller JSON configuration")
    commands = root.add_subparsers(dest="command", required=True)

    once = commands.add_parser("run-once", help="observe, plan, and optionally apply one bounded cycle")
    once.add_argument("--mode", choices=("observe-plan", "apply"), default=None)

    serve = commands.add_parser("serve", help="run the continuous local control loop")
    serve.add_argument("--mode", choices=("observe-plan", "apply"), default=None)

    commands.add_parser("status", help="verify and print private controller state")

    control = commands.add_parser("control", help="inspect or change the additional local dispatch gate")
    operations = control.add_subparsers(dest="operation", required=True)
    operations.add_parser("status", help="read control state without creating or repairing it")
    enable = operations.add_parser("enable", help="enable the local gate for a bounded lease")
    enable.add_argument("--ttl-seconds", required=True, type=int)
    refresh = operations.add_parser("refresh", help="explicitly renew an existing enable lease")
    refresh.add_argument("--ttl-seconds", required=True, type=int)
    operations.add_parser("disable", help="revoke permits and prevent new dispatch boundaries")
    operations.add_parser("drain", help="stop new attempts while preserving already-owned attempts")

    calibration = commands.add_parser("calibrate", help="run no-inference route and containment proofs")
    calibration.add_argument("--ttl-seconds", type=int, default=None)

    outcome = commands.add_parser("record-outcome", help="append one independently reviewed terminal outcome")
    outcome.add_argument("--receipt", required=True, type=Path)

    ack = commands.add_parser("acknowledge", help="explicitly accept one exact assignment and calibrated route")
    ack.add_argument("--work-order", required=True, type=Path)
    ack.add_argument("--recipient", required=True)
    ack.add_argument("--route", required=True)
    ack.add_argument("--ttl-seconds", type=int, default=3600)

    delegation = commands.add_parser("delegate", help="journal parent provenance and publish one exact child order")
    delegation.add_argument("--parent-order", type=Path)
    delegation.add_argument("--child-order", type=Path)
    delegation.add_argument("--owner")
    delegation.add_argument("--recover-event", help="republish the exact child recorded by an interrupted delegation")

    recipe = commands.add_parser("verify-recipe", help="run explicit local controls and retain raw recipe evidence")
    recipe.add_argument("--work-order", required=True, type=Path)
    recipe.add_argument("--spec", required=True, type=Path)

    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "control":
            _, config = load_config(args.config)
            control = LocalControl(config.state_dir / "control")
            operation = getattr(control, args.operation)
            result = operation(ttl_seconds=args.ttl_seconds) if args.operation in {"enable", "refresh"} else operation()
            print(_json(asdict(result)))
            return 0
        if args.command == "calibrate":
            raw, config = load_config(args.config)
            record = calibrate(
                raw_config=raw,
                routes=config.routes,
                output=config.calibration_file,
                ttl_seconds=args.ttl_seconds or config.calibration_ttl_seconds,
            )
            print(_json(asdict(record)))
            return 0

        mode = getattr(args, "mode", None)
        controller = ManagedFleetController(config_path=args.config, mode_override=mode)
        if args.command in {"acknowledge", "verify-recipe", "delegate"}:
            def configured_order(path):
                order = load_order(path)
                if order.repository.resolve() != controller.config.repository.resolve():
                    raise ValueError("work order belongs to a different controller repository")
                return order

            if args.command == "acknowledge":
                order = configured_order(args.work_order)
                routes = apply_calibration(
                    routes=controller.config.routes, record=load_calibration(controller.config.calibration_file),
                    raw_config=controller.raw_config,
                )
                route = next((route for route in routes if route.id == args.route), None)
                if route is None or not route.enabled:
                    raise ValueError("ACK requires an enabled, currently calibrated named route")
                require_current_route_proof(route)
                assert_order_route(order, route)
                row = acknowledge(
                    journal=controller.journal, order=order, route=route,
                    recipient=args.recipient, ttl_seconds=args.ttl_seconds,
                )
            elif args.command == "verify-recipe":
                specification = json.loads(read_regular(args.spec, maximum=1024 * 1024))
                if not isinstance(specification, dict):
                    raise ValueError("recipe specification must be an object")
                row = verify_recipe(
                    journal=controller.journal, control=controller.control,
                    order=configured_order(args.work_order), specification=specification,
                )
            elif args.recover_event:
                if any((args.parent_order, args.child_order, args.owner)):
                    raise ValueError("recover-event cannot change an existing delegation")
                row = recover_delegation(
                    journal=controller.journal, work_orders_dir=controller.config.work_orders_dir,
                    event=args.recover_event,
                )
            else:
                if not all((args.parent_order, args.child_order, args.owner)):
                    raise ValueError("delegate requires parent-order, child-order, and owner")
                row = delegate(
                    journal=controller.journal, work_orders_dir=controller.config.work_orders_dir,
                    parent=configured_order(args.parent_order), child=configured_order(args.child_order), owner=args.owner,
                )
            print(_json(row))
            return 0
        if args.command == "record-outcome":
            receipt = load_receipt(args.receipt)
            row = record_outcome(
                controller.journal,
                receipt,
                route_families={route.id: route.provider_family for route in controller.config.routes},
            )
            print(_json(row))
            return 0
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
