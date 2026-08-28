#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from fleet_control.util import atomic_write_json, utc_now


class InstallRefused(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={
            **os.environ,
            "NO_MODEL_INFERENCE": "1",
            "NO_PAYG_USAGE": "1",
            "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY",
        },
    )


def require(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    proc = run(command, cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        raise InstallRefused(f"command failed: {Path(command[0]).name} {command[1:3]}")
    return proc


def copy_tree(source: Path, target: Path) -> None:
    staging = target.with_name(f".{target.name}.staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name in ("fleet_control", "scripts"):
        shutil.copytree(source / name, staging / name)
    shutil.copy2(source / "pyproject.toml", staging / "pyproject.toml")
    if target.exists():
        backup = target.with_name(f".{target.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        target.replace(backup)
    staging.replace(target)


def launchd_plist(
    *,
    install_root: Path,
    config_dir: Path,
    state_dir: Path,
) -> dict[str, Any]:
    python = sys.executable
    fleetctl = install_root / "scripts/fleetctl.py"
    path = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    return {
        "Label": "com.idol.fleet-coordinator",
        "ProgramArguments": [
            python,
            str(fleetctl),
            "tick",
            "--runtime",
            str(config_dir / "fleet-runtime.json"),
            "--policy",
            str(config_dir / "fleet-policy.json"),
            "--apply",
        ],
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": path,
            "PYTHONPATH": str(install_root),
            "IDOL_FLEET_APPLY": "1",
            "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY",
            "IDOL_FLEET_STATE_DIR": str(state_dir),
            "IDOL_FLEET_PROVIDER_POLICY": str(config_dir / "fleet-providers.json"),
            "IDOL_FLEET_TASK_ROSTER": str(config_dir / "task-roster.json"),
            "IDOL_FLEET_ADAPTER_CONFIG": str(config_dir / "fleet-adapter.json"),
        },
        "StartInterval": 300,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(state_dir / "fleet.log"),
        "StandardErrorPath": str(state_dir / "fleet.err.log"),
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallRefused(f"{path} is not a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    repo = args.repo.resolve()
    home = Path.home()
    install_root = home / ".local/lib/idol-fleet"
    config_dir = home / ".config/idol"
    state_dir = home / ".local/state/idol-fleet"
    launch_agents = home / "Library/LaunchAgents"
    plist_path = launch_agents / "com.idol.fleet-coordinator.plist"
    sentinel = state_dir / "openclaw-auth-rotated.json"
    report_path = state_dir / "adapter-probe.json"

    try:
        if not (repo / "tools/node/dev/claim").is_file():
            raise InstallRefused("Idol canonical claim tool is absent")
        require([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=source)
        require([sys.executable, "-m", "py_compile", *[str(p) for p in sorted((source / "fleet_control").glob("*.py"))], *[str(p) for p in sorted((source / "scripts").glob("*.py"))]], cwd=source)

        copy_tree(source, install_root)
        config_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "config/fleet-policy.json", config_dir / "fleet-policy.json")
        shutil.copy2(source / "config/task-roster.json", config_dir / "task-roster.json")
        if not (config_dir / "fleet-providers.json").is_file():
            shutil.copy2(source / "config/providers.production.json", config_dir / "fleet-providers.json")

        rotation = run(
            [
                sys.executable,
                str(install_root / "scripts/rotate_openclaw_auth.py"),
                "--sentinel",
                str(sentinel),
            ],
            cwd=install_root,
            timeout=180,
        )
        rotation_ok = rotation.returncode == 0 and sentinel.is_file()

        probe = run(
            [
                sys.executable,
                str(install_root / "scripts/probe_local_adapter.py"),
                "--repo", str(repo),
                "--install-root", str(install_root),
                "--state-dir", str(state_dir),
                "--provider-policy", str(config_dir / "fleet-providers.json"),
                "--provider-output", str(config_dir / "fleet-providers.json"),
                "--adapter-output", str(config_dir / "fleet-adapter.json"),
                "--report-output", str(report_path),
            ],
            cwd=install_root,
            timeout=180,
        )
        probe_report = load_json(report_path) if report_path.is_file() else {"ready": False}
        adapter_ready = probe.returncode == 0 and probe_report.get("ready") is True
        apply_enabled = bool(args.enable and rotation_ok and adapter_ready)

        adapter = load_json(config_dir / "fleet-adapter.json") if (config_dir / "fleet-adapter.json").is_file() else {}
        adapter.update(
            {
                "state_dir": str(state_dir),
                "runner_path": str(install_root / "scripts/run_agent.py"),
                "adapter_config_path": str(config_dir / "fleet-adapter.json"),
            }
        )
        atomic_write_json(config_dir / "fleet-adapter.json", adapter)
        action = str(install_root / "scripts/local_action_adapter.py")
        runtime = {
            "state_dir": str(state_dir),
            "actor": "idol-fleet-controller",
            "apply_enabled": apply_enabled,
            "snapshot_command": [sys.executable, str(install_root / "scripts/collect_local.py")],
            "action_commands": {
                "claim.acquire": [sys.executable, action, "claim-acquire", "{action_file}", "--config", str(config_dir / "fleet-adapter.json")],
                "claim.release": [sys.executable, action, "claim-release", "{action_file}", "--config", str(config_dir / "fleet-adapter.json")],
                "agent.start": [sys.executable, action, "start", "{action_file}", "--config", str(config_dir / "fleet-adapter.json")],
                "agent.checkpoint": [sys.executable, action, "checkpoint", "{action_file}", "--config", str(config_dir / "fleet-adapter.json")],
                "agent.suspend": [sys.executable, action, "suspend", "{action_file}", "--config", str(config_dir / "fleet-adapter.json")],
                "agent.stop": [sys.executable, action, "stop", "{action_file}", "--config", str(config_dir / "fleet-adapter.json")]
            },
        }
        atomic_write_json(config_dir / "fleet-runtime.json", runtime)

        env = {
            **os.environ,
            "PYTHONPATH": str(install_root),
            "IDOL_FLEET_PROVIDER_POLICY": str(config_dir / "fleet-providers.json"),
            "IDOL_FLEET_TASK_ROSTER": str(config_dir / "task-roster.json"),
            "IDOL_FLEET_STATE_DIR": str(state_dir),
            "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY",
        }
        dry = subprocess.run(
            [
                sys.executable,
                str(install_root / "scripts/fleetctl.py"),
                "tick",
                "--runtime", str(config_dir / "fleet-runtime.json"),
                "--policy", str(config_dir / "fleet-policy.json"),
            ],
            cwd=install_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if dry.returncode != 0:
            apply_enabled = False
            runtime["apply_enabled"] = False
            atomic_write_json(config_dir / "fleet-runtime.json", runtime)

        launch_agents.mkdir(parents=True, exist_ok=True)
        if apply_enabled:
            with plist_path.open("wb") as handle:
                plistlib.dump(launchd_plist(install_root=install_root, config_dir=config_dir, state_dir=state_dir), handle)
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], capture_output=True)
            require(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], timeout=60)
            require(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.idol.fleet-coordinator"], timeout=60)
        else:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], capture_output=True)

        report = {
            "schema": "idol.fleet.install.v1",
            "installed_at": utc_now().isoformat(),
            "tests_passed": True,
            "credential_rotation": rotation_ok,
            "adapter_ready": adapter_ready,
            "dry_run_passed": dry.returncode == 0,
            "apply_enabled": apply_enabled,
            "continuous_service_loaded": apply_enabled,
            "no_payg_usage": True,
            "automatic_merge": False,
            "probe": probe_report,
        }
        atomic_write_json(state_dir / "install-report.json", report)
        print(json.dumps(report, sort_keys=True))
        return 0 if (not args.enable or apply_enabled) else 2
    except Exception as exc:
        report = {
            "schema": "idol.fleet.install.v1",
            "installed_at": utc_now().isoformat(),
            "apply_enabled": False,
            "continuous_service_loaded": False,
            "reason": type(exc).__name__,
            "detail": str(exc)[:300],
        }
        state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_dir / "install-report.json", report)
        print(json.dumps(report, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
