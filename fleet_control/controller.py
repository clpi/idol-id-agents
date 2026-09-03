from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping, Sequence

from .calibration import CalibrationError, apply_calibration, calibrate, config_hash, load_calibration
from .claims import ControllerLease, RepositoryClaimTransaction, SemanticClaimStore
from .gitops import (
    GitRefusal,
    commit_claimed,
    create_draft_pull_request,
    create_worktree,
    current_sha,
    fast_forward,
    fetch_remote_branch,
    is_dirty,
    paths_changed,
    publish_branch,
    remote_branch_sha,
    require_claimed_changes,
    require_exact_subject,
)
from .health import apply_circuits, record_failure, record_success
from .journal import Journal
from .model import Assignment, Route, WorkOrder, load_routes, mapping, sequence, stable_hash
from .policy import assert_order_route
from .performance import route_factors
from .runtime import CommandRuntime, RuntimeRefusal
from .scheduler import Plan, Rejection, build_plan
from .usage import refresh_routes


class ControllerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    mode: str
    repository: Path
    state_dir: Path
    work_orders_dir: Path
    calibration_file: Path
    interval_seconds: int
    max_assignments: int
    claim_ttl_seconds: int
    witness_timeout_seconds: int
    base_branch: str
    author_name: str
    author_email: str
    auto_calibrate: bool
    calibration_ttl_seconds: int
    repository_claim_required: bool
    max_consecutive_cycle_failures: int
    remote_name: str
    remote_head_required: bool
    auto_fast_forward: bool
    routes: tuple[Route, ...]

    def __post_init__(self) -> None:
        if self.mode not in {"observe-plan", "apply"}:
            raise ValueError("mode must be observe-plan or apply")
        for label, path in (
            ("repository", self.repository),
            ("state_dir", self.state_dir),
            ("work_orders_dir", self.work_orders_dir),
            ("calibration_file", self.calibration_file),
        ):
            if not path.is_absolute():
                raise ValueError(f"{label} must be absolute")
        if self.interval_seconds < 15 or self.interval_seconds > 86400:
            raise ValueError("interval_seconds outside supported bounds")
        if self.max_assignments < 1 or self.max_assignments > 32:
            raise ValueError("max_assignments outside supported bounds")
        if self.claim_ttl_seconds < 60 or self.claim_ttl_seconds > 86400:
            raise ValueError("claim_ttl_seconds outside supported bounds")
        if self.witness_timeout_seconds < 10 or self.witness_timeout_seconds > 86400:
            raise ValueError("witness_timeout_seconds outside supported bounds")
        if self.calibration_ttl_seconds < 300 or self.calibration_ttl_seconds > 86400:
            raise ValueError("calibration_ttl_seconds outside supported bounds")
        if not self.routes:
            raise ValueError("controller requires at least one route")
        if self.max_consecutive_cycle_failures < 1 or self.max_consecutive_cycle_failures > 100:
            raise ValueError("max_consecutive_cycle_failures outside supported bounds")
        if self.auto_fast_forward and not self.remote_head_required:
            raise ValueError("auto_fast_forward requires remote_head_required")
        enabled_routes = tuple(route for route in self.routes if route.enabled)
        if self.mode == "apply" and not enabled_routes:
            raise ValueError("apply mode requires at least one enabled route")
        if (
            self.mode == "apply"
            and self.claim_ttl_seconds <= max(route.timeout_seconds for route in enabled_routes) + 60
        ):
            raise ValueError("claim_ttl_seconds must exceed every route timeout by at least 60 seconds")


@dataclass(frozen=True, slots=True)
class Observation:
    at: float
    repository: str
    head: str
    remote_head: str | None
    remote_in_sync: bool | None
    remote_error: str | None
    dirty: bool
    work_orders: tuple[str, ...]
    invalid_work_orders: Mapping[str, str]
    live_semantic_claims: tuple[Mapping[str, Any], ...]
    route_subjects: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CycleResult:
    mode: str
    observation: Observation
    plan: Plan
    attempts: tuple[Mapping[str, Any], ...]


