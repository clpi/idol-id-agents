#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from fleet_control.util import atomic_write_json, utc_now


class RotationRefused(RuntimeError):
    pass


def run(command: list[str], timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "NO_MODEL_INFERENCE": "1"},
    )


def rpc_health() -> bool:
    encoded = "{}"
    for command in (
        ["openclaw", "gateway", "call", "health", "--json", "--params", encoded],
        ["openclaw", "gateway", "call", "health", "--json", encoded],
        ["openclaw", "gateway", "call", "health", "--json"],
    ):
        proc = run(command, timeout=20)
        if proc.returncode != 0:
            continue
        try:
            value: Any = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sentinel", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        openclaw = shutil.which("openclaw")
        if not openclaw:
            raise RotationRefused("openclaw executable is absent")
        if args.sentinel.is_file() and not args.force:
            existing = json.loads(args.sentinel.read_text(encoding="utf-8"))
            if existing.get("schema") == "idol.openclaw.auth-rotation.v1":
                print(json.dumps({"ok": True, "already_rotated": True, "rotated_at": existing.get("rotated_at")}))
                return 0

        config_help = run([openclaw, "config", "--help"])
        gateway_help = run([openclaw, "gateway", "--help"])
        if config_help.returncode != 0 or "set" not in (config_help.stdout + config_help.stderr):
            raise RotationRefused("openclaw config set is not an installed capability")
        if gateway_help.returncode != 0 or "restart" not in (gateway_help.stdout + gateway_help.stderr):
            raise RotationRefused("openclaw gateway restart is not an installed capability")

        new_token = secrets.token_hex(32)
        attempts = (
            [openclaw, "config", "set", "gateway.auth.token", new_token],
            [openclaw, "config", "set", "gateway.auth.token", json.dumps(new_token), "--json"],
        )
        configured = False
        for command in attempts:
            proc = run(command)
            if proc.returncode == 0:
                configured = True
                break
        if not configured:
            raise RotationRefused("gateway auth token could not be changed through the installed config interface")

        restart = run([openclaw, "gateway", "restart"], timeout=90)
        if restart.returncode != 0:
            raise RotationRefused("gateway restart failed after auth rotation")
        healthy = False
        for _ in range(30):
            time.sleep(2)
            if rpc_health():
                healthy = True
                break
        if not healthy:
            raise RotationRefused("rotated gateway did not recover local authenticated health")

        record = {
            "schema": "idol.openclaw.auth-rotation.v1",
            "rotated_at": utc_now().isoformat(),
            "token_fingerprint": hashlib.sha256(new_token.encode("utf-8")).hexdigest()[:16],
            "gateway_health_after_rotation": True,
        }
        atomic_write_json(args.sentinel.expanduser(), record)
        print(json.dumps({"ok": True, "rotated_at": record["rotated_at"], "gateway_health": True}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": type(exc).__name__, "detail": str(exc)[:300]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
