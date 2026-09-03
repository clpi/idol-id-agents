#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


SECRET = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9_-]+|[A-Fa-f0-9]{48,}|token[=:]\S+|password[=:]\S+|secret[=:]\S+)"
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CLAIM_VERBS = ("acquire", "renew", "release", "list", "status", "show", "check", "help")
COMMANDS = {
    "python3": (("--version",),),
    "git": (("--version",),),
    "gh": (("--version",),),
    "openclaw": (("--version",), ("version",)),
    "hermes": (("--version",),),
    "codex": (("--version",),),
    "ollama": (("--version",),),
    "tailscale": (("version",), ("--version",)),
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Discover IDOL and LIVE fleet host facts without inference")
    root.add_argument("--controller-root", type=Path, default=Path(__file__).resolve().parents[1])
    root.add_argument("--output", type=Path, required=True)
    root.add_argument("--receipt", type=Path, required=True)
    return root


def clean(text: str, *, limit: int = 1000) -> str:
    value = SECRET.sub("[secret-redacted]", text)
    value = EMAIL.sub("[email-redacted]", value)
    home = str(Path.home())
    if home:
        value = value.replace(home, "~")
    value = " ".join(value.replace("\x00", "").split())
    return value[:limit]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def run(arguments: Sequence[str], *, cwd: Path | None = None, timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env={
                key: os.environ[key]
                for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME")
                if key in os.environ
            }
            | {
                "IDOL_FLEET_NO_MODEL_INFERENCE": "1",
                "IDOL_FLEET_NO_PAYGO": "1",
            },
        )
        output = result.stdout[:2_000_000]
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": output,
            "output_hash": digest(output),
            "summary": clean(output),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "returncode": None,
            "output": "",
            "output_hash": digest(""),
            "summary": f"{type(exc).__name__}: {clean(str(exc))}",
        }


def atomic_json(path: Path, value: Mapping[str, Any], mode: int) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        Path(temporary).unlink(missing_ok=True)


def command_fact(name: str) -> Mapping[str, Any]:
    path = shutil.which(name)
    if not path:
        return {"found": False}
    selected = None
    for suffix in COMMANDS[name]:
        probe = run((path, *suffix), timeout=15)
        if probe["ok"]:
            selected = probe
            break
        if selected is None:
            selected = probe
    assert selected is not None
    return {
        "found": True,
        "path": path,
        "version_ok": selected["ok"],
        "version_hash": selected["output_hash"],
        "version": selected["summary"],
    }


def repo_fact(path: Path) -> Mapping[str, Any] | None:
    if not (path / ".git").exists():
        return None
    head = run(("git", "rev-parse", "HEAD"), cwd=path)
    branch = run(("git", "branch", "--show-current"), cwd=path)
    dirty = run(("git", "status", "--porcelain=v1", "--untracked-files=no"), cwd=path)
    remote = run(("git", "remote", "get-url", "origin"), cwd=path)
    return {
        "path": str(path.resolve()),
        "head": head["summary"] if head["ok"] else None,
        "branch": branch["summary"] if branch["ok"] else None,
        "tracked_dirty": bool(dirty["output"].strip()) if dirty["ok"] else None,
        "origin_hash": remote["output_hash"] if remote["ok"] else None,
    }