def _absolute(value: Any, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def load_config(path: Path) -> tuple[Mapping[str, Any], ControllerConfig]:
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"controller configuration is unreadable: {config_path}") from exc
    raw = mapping(raw, "configuration")
    routes_raw = sequence(raw.get("routes"), "routes")
    routes = load_routes(mapping(item, "route") for item in routes_raw)
    config = ControllerConfig(
        mode=str(raw.get("mode", "observe-plan")),
        repository=_absolute(raw.get("repository"), "repository"),
        state_dir=_absolute(raw.get("state_dir"), "state_dir"),
        work_orders_dir=_absolute(raw.get("work_orders_dir"), "work_orders_dir"),
        calibration_file=_absolute(raw.get("calibration_file"), "calibration_file"),
        interval_seconds=int(raw.get("interval_seconds", 60)),
        max_assignments=int(raw.get("max_assignments", 1)),
        claim_ttl_seconds=int(raw.get("claim_ttl_seconds", 3600)),
        witness_timeout_seconds=int(raw.get("witness_timeout_seconds", 1800)),
        base_branch=str(raw.get("base_branch", "main")),
        author_name=str(raw.get("author_name", "idol-fleet-agent")),
        author_email=str(raw.get("author_email", "noreply@idol.id")),
        auto_calibrate=raw.get("auto_calibrate", False) is True,
        calibration_ttl_seconds=int(raw.get("calibration_ttl_seconds", 21600)),
        repository_claim_required=raw.get("repository_claim_required", True) is True,
        max_consecutive_cycle_failures=int(raw.get("max_consecutive_cycle_failures", 5)),
        remote_name=str(raw.get("remote_name", "origin")),
        remote_head_required=raw.get("remote_head_required", False) is True,
        auto_fast_forward=raw.get("auto_fast_forward", False) is True,
        routes=routes,
    )
    return raw, config


