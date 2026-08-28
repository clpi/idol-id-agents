#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from fleet_control.provider_policy import ProviderPolicyError, merge_provider_policy
from fleet_control.util import atomic_write_json


def load(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProviderPolicyError(f"{path} must contain one JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    try:
        merged = merge_provider_policy(load(args.source), load(args.target.expanduser()))
        atomic_write_json(args.target.expanduser(), merged)
        print(json.dumps({"ok": True, "provider_count": len(merged)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "reason": type(exc).__name__, "detail": str(exc)[:300]},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
