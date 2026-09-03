#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import urllib.request


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--subject", required=True)
    root.add_argument("--provider", required=True)
    root.add_argument("--model", required=True)
    return root


def secret() -> str:
    path = Path.home() / ".hermes/.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GLM_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("Hermes environment has no GLM_API_KEY")


def main() -> int:
    args = parser().parse_args()
    try:
        if args.provider != "zai" or args.model != "glm-5":
            raise RuntimeError("adapter is bound to zai/glm-5")
        request = urllib.request.Request(
            "https://api.z.ai/api/monitor/usage/quota/limit",
            headers={"Authorization": "Bearer " + secret(), "User-Agent": "idol-fleet-usage/1"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.load(response)
        data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else {}
        if raw.get("success") is not True or str(data.get("level", "")).lower() != "max":
            raise RuntimeError("Z.AI account is not a witnessed Max plan")
        windows = []
        for limit in data.get("limits", ()):
            if not isinstance(limit, dict) or limit.get("type") != "TOKENS_LIMIT":
                continue
            percentage = float(limit.get("percentage"))
            reset = float(limit.get("nextResetTime")) / 1000.0
            if not 0 <= percentage <= 100 or reset <= time.time():
                continue
            windows.append(
                {
                    "label": f"tokens-{limit.get('number')}-{limit.get('unit')}",
                    "remaining_fraction": 1.0 - percentage / 100.0,
                    "resets_at": reset,
                }
            )
        if not windows:
            raise RuntimeError("Z.AI returned no live token allowance windows")
        print(
            json.dumps(
                {
                    "schema": "idol.fleet.usage.v1",
                    "route_subject": args.subject,
                    "provider": args.provider,
                    "model": args.model,
                    "billing": "included",
                    "observed_at": time.time(),
                    "windows": windows,
                    "extra_usage_enabled": False,
                    "paygo_enabled": False,
                    "purchased_credits_selected": False,
                    "topup_selected": False,
                    "reset_redeemed": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as exc:
        print(f"Z.AI usage refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
