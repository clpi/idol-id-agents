from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Mapping

from .model import BillingClass, Route, WorkOrder
from .process import run_command


class RuntimePolicyViolation(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunResult:
    status: str
    ok: bool
    provider: str | None
    model: str | None
    usage_input: int | None
    usage_output: int | None
    usage_total: int | None
    cost_usd: float | None
    session_hash: str | None
    tool_calls: int | None
    returncode: int | None
    timed_out: bool
    stdout_sha256: str
    stderr_sha256: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _parse_json_stdout(stdout: str) -> dict[str, object]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimePolicyViolation("runtime did not return the required JSON envelope") from exc
    if not isinstance(data, dict):
        raise RuntimePolicyViolation("runtime JSON envelope is not an object")
    return data


def _result(data: dict[str, object], *, returncode: int | None, timed_out: bool, stdout: str, stderr: str) -> RunResult:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    assert isinstance(usage, dict)
    tool = data.get("toolSummary") if isinstance(data.get("toolSummary"), dict) else {}
    assert isinstance(tool, dict)
    session = data.get("sessionId")
    return RunResult(
        status=str(data.get("status", "error")),
        ok=bool(data.get("ok", returncode == 0 and not timed_out)),
        provider=(str(data["provider"]) if data.get("provider") is not None else None),
        model=(str(data["model"]) if data.get("model") is not None else None),
        usage_input=(int(usage["input"]) if isinstance(usage.get("input"), (int, float)) else None),
        usage_output=(int(usage["output"]) if isinstance(usage.get("output"), (int, float)) else None),
        usage_total=(int(usage["total"]) if isinstance(usage.get("total"), (int, float)) else None),
        cost_usd=(float(data["costUsd"]) if isinstance(data.get("costUsd"), (int, float)) else None),
        session_hash=(_hash(str(session))[:16] if session else None),
        tool_calls=(int(tool["calls"]) if isinstance(tool.get("calls"), (int, float)) else None),
        returncode=returncode,
        timed_out=timed_out,
        stdout_sha256=_hash(stdout),
        stderr_sha256=_hash(stderr),
    )


def _enforce_route(route: Route, result: RunResult) -> None:
    if result.provider is not None and result.provider != route.provider:
        raise RuntimePolicyViolation(f"runtime provider {result.provider!r} differs from route {route.provider!r}")
    if route.billing in {BillingClass.LOCAL, BillingClass.INCLUDED} and result.cost_usd is not None and result.cost_usd > 0:
        raise RuntimePolicyViolation("included/local route reported positive metered cost")


class OpenClawRuntime:
    def __init__(self, *, executable: str = "openclaw") -> None:
        self.executable = executable

    def execute(
        self,
        order: WorkOrder,
        route: Route,
        prompt_path: Path,
        cwd: Path,
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> RunResult:
        if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
            raise RuntimePolicyViolation("runtime route is not local or included")
        if not route.billing_proven:
            raise RuntimePolicyViolation("runtime route billing proof is untrusted")
        if not route.config_path:
            raise RuntimePolicyViolation("OpenClaw route requires a pinned config path")
        config = Path(route.config_path)
        if not config.is_file():
            raise RuntimePolicyViolation("OpenClaw route config is absent")
        argv = [
            self.executable,
            "agent",
            "exec",
            "--message-file",
            str(prompt_path),
            "--cwd",
            str(cwd),
            "--config",
            str(config),
            "--model",
            route.model,
        ]
        for fallback in route.fallbacks:
            argv.extend(["--fallback", fallback])
        argv.extend(["--timeout", str(order.estimated_seconds), "--json"])
        command = run_command(argv, cwd=cwd, timeout=order.estimated_seconds + 30, env=extra_env)
        data = _parse_json_stdout(command.stdout)
        result = _result(data, returncode=command.returncode, timed_out=command.timed_out, stdout=command.stdout, stderr=command.stderr)
        _enforce_route(route, result)
        return result


class HermesRuntime:
    def __init__(self, *, executable: str = "hermes") -> None:
        self.executable = executable

    def execute(
        self,
        order: WorkOrder,
        route: Route,
        prompt_path: Path,
        cwd: Path,
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> RunResult:
        if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
            raise RuntimePolicyViolation("runtime route is not local or included")
        if not route.billing_proven:
            raise RuntimePolicyViolation("runtime route billing proof is untrusted")
        prompt = prompt_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="idol-hermes-usage-") as td:
            usage_path = Path(td) / "usage.json"
            argv = [
                self.executable,
                "--oneshot",
                prompt,
                "--usage-file",
                str(usage_path),
                "--provider",
                route.provider,
                "--model",
                route.model,
            ]
            command = run_command(argv, cwd=cwd, timeout=order.estimated_seconds + 30, env=extra_env)
            data = _parse_json_stdout(command.stdout)
            if usage_path.exists():
                try:
                    usage = json.loads(usage_path.read_text(encoding="utf-8"))
                    if isinstance(usage, dict):
                        data.setdefault("usage", usage.get("usage", usage))
                        if "costUsd" not in data and isinstance(usage.get("estimated_cost"), (int, float)):
                            data["costUsd"] = usage["estimated_cost"]
                        data.setdefault("model", usage.get("model"))
                except json.JSONDecodeError:
                    raise RuntimePolicyViolation("Hermes usage report is invalid JSON")
            data.setdefault("status", "ok" if command.returncode == 0 and not command.timed_out else "error")
            data.setdefault("ok", command.returncode == 0 and not command.timed_out)
            data.setdefault("provider", route.provider)
            data.setdefault("model", route.model)
            result = _result(data, returncode=command.returncode, timed_out=command.timed_out, stdout=command.stdout, stderr=command.stderr)
            _enforce_route(route, result)
            return result
