#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
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
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|(?:sk-|gh[pousr]_|github_pat_|tskey-)[A-Za-z0-9_-]+"
    r"|[A-Fa-f0-9]{48,}|[\w-]*(?:api[_-]?key|token|password|secret)[\w-]*[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,\"'&}]+)"
    r"|https?://[^\s/@]+:[^\s/@]+@[^\s]+)"
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
COMMANDS = {
    "python3": (("--version",),),
    "git": (("--version",),),
    "gh": (("--version",),),
    "openclaw": (("--version",), ("version",)),
    "hermes": (("--version",),),
    "codex": (("--version",),),
    "ollama": (("--version",),),
    "tailscale": (("version",), ("--version",)),
    "claude": (("--version",),),
    "kilo": (("--version",),),
    "opencode": (("--version",),),
    "n8n": (("--version",),),
    "cloudflared": (("--version",),),
    "railway": (("--version",),),
    "op": (("--version",),),
    "llama-cli": (("--version",),),
    "llama-server": (("--version",),),
}
MAX_METADATA = 2_000_000
MAX_RECORDS = 512


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Discover IDOL and LIVE fleet host facts without inference")
    root.add_argument("--controller-root", type=Path, default=Path(__file__).resolve().parents[1])
    root.add_argument("--output", type=Path, required=True)
    root.add_argument("--receipt", type=Path, required=True)
    root.add_argument("--repository", type=Path, action="append", default=[],
                      help="Additional existing checkout to observe; repeatable")
    root.add_argument("--storage-path", type=Path, action="append", default=[],
                      help="Existing model directory or mounted drive to measure; never created")
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
        output = result.stdout[:MAX_METADATA]
        truncated = len(result.stdout) > MAX_METADATA
        return {
            "ok": result.returncode == 0 and not truncated,
            "returncode": result.returncode,
            "output": output,
            "output_hash": digest(output),
            "summary": clean(output),
            "truncated": truncated,
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
    origin = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                          r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote["output"].strip()) if remote["ok"] else None
    return {
        "path": str(path.resolve()),
        "head": head["summary"] if head["ok"] else None,
        "branch": branch["summary"] if branch["ok"] else None,
        "tracked_dirty": bool(dirty["output"].strip()) if dirty["ok"] else None,
        "origin_hash": remote["output_hash"] if remote["ok"] else None,
        "origin_repository": origin[1].lower() if origin else None,
    }