def find_repositories() -> list[Mapping[str, Any]]:
    candidates = (
        Path.home() / "x/idol",
        Path("/Volumes/d 1/x/idol"),
        Path("/Volumes/d 1/idol"),
        Path.home() / "idol",
        Path.home() / "x/idol-live",
        Path("/Volumes/d 1/x/idol-live"),
    )
    rows: list[Mapping[str, Any]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        fact = repo_fact(resolved)
        if fact is not None:
            rows.append(fact)
    return rows


def file_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def claim_fact(repositories: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for repository in repositories:
        root = Path(str(repository["path"]))
        command = root / "tools/node/dev/claim"
        if not command.is_file():
            continue
        probes = []
        for arguments in ((str(command), "--help"), (str(command), "help"), (str(command),)):
            result = run(arguments, cwd=root, timeout=20)
            probes.append(result)
            if result["ok"] or result["output"]:
                break
        selected = probes[-1]
        output = selected["output"]
        verbs = [verb for verb in CLAIM_VERBS if re.search(rf"(?<![A-Za-z0-9_-]){verb}(?![A-Za-z0-9_-])", output)]
        return {
            "found": True,
            "repository": str(root),
            "sha256": file_sha(command),
            "help_returncode": selected["returncode"],
            "help_hash": selected["output_hash"],
            "verbs": verbs,
            "help_summary": clean(output, limit=3000),
        }
    return {"found": False}


def json_object(text: str) -> Mapping[str, Any] | None:
    lines = [line for line in text.splitlines() if line.strip()]
    for candidate in (text.strip(), *reversed(lines)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    return None


def adapter_fact(command: Sequence[str]) -> Mapping[str, Any]:
    result = run(command, timeout=45)
    raw = json_object(result["output"]) if result["ok"] else None
    fact: dict[str, Any] = {
        "ok": bool(result["ok"] and raw),
        "returncode": result["returncode"],
        "output_hash": result["output_hash"],
        "summary": result["summary"] if not raw else None,
    }
    if raw:
        fact["schema"] = raw.get("schema")
        if raw.get("schema") == "idol.fleet.inventory.v1":
            sessions = raw.get("sessions") if isinstance(raw.get("sessions"), list) else []
            agents = raw.get("agents") if isinstance(raw.get("agents"), list) else []
            fact["sessions"] = sessions
            fact["agents"] = agents
            fact["session_count"] = len(sessions)
            fact["agent_count"] = len(agents)
        elif raw.get("schema") == "idol.fleet.usage.v1":
            fact["provider"] = raw.get("provider")
            fact["billing"] = raw.get("billing")
            fact["windows"] = raw.get("windows")
            for key in (
                "extra_usage_enabled",
                "paygo_enabled",
                "purchased_credits_selected",
                "topup_selected",
                "reset_redeemed",
            ):
                fact[key] = raw.get(key)
    return fact


def auth_fact(command: Sequence[str], patterns: Sequence[str]) -> Mapping[str, Any]:
    result = run(command, timeout=20)
    output = result["output"]
    matches = [pattern for pattern in patterns if re.search(pattern, output, re.IGNORECASE)]
    return {
        "ok": result["ok"],
        "returncode": result["returncode"],
        "output_hash": result["output_hash"],
        "signals": matches,
    }


def ollama_fact() -> Mapping[str, Any]:
    path = shutil.which("ollama")
    if not path:
        return {"ok": False, "reason": "ollama-not-found"}
    result = run((path, "list"), timeout=30)
    models: list[str] = []
    if result["ok"]:
        for line in result["output"].splitlines()[1:]:
            columns = line.split()
            if columns:
                models.append(columns[0])
    return {
        "ok": result["ok"],
        "output_hash": result["output_hash"],
        "models": sorted(set(models)),
    }


def launchd_fact() -> Mapping[str, Any]:
    if platform.system() != "Darwin":
        return {"supported": False}
    label = f"gui/{os.getuid()}/com.idol.fleet.observe"
    result = run(("launchctl", "print", label), timeout=15)
    return {
        "supported": True,
        "installed": result["ok"],
        "output_hash": result["output_hash"],
    }


def tailscale_fact() -> Mapping[str, Any]:
    path = shutil.which("tailscale")
    if not path:
        app_path = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        path = str(app_path) if app_path.is_file() else None
    if not path:
        return {"found": False}
    result = run((path, "ping", "--timeout=8s", "r16"), timeout=15)
    return {
        "found": True,
        "r16_reachable": result["ok"],
        "output_hash": result["output_hash"],
        "summary": result["summary"],
    }


def main() -> int:
    args = parser().parse_args()
    root = args.controller_root.resolve()
    observed_at = time.time()
    repositories = find_repositories()
    commands = {name: command_fact(name) for name in COMMANDS}

    inventory_script = root / "scripts/openclaw-inventory-adapter.py"
    usage_script = root / "scripts/hermes-usage-adapter.py"
    inventory = adapter_fact((sys.executable, str(inventory_script))) if inventory_script.is_file() else {"ok": False}
    usage: dict[str, Any] = {}
    for provider in ("openai-codex", "anthropic"):
        usage[provider] = adapter_fact(
            (
                sys.executable,
                str(usage_script),
                "--route",
                f"discovery-{provider}",
                "--subject",
                f"discovery-{provider}",
                "--provider",
                provider,
                "--model",
                "discovery-only",
            )
        ) if usage_script.is_file() else {"ok": False}

    auth = {
        "codex": auth_fact(("codex", "login", "status"), (r"chatgpt", r"subscription", r"logged\s+in"))
        if commands["codex"].get("found") else {"ok": False},
        "hermes": auth_fact(("hermes", "auth", "status"), (r"anthropic", r"claude", r"oauth", r"subscription"))
        if commands["hermes"].get("found") else {"ok": False},
    }

    full = {
        "schema": "idol.fleet.host-discovery.v1",
        "observed_at": observed_at,
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "controller": {
            "root": str(root),
            "head": run(("git", "rev-parse", "HEAD"), cwd=root)["summary"],
        },
        "repositories": repositories,
        "commands": commands,
        "claim": claim_fact(repositories),
        "inventory": inventory,
        "usage": usage,
        "auth": auth,
        "ollama": ollama_fact(),
        "launchd": launchd_fact(),
        "tailscale": tailscale_fact(),
        "model_inference": False,
        "paygo_usage": False,
    }
    atomic_json(args.output, full, 0o600)

    receipt = {
        "schema": "idol.fleet.host-discovery-receipt.v1",
        "subject": full["controller"]["head"],
        "observed_at": observed_at,
        "host": {
            "hostname_hash": digest(full["host"]["hostname"]),
            "platform": full["host"]["platform"],
            "machine": full["host"]["machine"],
        },
        "repository_count": len(repositories),
        "idol_repository": any(Path(str(row["path"])).name == "idol" for row in repositories),
        "commands": {
            name: {
                "found": fact.get("found", False),
                "version_ok": fact.get("version_ok", False),
                "version_hash": fact.get("version_hash"),
            }
            for name, fact in commands.items()
        },
        "claim": {
            "found": full["claim"].get("found", False),
            "sha256": full["claim"].get("sha256"),
            "verbs": full["claim"].get("verbs", []),
            "help_hash": full["claim"].get("help_hash"),
        },
        "inventory": {
            "ok": inventory.get("ok", False),
            "session_count": inventory.get("session_count"),
            "agent_count": inventory.get("agent_count"),
            "output_hash": inventory.get("output_hash"),
        },
        "usage": {
            provider: {
                "ok": fact.get("ok", False),
                "billing": fact.get("billing"),
                "window_count": len(fact.get("windows") or []),
                "extra_usage_enabled": fact.get("extra_usage_enabled"),
                "paygo_enabled": fact.get("paygo_enabled"),
                "purchased_credits_selected": fact.get("purchased_credits_selected"),
                "output_hash": fact.get("output_hash"),
            }
            for provider, fact in usage.items()
        },
        "auth": auth,
        "ollama": {
            "ok": full["ollama"].get("ok", False),
            "model_count": len(full["ollama"].get("models", [])),
            "models_hash": digest(json.dumps(full["ollama"].get("models", []), sort_keys=True)),
        },
        "launchd": full["launchd"],
        "tailscale": {
            "found": full["tailscale"].get("found", False),
            "r16_reachable": full["tailscale"].get("r16_reachable", False),
            "output_hash": full["tailscale"].get("output_hash"),
        },
        "model_inference": False,
        "paygo_usage": False,
    }
    atomic_json(args.receipt, receipt, 0o644)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