class FleetController:
    def __init__(self, *, config_path: Path, mode_override: str | None = None) -> None:
        self.config_path = Path(config_path).expanduser()
        raw, config = load_config(self.config_path)
        if mode_override is not None:
            config = replace(config, mode=mode_override)
        self.raw_config = raw
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config.work_orders_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.state_dir, 0o700)
        os.chmod(self.config.work_orders_dir, 0o700)
        self.journal = Journal(self.config.state_dir / "fleet-history.jsonl")
        self.semantic_claims = SemanticClaimStore(self.config.state_dir / "claims")
        self.path_claims = SemanticClaimStore(self.config.state_dir / "path-claims")
        self.runtime = CommandRuntime(self.config.state_dir / "runtime")
        self.lease_path = self.config.state_dir / "controller.lock"

    def _routes(self, *, now: float | None = None) -> tuple[Route, ...]:
        routes = self.config.routes
        if self.config.mode == "apply":
            try:
                record = load_calibration(self.config.calibration_file)
                if not record.valid(config_hash=config_hash(self.raw_config), now=now):
                    raise CalibrationError("calibration is stale or does not match this controller/configuration")
            except CalibrationError:
                if not self.config.auto_calibrate:
                    raise
                record = calibrate(
                    raw_config=self.raw_config,
                    routes=routes,
                    output=self.config.calibration_file,
                    ttl_seconds=self.config.calibration_ttl_seconds,
                    now=now,
                )
                self.journal.append(
                    "fleet.calibration.refreshed",
                    {
                        "expires_at": record.expires_at,
                        "config_hash": record.config_hash,
                        "route_refusals": dict(record.route_refusals),
                    },
                    at=now,
                )
            routes = apply_calibration(
                routes=routes,
                record=record,
                raw_config=self.raw_config,
                now=now,
            )
        routes, facts = refresh_routes(routes, now=now)
        for fact in facts:
            self.journal.append("fleet.usage.observed" if fact["ok"] else "fleet.usage.refused", fact, at=now)
        routes, circuits = apply_circuits(routes, self.journal, now=now)
        for circuit in circuits:
            if circuit.open(time.time() if now is None else now):
                self.journal.append(
                    "fleet.route.circuit-open",
                    asdict(circuit),
                    at=now,
                )
        return routes

    def reconcile_expired_attempts(self, *, now: float | None = None) -> tuple[str, ...]:
        from .reconcile import reconcile_expired_attempts

        return reconcile_expired_attempts(self, now=now)

    def _load_work_orders(self) -> tuple[tuple[WorkOrder, ...], dict[str, str]]:
        orders: list[WorkOrder] = []
        invalid: dict[str, str] = {}
        for path in sorted(self.config.work_orders_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                order = WorkOrder.from_mapping(mapping(raw, "work order"))
                if order.repository.resolve() != self.config.repository.resolve():
                    raise ValueError("work-order repository differs from controller repository")
                orders.append(order)
            except Exception as exc:
                invalid[path.name] = f"{type(exc).__name__}: {exc}"
        ids = [order.id for order in orders]
        if len(ids) != len(set(ids)):
            duplicates = sorted({value for value in ids if ids.count(value) > 1})
            raise ControllerError("duplicate work-order ids: " + ", ".join(duplicates))
        return tuple(orders), invalid

    def _running_attempt_ids(self) -> frozenset[str]:
        state: dict[str, str] = {}
        terminal = {
            "attempt.ready",
            "attempt.refused",
            "attempt.failed",
            "attempt.cancelled",
            "attempt.admitted",
            "attempt.rejected",
            "attempt.reverted",
        }
        for row in self.journal.events():
            fact = row.get("fact")
            if not isinstance(fact, Mapping):
                continue
            attempt_id = fact.get("attempt_id")
            if not isinstance(attempt_id, str):
                continue
            kind = str(row.get("kind"))
            if kind == "attempt.started":
                state[attempt_id] = kind
            elif kind in terminal:
                state[attempt_id] = kind
        return frozenset(attempt_id for attempt_id, kind in state.items() if kind == "attempt.started")

    @staticmethod
    def _write_rebound_order(
        path: Path,
        *,
        old_sha: str,
        new_sha: str,
        expected_hash: str,
    ) -> None:
        raw = mapping(json.loads(path.read_text(encoding="utf-8")), "work order")
        if stable_hash(raw) != expected_hash:
            raise ControllerError(f"work order changed before remote rebind: {path.name}")
        if raw.get("base_sha") != old_sha:
            raise ControllerError(f"work order moved during remote refresh: {path.name}")
        updated = dict(raw)
        updated["base_sha"] = new_sha
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(updated, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def refresh_remote_base(self) -> None:
        if not self.config.remote_head_required or self.config.mode != "apply" or not self.config.auto_fast_forward:
            return
        observed_at = time.time()
        old_sha: str | None = None
        new_sha: str | None = None
        moved = False
        try:
            old_sha = current_sha(self.config.repository)
            new_sha = fetch_remote_branch(
                self.config.repository,
                remote=self.config.remote_name,
                branch=self.config.base_branch,
            )
            if new_sha == old_sha:
                return
            if is_dirty(self.config.repository):
                raise GitRefusal("authority worktree is dirty")
            running = self._running_attempt_ids()
            if running:
                raise GitRefusal("controller has a non-terminal attempt")
            if self.semantic_claims.list() or self.path_claims.list():
                raise GitRefusal("controller has live claims")
            orders, invalid = self._load_work_orders()
            if invalid:
                raise GitRefusal("invalid work orders prevent automatic remote refresh")
            rebound: list[tuple[Path, str, str]] = []
            held: dict[str, str] = {}
            by_id = {order.id: order for order in orders}
            for path in sorted(self.config.work_orders_dir.glob("*.json")):
                raw = mapping(json.loads(path.read_text(encoding="utf-8")), "work order")
                order_id = str(raw.get("id", ""))
                order = by_id.get(order_id)
                if order is None:
                    raise GitRefusal(f"work order changed during remote refresh: {path.name}")
                if order.base_sha != old_sha:
                    held[order.id] = "work-order-subject-already-differs"
                    continue
                if not order.follow_remote_main:
                    held[order.id] = "follow-remote-main-disabled"
                    continue
                watched = tuple(dict.fromkeys((*order.path_claims, *order.authority_files)))
                if paths_changed(self.config.repository, old_sha, new_sha, watched):
                    held[order.id] = "watched-path-changed"
                    continue
                rebound.append((path, order.id, stable_hash(raw)))
            fast_forward(
                self.config.repository,
                branch=self.config.base_branch,
                new_sha=new_sha,
            )
            moved = True
            rebound_orders: list[str] = []
            rebind_refusals: dict[str, str] = {}
            for path, order_id, expected_hash in rebound:
                try:
                    self._write_rebound_order(
                        path,
                        old_sha=old_sha,
                        new_sha=new_sha,
                        expected_hash=expected_hash,
                    )
                    rebound_orders.append(order_id)
                except Exception as exc:
                    rebind_refusals[order_id] = f"{type(exc).__name__}: {exc}"
            self.journal.append(
                "fleet.base.fast-forwarded",
                {
                    "remote": self.config.remote_name,
                    "branch": self.config.base_branch,
                    "old_sha": old_sha,
                    "new_sha": new_sha,
                    "rebound_orders": rebound_orders,
                    "rebind_refusals": rebind_refusals,
                    "held_orders": held,
                },
                at=observed_at,
            )
        except Exception as exc:
            self.journal.append(
                "fleet.base.post-refresh-refused" if moved else "fleet.base.refresh-refused",
                {
                    "remote": self.config.remote_name,
                    "branch": self.config.base_branch,
                    "old_sha": old_sha,
                    "new_sha": new_sha,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                at=observed_at,
            )

    def observe(self) -> tuple[Observation, tuple[WorkOrder, ...]]:
        if not (self.config.repository / ".git").exists():
            raise ControllerError(f"configured repository is not a Git worktree: {self.config.repository}")
        head = current_sha(self.config.repository)
        remote_head: str | None = None
        remote_error: str | None = None
        if self.config.remote_head_required:
            try:
                remote_head = remote_branch_sha(
                    self.config.repository,
                    remote=self.config.remote_name,
                    branch=self.config.base_branch,
                )
            except Exception as exc:
                remote_error = f"{type(exc).__name__}: {exc}"
        orders, invalid = self._load_work_orders()
        claims = self.semantic_claims.list()
        observation = Observation(
            at=time.time(),
            repository=str(self.config.repository),
            head=head,
            remote_head=remote_head,
            remote_in_sync=(head == remote_head) if remote_head is not None else None,
            remote_error=remote_error,
            dirty=is_dirty(self.config.repository),
            work_orders=tuple(order.id for order in orders),
            invalid_work_orders=invalid,
            live_semantic_claims=tuple(asdict(claim) for claim in claims),
            route_subjects={route.id: route.subject_hash for route in self.config.routes},
        )
        self.journal.append("fleet.observed", asdict(observation), at=observation.at)
        return observation, orders

    def _active_order_ids(self) -> frozenset[str]:
        state: dict[str, str] = {}
        for row in self.journal.events(
            {
                "attempt.started",
                "attempt.ready",
                "attempt.refused",
                "attempt.failed",
                "attempt.cancelled",
                "attempt.admitted",
                "attempt.rejected",
                "attempt.reverted",
            }
        ):
            fact = row.get("fact")
            if not isinstance(fact, Mapping):
                continue
            order_id = fact.get("order_id")
            if not isinstance(order_id, str):
                continue
            kind = str(row.get("kind"))
            if kind == "attempt.refused" and fact.get("retryable_route_failure") is True:
                state[order_id] = "retryable"
            else:
                state[order_id] = kind
        held = {
            "attempt.started",
            "attempt.ready",
            "attempt.admitted",
            "attempt.refused",
            "attempt.failed",
            "attempt.cancelled",
            "attempt.rejected",
            "attempt.reverted",
        }
        return frozenset(order_id for order_id, kind in state.items() if kind in held)

    def _attempt_index(self, order_id: str) -> int:
        return sum(
            1
            for row in self.journal.events({"attempt.started"})
            if isinstance(row.get("fact"), Mapping)
            and row["fact"].get("order_id") == order_id
        )

    def plan(self, observation: Observation, orders: Sequence[WorkOrder], routes: Sequence[Route]) -> Plan:
        remote_reasons: tuple[str, ...] = ()
        if self.config.remote_head_required:
            if observation.remote_error is not None or observation.remote_head is None:
                remote_reasons = ("remote-head-unavailable",)
            elif observation.remote_head != observation.head:
                remote_reasons = ("remote-head-mismatch",)
        if remote_reasons:
            plan = build_plan(
                base_sha=observation.head,
                orders=(),
                routes=routes,
                max_assignments=self.config.max_assignments,
                now=observation.at,
                route_performance=route_factors(self.journal),
            )
            plan = Plan(
                observed_at=plan.observed_at,
                base_sha=plan.base_sha,
                assignments=(),
                rejections=tuple(Rejection(order.id, None, remote_reasons) for order in orders),
            )
            self.journal.append(
                "fleet.proposed",
                {
                    "mode": self.config.mode,
                    "base_sha": plan.base_sha,
                    "remote_head": observation.remote_head,
                    "assignments": [],
                    "rejections": [asdict(rejection) for rejection in plan.rejections],
                },
                at=observation.at,
            )
            return plan
        active_ids = self._active_order_ids()
        available = tuple(order for order in orders if order.id not in active_ids)
        duplicate_rejections = tuple(
            Rejection(order.id, None, ("attempt-already-active",))
            for order in orders
            if order.id in active_ids
        )
        plan = build_plan(
            base_sha=observation.head,
            orders=available,
            routes=routes,
            max_assignments=self.config.max_assignments,
            now=observation.at,
            route_performance=route_factors(self.journal),
        )
        if duplicate_rejections:
            plan = Plan(
                observed_at=plan.observed_at,
                base_sha=plan.base_sha,
                assignments=plan.assignments,
                rejections=plan.rejections + duplicate_rejections,
            )
        self.journal.append(
            "fleet.proposed",
            {
                "mode": self.config.mode,
                "base_sha": plan.base_sha,
                "assignments": [
                    {
                        "order_id": assignment.order.id,
                        "route_id": assignment.route.id,
                        "score": assignment.score,
                        "reason": assignment.reason,
                    }
                    for assignment in plan.assignments
                ],
                "rejections": [asdict(rejection) for rejection in plan.rejections],
            },
            at=observation.at,
        )
        return plan

    @staticmethod
    def _authority_section(repository: Path, path: str) -> str:
        source = repository / path
        if not source.is_file():
            raise ControllerError(f"authority file is absent: {path}")
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        raw.decode("utf-8")
        return f"- `{path}` — SHA-256 `{digest}`, {len(raw)} bytes\n"

    def _prompt(self, assignment: Assignment, worktree: Path) -> Path:
        order = assignment.order
        route = assignment.route
        sections = [
            "# Bounded IDOL and LIVE fleet work order\n",
            f"Work order: `{order.id}`\n",
            f"Task: `{order.task_id}`\n",
            f"Issue/provenance: `{order.issue or 'not-specified'}`\n",
            f"Exact base SHA: `{order.base_sha}`\n",
            f"Role: `{order.role}`\n",
            f"Route identity: `{route.provider}/{route.model}` via `{route.runtime}`\n",
            f"Required outcome: {order.required_outcome}\n",
            "\n## Non-negotiable boundaries\n",
            "- Treat the authority manifest and the checked-out exact SHA as the subject. Read each named authority file from this worktree before editing; do not substitute memory or another checkout.\n",
            "- Acquire no new path or semantic scope.\n",
            "- Do not infer semantic identity from names, paths, source spelling, AST tags, opcodes, hashes, or physical representation.\n",
            "- Do not restore retired host namespaces, sentinel outcomes, duplicate catalogs, or parallel semantic authorities.\n",
            "- Stop rather than choose between multiple lawful semantic designs.\n",
            "- Do not merge, rewrite history, clean another worktree, spend pay-go credits, redeem resets, or change provider configuration.\n",
            f"- You may edit only: {', '.join(order.path_claims)}.\n",
            f"- Semantic claims: {', '.join(order.semantic_claims)}.\n",
            "\n## Stop conditions\n",
            *[f"- {condition}\n" for condition in order.stop_conditions],
            "\n## Required witnesses\n",
            *[f"- `{json.dumps(command)}`\n" for command in order.witnesses],
            "\nThe controller will independently reject any out-of-claim edit, stale base, failed witness, provider/model mismatch, or missing evidence.\n",
            "\n## Authority manifest\n",
        ]
        for path in order.authority_files:
            sections.append(self._authority_section(worktree, path))
        prompt_dir = self.config.state_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = prompt_dir / f"{order.id}-{int(time.time())}.md"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(sections))
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _run_witnesses(
        self,
        order: WorkOrder,
        worktree: Path,
        *,
        renew_claims,
    ) -> tuple[Mapping[str, Any], ...]:
        rows: list[Mapping[str, Any]] = []
        for command in order.witnesses:
            renew_claims()
            rendered = [
                part.replace("{worktree}", str(worktree)).replace("{repository}", str(worktree))
                for part in command
            ]
            started = time.time()
            result = subprocess.run(
                rendered,
                cwd=worktree,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.config.witness_timeout_seconds,
                check=False,
            )
            output = result.stdout[:1_000_000]
            row = {
                "command": rendered,
                "returncode": result.returncode,
                "started_at": started,
                "ended_at": time.time(),
                "output_hash": hashlib.sha256(output.encode()).hexdigest(),
                "output_tail": output[-4000:],
            }
            rows.append(row)
            self.journal.append("attempt.witnessed", {"order_id": order.id, **row})
            if result.returncode != 0:
                raise ControllerError(f"witness failed for {order.id}: {rendered!r}")
        return tuple(rows)

    def _pr_body(self, assignment: Assignment, commit: str, witness_rows: Sequence[Mapping[str, Any]]) -> Path:
        order = assignment.order
        path = self.config.state_dir / "handoffs" / f"{order.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = [
            "## Fleet attempt handoff\n\n",
            f"- Work order: `{order.id}`\n",
            f"- Task: `{order.task_id}`\n",
            f"- Base: `{order.base_sha}`\n",
            f"- Candidate: `{commit}`\n",
            f"- Implementer route: `{assignment.route.provider}/{assignment.route.model}`\n",
            f"- Claimed paths: `{', '.join(order.path_claims)}`\n",
            f"- Semantic claims: `{', '.join(order.semantic_claims)}`\n",
            "\n### Required outcome\n\n",
            order.required_outcome,
            "\n\n### Witnesses\n\n",
        ]
        for row in witness_rows:
            text.append(f"- `{json.dumps(row['command'])}` — exit `{row['returncode']}`, output hash `{row['output_hash']}`\n")
        text.extend(
            (
                "\n### Admission boundary\n\n",
                "This is a draft handoff, not admission. An independent reviewer from the required provider family must reach a terminal verdict, and integrated-head gates must pass. The controller never merges its own attempt.\n",
            )
        )
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(text))
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def dispatch(self, assignment: Assignment) -> Mapping[str, Any]:
        order = assignment.order
        route = assignment.route
        assert_order_route(order, route)
        require_exact_subject(self.config.repository, order.base_sha)
        if is_dirty(self.config.repository):
            raise ControllerError("apply refuses a dirty authority worktree")
        if self.config.remote_head_required:
            remote_sha = remote_branch_sha(
                self.config.repository,
                remote=self.config.remote_name,
                branch=self.config.base_branch,
            )
            if remote_sha != order.base_sha:
                raise GitRefusal(
                    f"remote subject moved: work order is {order.base_sha}, "
                    f"{self.config.remote_name}/{self.config.base_branch} is {remote_sha}"
                )
        owner = f"fleet-{stable_hash({'route': route.id, 'order': order.id})[:16]}"
        work_item = f"work-{stable_hash(order.task_id)[:16]}"
        repository_subject = stable_hash(str(self.config.repository.resolve()))[:16]
        path_targets = tuple(f"{repository_subject}/{path}" for path in order.path_claims)
        attempt_index = self._attempt_index(order.id)
        attempt_id = stable_hash(
            {
                "order": order.id,
                "task": order.task_id,
                "base": order.base_sha,
                "route": route.subject_hash,
                "attempt_index": attempt_index,
            }
        )[:20]
        branch = order.branch if attempt_index == 0 else f"{order.branch}-r{attempt_index}"
        worktree = self.config.state_dir / "worktrees" / attempt_id
        fact = {
            "attempt_id": attempt_id,
            "order_id": order.id,
            "task_id": order.task_id,
            "route_id": route.id,
            "base_sha": order.base_sha,
            "branch": branch,
            "worktree": str(worktree),
        }
        self.journal.append("attempt.started", fact)
        semantic_acquired = False
        paths_acquired = False
        try:
            self.semantic_claims.acquire(
                owner=owner,
                task_id=order.task_id,
                targets=order.semantic_claims,
                ttl_seconds=self.config.claim_ttl_seconds,
            )
            semantic_acquired = True
            self.path_claims.acquire(
                owner=owner,
                task_id=work_item,
                targets=path_targets,
                ttl_seconds=self.config.claim_ttl_seconds,
            )
            paths_acquired = True
            with RepositoryClaimTransaction(
                repository=self.config.repository,
                owner=owner,
                task_id=work_item,
                paths=order.path_claims,
                ttl_seconds=self.config.claim_ttl_seconds,
                required=self.config.repository_claim_required,
            ) as repository_claims:
                create_worktree(
                    repository=self.config.repository,
                    path=worktree,
                    branch=branch,
                    base_sha=order.base_sha,
                )
                prompt = self._prompt(assignment, worktree)
                try:
                    result = self.runtime.execute(
                        route=route,
                        order=order,
                        prompt_path=prompt,
                        cwd=worktree,
                    )
                except RuntimeRefusal as exc:
                    record_failure(
                        self.journal,
                        route,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    raise
                record_success(self.journal, route)
                self.journal.append(
                    "attempt.executed",
                    {
                        **fact,
                        "provider": result.provider,
                        "model": result.model,
                        "provider_family": route.provider_family,
                        "status": result.status,
                        "session_id": result.session_id,
                        "usage": dict(result.usage),
                        "cost_usd": result.cost_usd,
                        "stdout_hash": hashlib.sha256(result.stdout.encode()).hexdigest(),
                        "stderr_hash": hashlib.sha256(result.stderr.encode()).hexdigest(),
                    },
                )
                paths = require_claimed_changes(worktree, order.path_claims)
                def renew_claims() -> None:
                    self.semantic_claims.renew(
                        owner=owner,
                        task_id=order.task_id,
                        ttl_seconds=self.config.claim_ttl_seconds,
                    )
                    self.path_claims.renew(
                        owner=owner,
                        task_id=work_item,
                        ttl_seconds=self.config.claim_ttl_seconds,
                    )
                    repository_claims.renew()

                witness_rows = self._run_witnesses(
                    order,
                    worktree,
                    renew_claims=renew_claims,
                )
                paths = require_claimed_changes(worktree, order.path_claims)
                commit = commit_claimed(
                    repository=worktree,
                    paths=paths,
                    message=f"fleet: {order.task_id} ({order.id})",
                    author_name=self.config.author_name,
                    author_email=self.config.author_email,
                )
                pr_url: str | None = None
                if order.publish_branch:
                    publish_branch(worktree, branch)
                    if order.create_draft_pr:
                        body = self._pr_body(assignment, commit, witness_rows)
                        pr_url = create_draft_pull_request(
                            repository=worktree,
                            branch=branch,
                            base=self.config.base_branch,
                            title=f"fleet: {order.task_id}",
                            body_path=body,
                        )
                ready = {
                    **fact,
                    "commit": commit,
                    "paths": paths,
                    "witnesses": witness_rows,
                    "pull_request_url": pr_url,
                    "worktree_preserved": True,
                }
                self.journal.append("attempt.ready", ready)
                return ready
        except Exception as exc:
            refused = {
                **fact,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "retryable_route_failure": isinstance(exc, RuntimeRefusal),
                "worktree_preserved": worktree.exists(),
            }
            self.journal.append("attempt.refused", refused)
            return refused
        finally:
            if semantic_acquired:
                self.semantic_claims.release(owner=owner, task_id=order.task_id)
            if paths_acquired:
                self.path_claims.release(owner=owner, task_id=work_item)

    def dispatch_plan(self, plan: Plan) -> tuple[Mapping[str, Any], ...]:
        if not plan.assignments:
            return ()
        if len(plan.assignments) == 1:
            return (self.dispatch(plan.assignments[0]),)
        with ThreadPoolExecutor(
            max_workers=min(len(plan.assignments), self.config.max_assignments),
            thread_name_prefix="idol-fleet",
        ) as executor:
            return tuple(executor.map(self.dispatch, plan.assignments))

    def run_once(self) -> CycleResult:
        with ControllerLease(self.lease_path):
            self.reconcile_expired_attempts()
            self.refresh_remote_base()
            observation, orders = self.observe()
            routes = self._routes(now=observation.at)
            plan = self.plan(observation, orders, routes)
            attempts: list[Mapping[str, Any]] = []
            if self.config.mode == "apply":
                attempts.extend(self.dispatch_plan(plan))
            result = CycleResult(
                mode=self.config.mode,
                observation=observation,
                plan=plan,
                attempts=tuple(attempts),
            )
            self.journal.append(
                "fleet.cycle.completed",
                {
                    "mode": result.mode,
                    "base_sha": result.observation.head,
                    "assignment_count": len(result.plan.assignments),
                    "attempt_count": len(result.attempts),
                },
            )
            return result

    def serve(self) -> None:
        consecutive_failures = 0
        while True:
            started = time.monotonic()
            try:
                self.run_once()
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                self.journal.append(
                    "fleet.cycle.failed",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "consecutive_failures": consecutive_failures,
                    },
                )
                if consecutive_failures >= self.config.max_consecutive_cycle_failures:
                    raise
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, self.config.interval_seconds - elapsed))

    def status(self) -> Mapping[str, Any]:
        from .health import circuit_state

        rows = self.journal.verify()
        latest = rows[-1] if rows else None
        now = time.time()
        factors = route_factors(self.journal)
        circuits = {route.id: circuit_state(self.journal, route) for route in self.config.routes}
        return {
            "mode": self.config.mode,
            "repository": str(self.config.repository),
            "state_dir": str(self.config.state_dir),
            "journal_events": len(rows),
            "latest": latest,
            "config_hash": config_hash(self.raw_config),
            "routes": [
                {
                    "id": route.id,
                    "provider": route.provider,
                    "model": route.model,
                    "billing": route.billing.value,
                    "configured_enabled": route.enabled,
                    "subject_hash": route.subject_hash,
                    "performance_factor": factors.get(route.id, 1.0),
                    "circuit": asdict(circuits[route.id]),
                    "circuit_open": circuits[route.id].open(now),
                }
                for route in self.config.routes
            ],
        }