def find_repositories(additional: Sequence[Path] = ()) -> list[Mapping[str, Any]]:
    candidates = (
        *additional,
        Path.home() / "work/idol-main",
        Path.home() / "hermes-jobs/idol-live",
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
            resolved = candidate.expanduser().resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            fact = repo_fact(resolved)
        except (OSError, RuntimeError):
            # Broken/symlink-loop/inaccessible candidates do not stop other hosts' facts.
            continue
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
    observation = {"inspection_only": True, "compatibility_verified": False}
    for repository in repositories:
        root = Path(str(repository["path"]))
        command = root / "tools/node/dev/claim"
        try:
            present = command.is_file()
        except (OSError, RuntimeError):
            continue
        if not present:
            continue
        # A repository path or origin is not permission to execute its programs.
        # Even --help can mutate state; discovery only records the file identity.
        fact: dict[str, Any] = {
            **observation,
            "found": True,
            "repository": str(root),
            "sha256": None,
            "help_returncode": None,
            "help_hash": None,
            "verbs": [],
            "help_summary": None,
        }
        try:
            fact["sha256"] = file_sha(command)
        except (OSError, RuntimeError):
            fact["reason"] = "file-identity-unavailable"
        return fact
    return {**observation, "found": False}


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
    # Only the fixed loopback metadata endpoint: no proxy, redirects, auth,
    # model load or inference. A catalog entry does not prove local execution.
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=3)
    try:
        connection.request("GET", "/api/tags", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return {"ok": False, "reason": "local-metadata-http-error", "status": response.status}
        body = response.read(MAX_METADATA + 1)
        if len(body) > MAX_METADATA:
            return {"ok": False, "reason": "local-metadata-too-large"}
        raw = json.loads(body)
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
            raise ValueError("invalid models")
        if len(raw["models"]) > MAX_RECORDS:
            raise ValueError("too many models")
        inventory = []
        for item in raw["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
                raise ValueError("invalid model")
            size, sha = item.get("size"), item.get("digest")
            if type(size) is not int or size < 0 or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("invalid model identity or size")
            details = item.get("details", {})
            if not isinstance(details, dict):
                raise ValueError("invalid model details")
            row = {"name": clean(item["name"], limit=240), "digest": sha, "size_bytes": size}
            for key in ("format", "family", "parameter_size", "quantization_level"):
                value = details.get(key)
                if isinstance(value, str):
                    row[key] = clean(value, limit=240)
            for key in ("modified_at", "remote_model"):
                value = item.get(key)
                if isinstance(value, str):
                    row[key] = clean(value, limit=240)
            inventory.append(row)
        return {"ok": True, "output_hash": hashlib.sha256(body).hexdigest(),
                "models": sorted({row["name"] for row in inventory}), "inventory": inventory,
                "source": "loopback-api-tags", "inference_verified": False}
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        return {"ok": False, "reason": "local-metadata-unavailable-or-invalid"}
    finally:
        connection.close()


def hardware_fact() -> Mapping[str, Any]:
    fact: dict[str, Any] = {"logical_cpus": os.cpu_count(), "memory_bytes": None,
                            "memory_scope": "host-physical"}
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo", encoding="ascii") as handle:
                match = re.search(r"(?m)^MemTotal:\s+(\d+)\s+kB$", handle.read(65536))
            if match:
                fact.update(memory_bytes=int(match[1]) * 1024, memory_source="/proc/meminfo")
            if hasattr(os, "sched_getaffinity"):
                fact["available_cpus"] = len(os.sched_getaffinity(0))
        elif platform.system() == "Darwin":
            probe = run(("sysctl", "-n", "hw.memsize"), timeout=5)
            if probe["ok"] and probe["output"].strip().isdigit():
                fact.update(memory_bytes=int(probe["output"].strip()), memory_source="sysctl hw.memsize")
    except (OSError, ValueError):
        pass
    return fact


def storage_fact(additional: Sequence[Path] = ()) -> list[Mapping[str, Any]]:
    candidates = [Path("/"), Path("/Volumes/d 1"), Path.home() / ".ollama/models",
                  Path.home() / ".cache/huggingface/hub", *additional]
    for name in ("OLLAMA_MODELS", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(name):
            candidates.append(Path(os.environ[name]))
    if os.environ.get("HF_HOME"):
        candidates.append(Path(os.environ["HF_HOME"]) / "hub")
    facts = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.expanduser().absolute()
        if path in seen:
            continue
        seen.add(path)
        fact: dict[str, Any] = {"path": clean(str(path)), "exists": None}
        try:
            path.stat()
            fact["exists"] = True
            usage = shutil.disk_usage(path)
            fact.update(is_mount=os.path.ismount(path), capacity_bytes=usage.total,
                        used_bytes=usage.used, free_bytes=usage.free)
        except FileNotFoundError:
            fact["exists"] = False
        except OSError:
            fact["reason"] = "unavailable"
        facts.append(fact)
    return facts


def hermes_fact() -> Mapping[str, Any]:
    path = Path.home() / ".hermes/config.yaml"
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_METADATA + 1)
        if len(body) > MAX_METADATA:
            return {"ok": False, "reason": "configuration-too-large"}
        import yaml
        config = yaml.safe_load(body)
        if config is None:
            config = {}
        if not isinstance(config, dict):
            return {"ok": False, "reason": "configuration-invalid"}
    except FileNotFoundError:
        return {"ok": False, "reason": "configuration-absent"}
    except (OSError, UnicodeError, ValueError, ImportError):
        return {"ok": False, "reason": "configuration-unavailable"}
    except Exception:
        # YAML diagnostics may quote an entire secret-bearing source line.
        return {"ok": False, "reason": "configuration-invalid"}

    def route(value: Any) -> Mapping[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {key: clean(value[key], limit=240) for key in ("provider", "model", "default", "default_model")
                if isinstance(value.get(key), str)}

    providers = config.get("providers")
    fallback = config.get("fallback_providers")
    auxiliary = config.get("auxiliary")
    mcp = config.get("mcp_servers")
    return {"ok": True, "model": route(config.get("model")),
            "providers": sorted(clean(str(name), limit=240) for name in providers)[:MAX_RECORDS]
            if isinstance(providers, dict) else [],
            "fallback_providers": [route(item) for item in fallback[:MAX_RECORDS]]
            if isinstance(fallback, list) else [],
            "auxiliary": {clean(str(name), limit=240): route(value) for name, value in list(auxiliary.items())[:MAX_RECORDS]}
            if isinstance(auxiliary, dict) else {},
            "delegation": route(config.get("delegation")),
            "mcp_servers": sorted(clean(str(name), limit=240) for name in mcp)[:MAX_RECORDS]
            if isinstance(mcp, dict) else [],
            "credentials_checked": False, "configuration_only": True}


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
    result = run((path, "ping", "--c=1", "--timeout=3s", "r16"), timeout=5)
    status = run((path, "status", "--json"), timeout=10)
    fact: dict[str, Any] = {
        "found": True,
        "r16_reachable": result["ok"],
        "output_hash": result["output_hash"],
        "status_ok": False,
        "status_hash": status["output_hash"],
    }
    try:
        raw = json.loads(status["output"]) if status["ok"] else None
        if (not isinstance(raw, dict) or not isinstance(raw.get("BackendState"), str)
                or "Self" not in raw or "Peer" not in raw):
            raise ValueError("invalid status")
        peers = raw.get("Peer")
        if peers is None:
            peers = {}
        if not isinstance(peers, dict) or len(peers) > MAX_RECORDS:
            raise ValueError("invalid peers")

        def peer(value: Any) -> Mapping[str, Any]:
            if not isinstance(value, dict):
                raise ValueError("invalid peer")
            row: dict[str, Any] = {"online_reported": value.get("Online") if type(value.get("Online")) is bool else None}
            for key in ("ID", "HostName", "DNSName", "OS", "LastSeen"):
                if isinstance(value.get(key), str):
                    row[key] = clean(value[key], limit=240)
            addresses = value.get("TailscaleIPs", [])
            if (not isinstance(addresses, list) or len(addresses) > 16
                    or any(not isinstance(address, str) for address in addresses)):
                raise ValueError("invalid addresses")
            row["addresses"] = [str(ipaddress.ip_address(address)) for address in addresses]
            return row

        fact.update(status_ok=True, backend_state=clean(raw["BackendState"], limit=120),
                    self=peer(raw["Self"]) if raw.get("Self") is not None else None,
                    peers=[peer(value) for value in peers.values()],
                    inventory_source="tailscale-status", compute_authorization="unverified")
    except (UnicodeError, ValueError, TypeError):
        fact["reason"] = "status-unavailable-or-invalid"
    return fact


def main() -> int:
    args = parser().parse_args()
    root = args.controller_root.resolve()
    observed_at = time.time()
    repositories = find_repositories(args.repository)
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
            "hardware": hardware_fact(),
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
        "storage": storage_fact(args.storage_path),
        "harness": {"hermes": hermes_fact()},
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
        "idol_repository": any(row.get("origin_repository") == "clpi/idol" for row in repositories),
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
            "inspection_only": True,
            "compatibility_verified": False,
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
            "model_count": len(full["ollama"].get("models", [])) if full["ollama"].get("ok") else None,
            "models_hash": digest(json.dumps(full["ollama"].get("models", []), sort_keys=True)),
        },
        "launchd": full["launchd"],
        "tailscale": {
            "found": full["tailscale"].get("found", False),
            "r16_reachable": full["tailscale"].get("r16_reachable", False),
            "output_hash": full["tailscale"].get("output_hash"),
            "status_ok": full["tailscale"].get("status_ok", False),
            "peer_count": len(full["tailscale"].get("peers", [])) if full["tailscale"].get("status_ok") else None,
            "status_hash": full["tailscale"].get("status_hash"),
        },
        "hardware": full["host"]["hardware"],
        "storage": [{key: value for key, value in row.items() if key != "path"} | {"path_hash": digest(row["path"])}
                    for row in full["storage"]],
        "harness": {"hermes": {"ok": full["harness"]["hermes"].get("ok", False),
                                 "configuration_only": True}},
        "model_inference": False,
        "paygo_usage": False,
    }
    atomic_json(args.receipt, receipt, 0o644)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
