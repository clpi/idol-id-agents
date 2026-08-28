from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any, Mapping

from .model import BillingClass, Route, WorkOrder
from .policy import assert_order_route


class RuntimeRefusal(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunResult:
    route_id: str
    provider: str
    model: str
    status: str
    returncode: int
    started_at: float
    ended_at: float
    stdout: str
    stderr: str
    usage: Mapping[str, Any]
    cost_usd: float | None
    session_id: str | None


class CommandRuntime:
    """Run one calibrated route without a shell and with bounded termination."""

    _BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME")

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _format_argument(argument: str, values: Mapping[str, str]) -> str:
        try:
            return argument.format_map(values)
        except KeyError as exc:
            raise RuntimeRefusal(f"runtime argument references unknown placeholder: {exc.args[0]}") from exc

    def _environment(self, route: Route) -> dict[str, str]:
        env = {name: os.environ[name] for name in self._BASE_ENV if name in os.environ}
        env["IDOL_FLEET_ROUTE"] = route.id
        env["IDOL_FLEET_NO_PAYGO"] = "1"
        env["IDOL_FLEET_BILLING_CLASS"] = route.billing.value
        for name in route.auth_env:
            value = os.environ.get(name)
            if value is None:
                raise RuntimeRefusal(f"required route auth environment is absent: {name}")
            env[name] = value
        return env

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=8)

    @staticmethod
    def _bounded_text(value: str, limit: int = 2_000_000) -> str:
        if len(value) <= limit:
            return value
        return value[:limit] + "\n[controller-output-truncated]\n"

    @staticmethod
    def _json_object(text: str) -> Mapping[str, Any]:
        stripped = text.strip()
        if not stripped:
            raise RuntimeRefusal("runtime returned no JSON envelope")
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            # Some CLIs print progress before one final JSON line. Read only a
            # complete object from the last non-empty line; never guess fields.
            lines = [line for line in stripped.splitlines() if line.strip()]
            try:
                value = json.loads(lines[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise RuntimeRefusal("runtime output has no valid JSON envelope") from exc
        if not isinstance(value, Mapping):
            raise RuntimeRefusal("runtime JSON envelope is not an object")
        return value

    def _parse_openclaw(
        self,
        *,
        route: Route,
        stdout: str,
        stderr: str,
        returncode: int,
        started_at: float,
        ended_at: float,
    ) -> RunResult:
        payload = self._json_object(stdout)
        status = str(payload.get("status") or ("ok" if payload.get("ok") is True else "failed"))
        provider = str(payload.get("provider") or payload.get("usage", {}).get("provider") or "")
        model = str(payload.get("model") or payload.get("usage", {}).get("model") or "")
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        raw_cost = payload.get("costUsd")
        if raw_cost is None and isinstance(usage, Mapping):
            raw_cost = usage.get("costUsd")
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        session = payload.get("sessionId") or payload.get("session_id")
        if returncode != 0 or status.lower() not in {"ok", "success", "completed"}:
            raise RuntimeRefusal(f"OpenClaw route failed: status={status!r} returncode={returncode}")
        if provider and provider != route.provider:
            raise RuntimeRefusal(f"provider mismatch: expected {route.provider!r}, observed {provider!r}")
        if model and model != route.model:
            raise RuntimeRefusal(f"model mismatch: expected {route.model!r}, observed {model!r}")
        if route.billing is BillingClass.LOCAL and cost not in {None, 0.0}:
            raise RuntimeRefusal("local route reported positive model cost")
        return RunResult(
            route_id=route.id,
            provider=provider or route.provider,
            model=model or route.model,
            status=status,
            returncode=returncode,
            started_at=started_at,
            ended_at=ended_at,
            stdout=self._bounded_text(stdout),
            stderr=self._bounded_text(stderr),
            usage=dict(usage),
            cost_usd=cost,
            session_id=str(session) if session else None,
        )

    def _parse_plain_json(
        self,
        *,
        route: Route,
        stdout: str,
        stderr: str,
        returncode: int,
        started_at: float,
        ended_at: float,
    ) -> RunResult:
        payload = self._json_object(stdout)
        status = str(payload.get("status", "ok" if returncode == 0 else "failed"))
        if returncode != 0 or status.lower() not in {"ok", "success", "completed"}:
            raise RuntimeRefusal(f"plain JSON route failed: status={status!r} returncode={returncode}")
        provider = str(payload.get("provider", route.provider))
        model = str(payload.get("model", route.model))
        if provider != route.provider or model != route.model:
            raise RuntimeRefusal("plain JSON route identity mismatch")
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        raw_cost = payload.get("costUsd")
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        if route.billing is BillingClass.LOCAL and cost not in {None, 0.0}:
            raise RuntimeRefusal("local route reported positive model cost")
        return RunResult(
            route_id=route.id,
            provider=provider,
            model=model,
            status=status,
            returncode=returncode,
            started_at=started_at,
            ended_at=ended_at,
            stdout=self._bounded_text(stdout),
            stderr=self._bounded_text(stderr),
            usage=dict(usage),
            cost_usd=cost,
            session_id=str(payload.get("sessionId")) if payload.get("sessionId") else None,
        )

    def _parse_hermes(
        self,
        *,
        route: Route,
        stdout: str,
        stderr: str,
        returncode: int,
        usage_path: Path,
        started_at: float,
        ended_at: float,
    ) -> RunResult:
        if returncode != 0:
            raise RuntimeRefusal(f"Hermes route failed with returncode {returncode}")
        if not usage_path.is_file():
            raise RuntimeRefusal("Hermes route produced no usage envelope")
        payload = self._json_object(usage_path.read_text(encoding="utf-8"))
        provider = str(payload.get("provider") or "")
        model = str(payload.get("model") or "")
        if provider != route.provider or model != route.model:
            raise RuntimeRefusal(
                f"Hermes route identity mismatch: expected {route.provider}/{route.model}, observed {provider}/{model}"
            )
        raw_cost = payload.get("cost_usd", payload.get("costUsd"))
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        if route.billing is BillingClass.LOCAL and cost not in {None, 0.0}:
            raise RuntimeRefusal("local Hermes route reported positive model cost")
        return RunResult(
            route_id=route.id,
            provider=provider,
            model=model,
            status="completed",
            returncode=returncode,
            started_at=started_at,
            ended_at=ended_at,
            stdout=self._bounded_text(stdout),
            stderr=self._bounded_text(stderr),
            usage=dict(payload),
            cost_usd=cost,
            session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
        )

    def execute(self, *, route: Route, order: WorkOrder, prompt_path: Path, cwd: Path) -> RunResult:
        assert_order_route(order, route)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="idol-fleet-usage-",
            suffix=".json",
            dir=self.state_dir,
            delete=False,
        ) as usage_handle:
            usage_path = Path(usage_handle.name)
        usage_path.unlink(missing_ok=True)
        values = {
            "prompt": str(prompt_path),
            "prompt_text": prompt_text,
            "cwd": str(cwd),
            "model": route.model,
            "provider": route.provider,
            "order": order.id,
            "task": order.task_id,
            "usage": str(usage_path),
        }
        command = [self._format_argument(argument, values) for argument in route.command]
        started_at = time.time()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=self._environment(route),
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=route.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            raise RuntimeRefusal(f"route {route.id} exceeded {route.timeout_seconds}s") from exc
        finally:
            ended_at = time.time()
        try:
            if route.parser == "openclaw-json":
                return self._parse_openclaw(
                    route=route,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=process.returncode,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            if route.parser == "hermes-usage":
                return self._parse_hermes(
                    route=route,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=process.returncode,
                    usage_path=usage_path,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            return self._parse_plain_json(
                route=route,
                stdout=stdout,
                stderr=stderr,
                returncode=process.returncode,
                started_at=started_at,
                ended_at=ended_at,
            )
        finally:
            usage_path.unlink(missing_ok=True)
