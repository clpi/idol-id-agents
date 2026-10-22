from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from .control import ControlRefusal
from .coordination import (
    CoordinationError, assignment_subject, declaration, dependency_subject, digest,
    latest_assignment_ack, premise_subject, read_regular, route_subject, scope_subject,
)
from .journal import Journal, JournalTransaction
from .model import Route, WorkOrder, stable_hash
from .processes import kill_group_and_reap


LAW = "coordination/dispatch.id"
CONTROLS = "tests/dispatch.py"
SLOTS = "0123456789abcdefghijklmnopqrstuv"


class NativeDispatchRefusal(ControlRefusal):
    """Native evidence or verdict holds this assignment without route fallback."""


@dataclass(frozen=True, slots=True)
class NativeConfig:
    live_repository: Path
    live_revision: str
    source_sha256: str
    controls_sha256: str
    compiler: Path
    compiler_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "NativeConfig":
        if not isinstance(raw, Mapping):
            raise NativeDispatchRefusal("native_dispatch configuration is required for apply")
        try:
            value = cls(
                live_repository=Path(raw["live_repository"]).expanduser(),
                live_revision=raw["live_revision"], source_sha256=raw["source_sha256"],
                controls_sha256=raw["controls_sha256"],
                compiler=Path(raw["compiler"]).expanduser(), compiler_sha256=raw["compiler_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeDispatchRefusal("native_dispatch configuration is incomplete") from exc
        if not value.live_repository.is_absolute() or not value.compiler.is_absolute():
            raise NativeDispatchRefusal("native source and compiler paths must be absolute")
        for label, content, length in (
            ("live_revision", value.live_revision, 40), ("source_sha256", value.source_sha256, 64),
            ("controls_sha256", value.controls_sha256, 64), ("compiler_sha256", value.compiler_sha256, 64),
        ):
            if not isinstance(content, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", content):
                raise NativeDispatchRefusal(f"native {label} must be an exact lowercase digest")
        return value

    def subject(self) -> str:
        fields = asdict(self)
        fields["live_repository"] = str(self.live_repository)
        fields["compiler"] = str(self.compiler)
        return digest(fields)

    def source_files(self) -> tuple[Mapping[str, Any], ...]:
        rows = []
        for path, expected in (
            (self.live_repository / LAW, self.source_sha256),
            (self.live_repository / CONTROLS, self.controls_sha256),
            (self.compiler, self.compiler_sha256),
        ):
            try:
                row = asdict(Route._capture_proof_subject(path))
            except Exception as exc:
                raise NativeDispatchRefusal("native source/compiler subject is unavailable") from exc
            if row["sha256"] != expected:
                raise NativeDispatchRefusal("native source/compiler digest differs from its explicit pin")
            rows.append(row)
        return tuple(rows)


def _bounded(command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(command), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        returncode = process.returncode
        # A leader can close its pipes and exit while an ordinary descendant
        # continues mutating a build artifact. Finish the owned group before
        # accepting compiler, control-harness, or verdict evidence.
        kill_group_and_reap(process)
    except BaseException:
        kill_group_and_reap(process)
        raise
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _build_environment() -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env.update(IDOL_FLEET_NO_MODEL_INFERENCE="1", IDOL_FLEET_NO_PAYGO="1")
    return env


def calibrate_native(
    *, raw_config: Mapping[str, Any], state_dir: Path, observed_at: float, expires_at: float,
) -> Mapping[str, Any]:
    """Compile and exercise the canonical Live law ahead of admission locks."""
    raw = raw_config.get("native_dispatch")
    if raw is None:
        return {}
    config = NativeConfig.from_mapping(raw)
    before = config.source_files()
    for name, expected in ((LAW, config.source_sha256), (CONTROLS, config.controls_sha256)):
        result = _bounded(
            ["git", "show", f"{config.live_revision}:{name}"],
            cwd=config.live_repository, env=_build_environment(), timeout=10,
        )
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != expected:
            raise NativeDispatchRefusal("native files do not belong to the pinned Live revision")
    revision = _bounded(
        ["git", "rev-parse", "HEAD"], cwd=config.live_repository, env=_build_environment(), timeout=10,
    )
    if revision.returncode or revision.stdout.decode().strip() != config.live_revision:
        raise NativeDispatchRefusal("native Live checkout is not at its pinned revision")
    artifacts = Path(state_dir) / "native-dispatch"
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="compile-", dir=artifacts) as temporary:
        binary = Path(temporary) / "dispatch.bin"
        compiled = _bounded(
            [str(config.compiler), "compile", "--backend=direct", "--no-cache", str(config.live_repository / LAW), "-o", str(binary)],
            cwd=config.live_repository, env=_build_environment(), timeout=90,
        )
        if compiled.returncode or not binary.is_file():
            raise NativeDispatchRefusal("direct native dispatch compilation failed: " + compiled.stderr.decode("utf-8", "replace")[:1000])
        controlled = _bounded(
            [sys.executable, str(config.live_repository / CONTROLS), "--binary", str(binary), "--json"],
            cwd=config.live_repository, env=_build_environment(), timeout=60,
        )
        try:
            controls = json.loads(controlled.stdout)
        except (ValueError, TypeError) as exc:
            raise NativeDispatchRefusal("native controls did not emit bounded JSON evidence") from exc
        content = read_regular(binary, maximum=128 * 1024 * 1024)
        binary_hash = hashlib.sha256(content).hexdigest()
        if (
            controlled.returncode != 0 or not isinstance(controls, Mapping)
            or controls.get("version") != 1 or controls.get("backend") != "direct"
            or type(controls.get("cases")) is not int or controls["cases"] < 1
            or controls.get("passed") != controls["cases"] or controls.get("failures") != []
            or controls.get("source_sha256") != config.source_sha256
            or controls.get("controls_sha256") != config.controls_sha256
            or controls.get("binary_sha256") != binary_hash
        ):
            raise NativeDispatchRefusal("direct native dispatch controls failed or changed subject")
        if config.source_files() != before:
            raise NativeDispatchRefusal("native source/compiler changed during calibration")
        target = artifacts / f"{binary_hash}.bin"
        if target.exists():
            if read_regular(target, maximum=128 * 1024 * 1024) != content:
                raise NativeDispatchRefusal("native artifact path contains different bytes")
        else:
            os.chmod(binary, 0o500)
            try:
                os.link(binary, target)
            except FileExistsError:
                if read_regular(target, maximum=128 * 1024 * 1024) != content:
                    raise NativeDispatchRefusal("concurrent native artifact differs")
    artifact = asdict(Route._capture_proof_subject(target))
    return {
        "schema": "idol.fleet.native-dispatch-proof.v1", "backend": "direct",
        "config_subject": config.subject(), "observed_at": observed_at, "expires_at": expires_at,
        "live_revision": config.live_revision, "source_files": before, "artifact": artifact,
        "controls": dict(controls),
        "compilation": {
            "backend": "direct", "exit": compiled.returncode,
            "compiler_sha256": config.compiler_sha256, "source_sha256": config.source_sha256,
            "artifact_sha256": binary_hash,
        },
    }


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    return ""


def _fact(row: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = row.get("fact") if row else None
    return value if isinstance(value, Mapping) else {}


def _event(rows: Sequence[Mapping[str, Any]], event: str) -> Mapping[str, Any] | None:
    return next((row for row in rows if row.get("hash") == event), None)


def _ack_fields(env: dict[str, str], prefix: str, row: Mapping[str, Any] | None) -> None:
    fact = _fact(row)
    env[prefix + "EVENT"] = _text(row.get("hash")) if row else ""
    for field, key in {
        "TASK": "task_id", "ASSIGNMENT": "assignment_subject", "RECIPIENT": "recipient",
        "AT": "accepted_at", "EXPIRES": "expires_at", "SOURCE": "source", "UID": "uid",
    }.items():
        env[prefix + field] = _text(fact.get(key))


def evidence_environment(
    *, phase: str, order: WorkOrder, route: Route, attempt_id: str,
    rows: Sequence[Mapping[str, Any]], now: int, state_dir: Path,
) -> tuple[dict[str, str], Mapping[str, Any]]:
    """Project raw facts only; legality and retry decisions stay in the .id law."""
    coordination = declaration(order)
    subject = assignment_subject(order)
    dependency = dependency_subject(order, rows, observed=False)
    env = {
        "VERSION": "1", "PHASE": phase, "TASK": order.task_id, "ASSIGNMENT": subject,
        "SCOPE": scope_subject(order), "BASE": order.base_sha, "MODE": coordination.mode,
        "RECIPIENT": coordination.recipient, "PARENT_TASK": coordination.parent_task,
        "PARENT_SUBJECT": coordination.parent_subject, "DEPENDENCY": dependency,
        "OBSERVED_DEPENDENCY": dependency_subject(order, rows, observed=True),
        "PREMISE": premise_subject(order), "ROUTE": route_subject(route),
        "HYPOTHESIS": coordination.hypothesis, "ATTEMPTS": str(coordination.attempts),
        "ALLOWANCE": str(coordination.allowance), "TIMEOUT": str(route.timeout_seconds),
        "RECOVERY": coordination.recovery, "QUESTION": coordination.question,
        "ARTIFACT": coordination.artifact, "RECIPE": coordination.recipe,
        "NOW": str(now), "ATTEMPT": attempt_id,
    }
    starts = [row for row in rows if row.get("kind") == "attempt.started" and _fact(row).get("task_id") == order.task_id]
    own = next((row for row in starts if _fact(row).get("attempt_id") == attempt_id), None)
    if phase == "launch" and own is not None:
        accepted = _event(rows, _text(_fact(own).get("ack_event")))
    else:
        accepted = latest_assignment_ack(rows, order.task_id)
    if accepted is not None and accepted.get("kind") != "assignment.ack":
        accepted = None
    _ack_fields(env, "ACK_", accepted)
    for field, key in {
        "SCOPE": "scope_subject", "BASE": "base_sha", "MODE": "mode", "PARENT_TASK": "parent_task",
        "PARENT_SUBJECT": "parent_subject", "DEPENDENCY": "dependency_subject", "ROUTE": "route_subject",
    }.items():
        env["ACK_" + field] = _text(_fact(accepted).get(key))
    delegated = next((row for row in reversed(rows) if row.get("kind") == "assignment.delegated" and _fact(row).get("assignment_subject") == subject), None)
    env["DELEGATION_EVENT"] = _text(delegated.get("hash")) if delegated else ""
    for field, key in {
        "PARENT_TASK": "parent_task", "PARENT_SUBJECT": "parent_subject", "TASK": "task_id",
        "ASSIGNMENT": "assignment_subject", "RECIPIENT": "recipient", "OWNER": "owner",
    }.items():
        env["DELEGATION_" + field] = _text(_fact(delegated).get(key))
    parent_ack = _event(rows, _text(_fact(delegated).get("parent_ack")))
    if parent_ack is not None and parent_ack.get("kind") != "assignment.ack":
        parent_ack = None
    _ack_fields(env, "PARENT_ACK_", parent_ack)
    recipe = _event(rows, coordination.recipe)
    if recipe is not None and recipe.get("kind") != "recipe.observed":
        recipe = None
    env["RECIPE_EVENT"] = _text(recipe.get("hash")) if recipe else ""
    for field, key in {
        "ID": "recipe_id", "TASK": "task_id", "SCOPE": "scope_subject", "BASE": "base_sha",
        "DEPENDENCY": "dependency_subject", "PREMISE": "premise_subject", "VERIFIER": "verifier",
        "AT": "observed_at", "POSITIVE_CONTROL": "positive_control",
        "POSITIVE_EXPECTED": "positive_expected", "POSITIVE_ACTUAL": "positive_actual",
        "NEGATIVE_CONTROL": "negative_control", "NEGATIVE_EXPECTED": "negative_expected",
        "NEGATIVE_ACTUAL": "negative_actual", "ARTIFACT": "artifact_sha256",
    }.items():
        env["RECIPE_" + field] = _text(_fact(recipe).get(key))
    env["RECIPE_OBSERVED_ARTIFACT"] = ""
    if recipe is not None:
        artifact = Path(_text(_fact(recipe).get("artifact_path")))
        try:
            artifact.resolve(strict=True).relative_to((state_dir / "recipe-evidence").resolve(strict=True))
            env["RECIPE_OBSERVED_ARTIFACT"] = hashlib.sha256(read_regular(artifact)).hexdigest()
        except (OSError, ValueError, CoordinationError):
            pass  # Missing physical evidence is carried to the native refusal.
    env["HISTORY_COUNT"] = str(len(starts))
    # Overflow is explicit, never a shortened history presented as complete.
    if len(starts) <= len(SLOTS):
        for slot, started in zip(SLOTS, starts):
            fact = _fact(started)
            history_id = fact.get("attempt_id")
            events = [row for row in rows if str(row.get("kind", "")).startswith("attempt.") and _fact(row).get("attempt_id") == history_id]
            prefix = f"HISTORY_{slot}_"
            for field, key in {
                "ID": "attempt_id", "TASK": "task_id", "ASSIGNMENT": "assignment_subject",
                "DEPENDENCY": "dependency_subject", "HYPOTHESIS": "hypothesis",
                "MODE": "mode", "ACK": "ack_event",
            }.items():
                env[prefix + field] = _text(fact.get(key))
            env[prefix + "STATE"] = _text(events[-1].get("kind")) if events else ""
            env[prefix + "LAUNCHES"] = str(sum(row.get("kind") == "attempt.launching" for row in events))
    facts = {
        "assignment_subject": subject, "scope_subject": env["SCOPE"], "dependency_subject": dependency,
        "mode": coordination.mode, "hypothesis": coordination.hypothesis, "recipient": coordination.recipient,
        "ack_event": env["ACK_EVENT"], "native_route_subject": env["ROUTE"],
    }
    return {"DISPATCH_" + key: value for key, value in env.items()}, facts


class NativeDispatcher:
    def __init__(self, *, raw_config: Mapping[str, Any], calibration_file: Path, journal: Journal) -> None:
        self.raw_config = raw_config
        self.calibration_file = calibration_file
        self.journal = journal

    def proof(self) -> Mapping[str, Any]:
        config = NativeConfig.from_mapping(self.raw_config.get("native_dispatch"))
        try:
            calibration = json.loads(read_regular(self.calibration_file, maximum=4 * 1024 * 1024))
            proof = calibration["native_dispatch"]
            now = time.time()
            if (
                calibration["config_hash"] != stable_hash(self.raw_config)
                or proof["schema"] != "idol.fleet.native-dispatch-proof.v1" or proof["backend"] != "direct"
                or proof["config_subject"] != config.subject()
                or not proof["observed_at"] <= now < proof["expires_at"]
                or tuple(proof["source_files"]) != config.source_files()
            ):
                raise NativeDispatchRefusal("native calibration is stale or its source changed")
            artifact = asdict(Route._capture_proof_subject(Path(proof["artifact"]["path"])))
            if artifact != proof["artifact"]:
                raise NativeDispatchRefusal("native compiled artifact changed after calibration")
            controls = proof["controls"]
            compilation = proof["compilation"]
            if (
                controls.get("backend") != "direct" or controls.get("binary_sha256") != artifact["sha256"]
                or controls.get("source_sha256") != config.source_sha256
                or controls.get("controls_sha256") != config.controls_sha256
                or type(controls.get("cases")) is not int or controls["cases"] < 1
                or type(controls.get("passed")) is not int or controls["passed"] != controls["cases"]
                or controls.get("failures") != []
                or compilation != {"backend": "direct", "exit": 0, "compiler_sha256": config.compiler_sha256,
                                   "source_sha256": config.source_sha256, "artifact_sha256": artifact["sha256"]}
            ):
                raise NativeDispatchRefusal("native calibration lacks its direct control evidence")
            return proof
        except NativeDispatchRefusal:
            raise
        except Exception as exc:
            raise NativeDispatchRefusal("native calibration evidence is unavailable or malformed") from exc

    def subject(self) -> str:
        return digest(self.proof())

    def decide(
        self, *, phase: str, order: WorkOrder, route: Route, attempt_id: str,
        transaction: JournalTransaction, native_subject: str,
    ) -> Mapping[str, Any]:
        proof = self.proof()
        if digest(proof) != native_subject:
            raise NativeDispatchRefusal("native proof differs from the authenticated execution subject")
        env, facts = evidence_environment(
            phase=phase, order=order, route=route, attempt_id=attempt_id,
            rows=transaction.events(), now=int(time.time()), state_dir=self.journal.path.parent,
        )
        # A clean evidence-only environment prevents inherited DISPATCH_* values
        # or provider auth variables from inventing missing faces.
        try:
            result = _bounded(
                [proof["artifact"]["path"]], cwd=self.journal.path.parent,
                env={**env, "LC_ALL": "C"}, timeout=2,
            )
        except Exception as exc:
            transaction.append("dispatch.checked", {
                "phase": phase, "task_id": order.task_id, "order_id": order.id,
                "attempt_id": attempt_id, "native_subject": native_subject,
                "evidence_sha256": digest(env), "exit": None, "error_type": type(exc).__name__,
            })
            raise NativeDispatchRefusal("native dispatch process did not return a bounded verdict") from exc
        transaction.append("dispatch.checked", {
            **facts, "phase": phase, "task_id": order.task_id, "order_id": order.id,
            "attempt_id": attempt_id, "native_subject": native_subject,
            "evidence_sha256": digest(env), "exit": result.returncode,
        })
        if result.returncode != 0:
            raise NativeDispatchRefusal(f"native dispatch refused {phase} with exit {result.returncode}")
        return {**facts, "native_subject": native_subject, "native_evidence_sha256": digest(env)}
