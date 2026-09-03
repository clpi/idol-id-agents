#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--route", required=True)
    root.add_argument("--subject", required=True)
    root.add_argument("--provider", required=True)
    root.add_argument("--model", required=True)
    return root


def timestamp(value) -> float:
    if value is None:
        raise RuntimeError("allowance window lacks reset time")
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def main() -> int:
    args = parser().parse_args()
    try:
        from agent.account_usage import fetch_account_usage
    except Exception as exc:
        print(f"Hermes account-usage module unavailable: {exc}", file=sys.stderr)
        return 2

    snapshot = fetch_account_usage(args.provider)
    if snapshot is None or not getattr(snapshot, "available", False):
        reason = getattr(snapshot, "unavailable_reason", None) if snapshot is not None else None
        print(f"account usage unavailable: {reason or 'no snapshot'}", file=sys.stderr)
        return 3

    source = str(getattr(snapshot, "source", ""))
    if args.provider == "anthropic" and source != "oauth_usage_api":
        print("Anthropic route is not backed by the OAuth usage authority", file=sys.stderr)
        return 4
    if args.provider == "openai-codex" and source != "usage_api":
        print("Codex route is not backed by the ChatGPT-account usage authority", file=sys.stderr)
        return 4

    windows = []
    for window in getattr(snapshot, "windows", ()):
        used = getattr(window, "used_percent", None)
        reset = getattr(window, "reset_at", None)
        if used is None or reset is None:
            continue
        used_fraction = float(used) / 100.0
        if not 0.0 <= used_fraction <= 1.0:
            raise RuntimeError("provider returned an invalid utilization")
        windows.append(
            {
                "label": str(getattr(window, "label", "window")),
                "remaining_fraction": 1.0 - used_fraction,
                "resets_at": timestamp(reset),
            }
        )
    if not windows:
        print("provider returned no bounded allowance windows", file=sys.stderr)
        return 5

    details = tuple(str(detail) for detail in getattr(snapshot, "details", ()))
    extra_usage = any(detail.lower().startswith("extra usage:") for detail in details)
    # A visible credit balance is treated conservatively as a possible credit
    # fallback. The route remains disabled until host calibration proves its
    # runtime cannot select that balance.
    credit_surface = any("credits balance:" in detail.lower() for detail in details)

    fetched = getattr(snapshot, "fetched_at", datetime.now(timezone.utc))
    observed_at = timestamp(fetched)
    print(
        json.dumps(
            {
                "schema": "idol.fleet.usage.v1",
                "route_subject": args.subject,
                "provider": args.provider,
                "model": args.model,
                "billing": "included",
                "observed_at": observed_at,
                "windows": windows,
                "extra_usage_enabled": extra_usage,
                "paygo_enabled": extra_usage,
                "purchased_credits_selected": credit_surface,
                "topup_selected": False,
                "reset_redeemed": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
