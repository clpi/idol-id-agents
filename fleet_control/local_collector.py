from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .util import sanitize, utc_now


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "NO_MODEL_INFERENCE": "1"},
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo(path: Path, repo_id: str) -> dict[str, Any] | None:
    if not (path / ".git").exists():
        return None
    rc, head, _ = _run(["git", "rev-parse", "HEAD"], cwd=path)
    if rc != 0:
        return None
    return {
        "id": repo_id,
        "path": str(path.resolve()),
        "head_sha": head.strip(),
        "law_sha256": _sha256(path / "docs/spec/law.md"),
        "constitution_sha256": _sha256(path / "docs/spec/constitution.md"),
        "bootstrap_sha256": _sha256(path / "docs/bootstrap.md"),
    }


def _json_command(candidates: list[list[str]], timeout: int = 45) -> dict[str, Any] | None:
    for command in candidates:
        if shutil.which(command[0]) is None:
            continue
        rc, stdout, _ = _run(command, timeout=timeout)
        if rc != 0:
            continue
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return sanitize(value)
    return None


def _openclaw_rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    encoded = json.dumps(params or {}, separators=(",", ":"))
    return _json_command(
        [
            ["openclaw", "gateway", "call", method, "--json", "--params", encoded],
            ["openclaw", "gateway", "call", method, "--json", encoded],
            ["openclaw", "gateway", "call", method, "--json"],
        ]
    )


def _provider_rows(usage: dict[str, Any] | None) -> list[dict[str, Any]]:
    configured: dict[str, Any] = {}
    path = Path(os.environ.get("IDOL_FLEET_PROVIDER_POLICY", "~/.config/idol/fleet-providers.json")).expanduser()
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                configured = value
        except (OSError, json.JSONDecodeError):
            configured = {}
    rows: list[dict[str, Any]] = []
    source: Any = []
    if isinstance(usage, dict):
        source = usage.get("providers") or usage.get("usage") or usage.get("items") or []
    if isinstance(source, dict):
        source = [{"id": key, **(val if isinstance(val, dict) else {})} for key, val in source.items()]
    for row in source if isinstance(source, list) else []:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get("id") or row.get("provider") or "").strip()
        if not provider_id:
            continue
        policy = configured.get(provider_id, {}) if isinstance(configured, dict) else {}
        rows.append(
            {
                "id": provider_id,
                "family": policy.get("family", provider_id),
                "model": row.get("model") or policy.get("model"),
                "cost_class": policy.get("cost_class", "unknown"),
                "enabled": bool(policy.get("enabled", True)),
                "roles": policy.get("roles", []),
                "quality": policy.get("quality", 0.5),
                "max_concurrency": policy.get("max_concurrency", 1),
                "windows": row.get("windows") or [],
            }
        )
    return rows


def collect() -> dict[str, Any]:
    home = Path.home()
    candidates = [
        (home / "x/idol", "idol"),
        (Path("/Volumes/d 1/x/idol"), "idol"),
        (home / "x/idol-native", "idol-native"),
        (Path("/Volumes/d 1/x/idol-native"), "idol-native"),
    ]
    repositories: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, repo_id in candidates:
        try:
            canonical = str(path.resolve())
        except OSError:
            continue
        if canonical in seen:
            continue
        row = _repo(path, repo_id)
        if row:
            repositories.append(row)
            seen.add(canonical)

    agents_rpc = _openclaw_rpc("agents.list")
    sessions_rpc = _openclaw_rpc("sessions.list", {"limit": 250, "ownerFirst": True})
    usage_rpc = _openclaw_rpc("usage.status")
    models_rpc = _openclaw_rpc("models.list")
    cron_rpc = _openclaw_rpc("cron.list")
    health_rpc = _openclaw_rpc("health")

    agents: list[dict[str, Any]] = []
    for source in (agents_rpc, sessions_rpc):
        if not isinstance(source, dict):
            continue
        rows = source.get("agents") or source.get("sessions") or source.get("items") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            agent_id = str(row.get("id") or row.get("agentId") or row.get("sessionKey") or "").strip()
            if not agent_id:
                continue
            agents.append(
                {
                    "id": agent_id,
                    "provider_id": row.get("provider") or row.get("providerId"),
                    "role": row.get("role") or "unclassified",
                    "status": row.get("status") or "observed",
                    "task_id": row.get("taskId"),
                    "base_sha": row.get("baseSha"),
                    "last_activity_at": row.get("updatedAt") or row.get("lastActivityAt"),
                    "progress": row.get("progress", 0),
                    "claims": {},
                }
            )

    tasks: list[dict[str, Any]] = []
    order_dir = Path(os.environ.get("IDOL_FLEET_WORK_ORDERS", "~/.config/idol/work-orders")).expanduser()
    if order_dir.is_dir():
        for path in sorted(order_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                tasks.append(value)

    return sanitize(
        {
            "schema": "idol.fleet.snapshot.v1",
            "observed_at": utc_now().isoformat(),
            "host": os.uname().nodename,
            "repositories": repositories,
            "providers": _provider_rows(usage_rpc),
            "agents": agents,
            "tasks": tasks,
            "runtime": {
                "openclaw_health": health_rpc,
                "cron": cron_rpc,
                "models_observed": models_rpc is not None,
                "usage_observed": usage_rpc is not None,
            },
        }
    )


def main() -> int:
    json.dump(collect(), sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
