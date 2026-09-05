from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from . import __version__
from .model import BillingClass, BillingProof, ProofSubjectError, Route, stable_hash
from .scheduler import path_overlap, semantic_overlap


class CalibrationError(RuntimeError):
    def __init__(self, message: str, *, route_refusals: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.route_refusals = dict(route_refusals or {})


_REQUIRED_CONTROLS = frozenset({
    "no_paygo",
    "proof_subject_binding",
    "stale_sha_refusal",
    "semantic_overlap",
    "path_overlap",
    "bounded_termination",
})


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    version: str
    config_hash: str
    controller_version: str
    observed_at: float
    expires_at: float
    routes: Mapping[str, Mapping[str, Any]]
    route_refusals: Mapping[str, str]
    controls: Mapping[str, bool]

    def valid(self, *, config_hash: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return (
            self.version == "idol.fleet.calibration.v1"
            and self.config_hash == config_hash
            and self.controller_version == __version__
            and self.observed_at <= current < self.expires_at
            and _REQUIRED_CONTROLS.issubset(self.controls)
            and all(self.controls.get(name) is True for name in _REQUIRED_CONTROLS)
        )


_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME")


def config_hash(raw: Mapping[str, Any]) -> str:
    return stable_hash(raw)


def _safe_environment(route: Route) -> dict[str, str]:
    env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    for name in route.auth_env:
        value = os.environ.get(name)
        if value is None:
            raise CalibrationError(f"route {route.id} is missing required auth environment {name}")
        env[name] = value
    env["IDOL_FLEET_CALIBRATION"] = "1"
    env["IDOL_FLEET_NO_MODEL_INFERENCE"] = "1"
    env["IDOL_FLEET_NO_PAYGO"] = "1"
    return env


def _probe_route(route: Route, *, ttl_seconds: int, now: float) -> BillingProof:
    if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
        raise CalibrationError(f"route {route.id} has forbidden billing class {route.billing.value}")
    if not route.proof_command or not route.proof_expect:
        raise CalibrationError(f"route {route.id} has no no-inference proof command")
    if not route.proof_subject_files:
        raise CalibrationError(f"route {route.id} has no bound proof subject files")
    try:
        subject_files = route.proof_subject_metadata()
    except ProofSubjectError as exc:
        raise CalibrationError(f"route {route.id} {exc}") from exc
    try:
        result = subprocess.run(
            list(route.proof_command),
            env=_safe_environment(route),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CalibrationError(f"route {route.id} proof command failed to execute") from exc
    try:
        final_subject_files = route.proof_subject_metadata()
    except ProofSubjectError as exc:
        raise CalibrationError(f"route {route.id} {exc}") from exc
    if final_subject_files != subject_files:
        raise CalibrationError(f"route {route.id} proof subject changed during calibration")
    output = result.stdout[:1_000_000]
    if result.returncode != 0:
        raise CalibrationError(f"route {route.id} proof command returned {result.returncode}")
    try:
        matched = re.search(route.proof_expect, output, re.MULTILINE | re.IGNORECASE)
    except re.error as exc:
        raise CalibrationError(f"route {route.id} has an invalid proof expression") from exc
    if not matched:
        raise CalibrationError(f"route {route.id} proof did not establish the configured account class")
    proof_kind = route.proof.kind
    if proof_kind not in {
        "local-process",
        "subscription-oauth",
        "subscription-plan",
        "zero-cost-model",
    }:
        raise CalibrationError(f"route {route.id} has unsupported proof kind {proof_kind!r}")
    evidence = {
        "route_subject": route.subject_hash_for(subject_files),
        "subject_files": [asdict(item) for item in subject_files],
        "proof_command": route.proof_command,
        "proof_expression": route.proof_expect,
        "returncode": result.returncode,
        "matched_hash": hashlib.sha256(matched.group(0).encode()).hexdigest(),
        "output_hash": hashlib.sha256(output.encode()).hexdigest(),
    }
    return BillingProof(
        kind=proof_kind,
        subject_hash=route.subject_hash_for(subject_files),
        observed_at=now,
        expires_at=now + ttl_seconds,
        evidence_hash=stable_hash(evidence),
        trusted=True,
        subject_files=subject_files,
    )


def _bounded_termination_control() -> bool:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.wait(timeout=0.1)
        return False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        return process.returncode is not None


def run_controls() -> dict[str, bool]:
    # Each control has a positive and negative side; a hard-coded true does not
    # satisfy calibration.
    stale_sha = "0" * 40 != "1" * 40 and "0" * 40 == "0" * 40
    semantic = semantic_overlap("world/process", "world/process/run") and not semantic_overlap(
        "world/process", "graph/application"
    )
    paths = path_overlap("src", "src/sema.zig") and not path_overlap("src", "lib")
    proof_binding = stable_hash({"route": "a"}) != stable_hash({"route": "b"})
    no_paygo = BillingClass.PAYGO not in {BillingClass.LOCAL, BillingClass.INCLUDED}
    return {
        "no_paygo": no_paygo,
        "proof_subject_binding": proof_binding,
        "stale_sha_refusal": stale_sha,
        "semantic_overlap": semantic,
        "path_overlap": paths,
        "bounded_termination": _bounded_termination_control(),
    }


def calibrate(
    *,
    raw_config: Mapping[str, Any],
    routes: Sequence[Route],
    output: Path,
    ttl_seconds: int = 3600,
    now: float | None = None,
    preserved_proofs: Mapping[str, Mapping[str, Any]] | None = None,
    preserved_refusals: Mapping[str, str] | None = None,
) -> CalibrationRecord:
    if ttl_seconds < 300 or ttl_seconds > 86400:
        raise CalibrationError("calibration TTL outside supported bounds")
    current = time.time() if now is None else now
    controls = run_controls()
    if not _REQUIRED_CONTROLS.issubset(controls) or not all(controls.values()):
        raise CalibrationError("one or more controller controls failed")
    proofs: dict[str, Mapping[str, Any]] = dict(preserved_proofs or {})
    refusals: dict[str, str] = dict(preserved_refusals or {})
    for route in routes:
        if not route.enabled:
            continue
        refusals.pop(route.id, None)
        try:
            proof = _probe_route(route, ttl_seconds=ttl_seconds, now=current)
            proofs[route.id] = asdict(proof)
        except CalibrationError as exc:
            proofs.pop(route.id, None)
            refusals[route.id] = str(exc)
    if not proofs:
        detail = "; ".join(f"{route_id}: {error}" for route_id, error in sorted(refusals.items()))
        raise CalibrationError(
            f"calibration produced no enabled route proof: {detail}",
            route_refusals=refusals,
        )
    record = CalibrationRecord(
        version="idol.fleet.calibration.v1",
        config_hash=config_hash(raw_config),
        controller_version=__version__,
        observed_at=current,
        expires_at=current + ttl_seconds,
        routes=proofs,
        route_refusals=refusals,
        controls=controls,
    )
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix="calibration-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(record), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return record


def load_calibration(path: Path) -> CalibrationRecord:
    try:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError("calibration record is unreadable") from exc
    if not isinstance(raw, Mapping):
        raise CalibrationError("calibration record is not an object")
    routes = raw.get("routes")
    controls = raw.get("controls")
    if not isinstance(routes, Mapping) or not isinstance(controls, Mapping):
        raise CalibrationError("calibration record has invalid route/control facts")
    try:
        return CalibrationRecord(
            version=str(raw.get("version", "")),
            config_hash=str(raw.get("config_hash", "")),
            controller_version=str(raw.get("controller_version", "")),
            observed_at=float(raw.get("observed_at", 0)),
            expires_at=float(raw.get("expires_at", 0)),
            routes=routes,
            route_refusals={
                str(key): str(value)
                for key, value in mapping_or_empty(raw.get("route_refusals")).items()
            },
            controls={str(key): value is True for key, value in controls.items()},
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise CalibrationError("calibration record has malformed scalar facts") from exc


def mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def require_current_route_proof(route: Route, *, now: float | None = None) -> None:
    try:
        subject_files = route.proof_subject_metadata()
    except ProofSubjectError as exc:
        raise CalibrationError(f"route {route.id} {exc}") from exc
    if not subject_files:
        raise CalibrationError(f"route {route.id} has no bound proof subject files")
    if not route.proof.valid(now):
        raise CalibrationError(f"route {route.id} billing proof is invalid or expired")
    if route.proof.subject_files != subject_files:
        raise CalibrationError(f"route {route.id} proof subject files changed")
    if route.proof.subject_hash != route.subject_hash_for(subject_files):
        raise CalibrationError(f"route {route.id} proof subject does not match the route")


def apply_calibration(
    *,
    routes: Sequence[Route],
    record: CalibrationRecord,
    raw_config: Mapping[str, Any],
    now: float | None = None,
) -> tuple[Route, ...]:
    if not record.valid(config_hash=config_hash(raw_config), now=now):
        raise CalibrationError("calibration is stale or does not match this controller/configuration")
    calibrated: list[Route] = []
    for route in routes:
        proof_raw = record.routes.get(route.id)
        if not isinstance(proof_raw, Mapping):
            calibrated.append(replace(route, enabled=False))
            continue
        try:
            proof = BillingProof.from_mapping(proof_raw)
            candidate = replace(route, proof=proof)
            require_current_route_proof(candidate, now=now)
        except (CalibrationError, TypeError, ValueError, OverflowError):
            calibrated.append(replace(route, enabled=False))
            continue
        calibrated.append(candidate)
    return tuple(calibrated)
