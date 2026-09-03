from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, Sequence

from .claims import ControllerLease
from .controller import CycleResult, FleetController, Observation
from .inventory import (
    InventoryConfig,
    InventoryObservation,
    SessionFact,
    cancel_session,
    inventory_fact,
    load_adoptions,
    observe_inventory,
)
from .model import Route, WorkOrder, stable_hash
from .scheduler import Plan, Rejection


class ManagedFleetController(FleetController):
    """FleetController plus metadata-only live session reconciliation."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        raw_inventory = self.raw_config.get("inventory")
        if raw_inventory is not None and not isinstance(raw_inventory, Mapping):
            raise ValueError("inventory configuration must be an object")
        self.inventory_config = InventoryConfig.from_mapping(
            raw_inventory,
            state_dir=self.config.state_dir,
        )
        self.inventory_observation: InventoryObservation | None = None
        self.inventory_error: str | None = None

    def observe(self) -> tuple[Observation, tuple[WorkOrder, ...]]:
        observation, orders = super().observe()
        try:
            inventory = observe_inventory(self.inventory_config, now=observation.at)
            self.inventory_observation = inventory
            self.inventory_error = None
            if inventory is not None:
                fact = inventory_fact(inventory)
                self.journal.append(
                    "fleet.inventory.observed",
                    {**fact, "digest": stable_hash(fact)},
                    at=observation.at,
                )
        except Exception as exc:
            self.inventory_observation = None
            self.inventory_error = f"{type(exc).__name__}: {exc}"
            self.journal.append(
                "fleet.inventory.refused",
                {"error_type": type(exc).__name__, "error": str(exc)},
                at=observation.at,
            )
        return observation, orders

    def _external_active(self) -> tuple[frozenset[str], frozenset[str]]:
        if self.inventory_observation is None:
            return frozenset(), frozenset()
        order_ids = {
            session.order_id
            for session in self.inventory_observation.sessions
            if not session.terminal and session.order_id
        }
        task_ids = {
            session.task_id
            for session in self.inventory_observation.sessions
            if not session.terminal and session.task_id
        }
        return frozenset(order_ids), frozenset(task_ids)

    def plan(self, observation: Observation, orders: Sequence[WorkOrder], routes: Sequence[Route]) -> Plan:
        if self.inventory_config.enabled and self.inventory_observation is None:
            plan = super().plan(observation, (), routes)
            refused = Plan(
                observed_at=plan.observed_at,
                base_sha=plan.base_sha,
                assignments=(),
                rejections=plan.rejections + tuple(
                    Rejection(order.id, None, ("live-inventory-unavailable",))
                    for order in orders
                ),
            )
            self.journal.append(
                "fleet.proposal.reconciled",
                {
                    "base_sha": observation.head,
                    "blocked_orders": [order.id for order in orders],
                    "reason": "live-inventory-unavailable",
                    "inventory_error": self.inventory_error,
                },
                at=observation.at,
            )
            return refused
        unidentified = tuple(
            session
            for session in self.inventory_observation.sessions
            if not session.terminal and not session.order_id and not session.task_id
        ) if self.inventory_observation is not None else ()
        if unidentified:
            plan = super().plan(observation, (), routes)
            refused = Plan(
                observed_at=plan.observed_at,
                base_sha=plan.base_sha,
                assignments=(),
                rejections=plan.rejections + tuple(
                    Rejection(order.id, None, ("unidentified-live-session",))
                    for order in orders
                ),
            )
            self.journal.append(
                "fleet.proposal.reconciled",
                {
                    "base_sha": observation.head,
                    "blocked_orders": [order.id for order in orders],
                    "reason": "unidentified-live-session",
                    "session_ids": [session.id for session in unidentified],
                },
                at=observation.at,
            )
            return refused
        external_orders, external_tasks = self._external_active()
        blocked = tuple(
            order
            for order in orders
            if order.id in external_orders or order.task_id in external_tasks
        )
        remaining = tuple(order for order in orders if order not in blocked)
        plan = super().plan(observation, remaining, routes)
        if not blocked:
            return plan
        rejections = plan.rejections + tuple(
            Rejection(order.id, None, ("live-session-already-covers-task",))
            for order in blocked
        )
        reconciled = Plan(
            observed_at=plan.observed_at,
            base_sha=plan.base_sha,
            assignments=plan.assignments,
            rejections=rejections,
        )
        self.journal.append(
            "fleet.proposal.reconciled",
            {
                "base_sha": observation.head,
                "blocked_orders": [order.id for order in blocked],
                "reason": "live-session-already-covers-task",
            },
            at=observation.at,
        )
        return reconciled

    def _attempt_state(self) -> tuple[Mapping[str, Mapping[str, Any]], frozenset[str], frozenset[str]]:
        attempts: dict[str, Mapping[str, Any]] = {}
        terminal_attempts: set[str] = set()
        terminal_tasks: set[str] = set()
        terminal_kinds = {
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
            task_id = fact.get("task_id")
            if isinstance(attempt_id, str):
                attempts[attempt_id] = fact
                if row.get("kind") in terminal_kinds:
                    terminal_attempts.add(attempt_id)
            if isinstance(task_id, str) and row.get("kind") in terminal_kinds:
                terminal_tasks.add(task_id)
        return attempts, frozenset(terminal_attempts), frozenset(terminal_tasks)

    def reconcile_sessions(self, *, head: str, now: float) -> tuple[Mapping[str, Any], ...]:
        inventory = self.inventory_observation
        if inventory is None:
            return ()
        attempts, terminal_attempts, terminal_tasks = self._attempt_state()
        try:
            adoptions = load_adoptions(self.inventory_config.adoptions_file)
        except Exception as exc:
            self.journal.append(
                "fleet.session.adoptions.refused",
                {"error_type": type(exc).__name__, "error": str(exc)},
                at=now,
            )
            adoptions = {}
        actions: list[Mapping[str, Any]] = []
        for session in inventory.sessions:
            if session.terminal:
                continue
            adoption = adoptions.get(session.id)
            controller_owned = bool(session.attempt_id and session.attempt_id in attempts)
            adopted = bool(adoption and adoption.approved)
            managed = controller_owned or adopted
            if not managed:
                fact = {
                    "session_id": session.id,
                    "order_id": session.order_id,
                    "task_id": session.task_id,
                    "status": session.status,
                    "verdict": "unmanaged-observe-only",
                }
                self.journal.append("fleet.session.unmanaged", fact, at=now)
                actions.append(fact)
                continue
            expected_base = (
                attempts.get(session.attempt_id or "", {}).get("base_sha")
                if controller_owned
                else adoption.base_sha if adoption else None
            )
            expected_task = (
                attempts.get(session.attempt_id or "", {}).get("task_id")
                if controller_owned
                else adoption.task_id if adoption else None
            )
            reasons: list[str] = []
            if expected_base and expected_base != head:
                reasons.append("repository-subject-moved")
            if session.base_sha and session.base_sha != head:
                reasons.append("session-base-stale")
            if session.attempt_id and session.attempt_id in terminal_attempts:
                reasons.append("attempt-terminal")
            if expected_task and expected_task in terminal_tasks:
                reasons.append("task-terminal")
            if not reasons:
                continue
            fact: dict[str, Any] = {
                "session_id": session.id,
                "attempt_id": session.attempt_id,
                "order_id": session.order_id or (adoption.order_id if adoption else None),
                "task_id": session.task_id or expected_task,
                "managed_by": "controller" if controller_owned else "explicit-adoption",
                "reasons": tuple(dict.fromkeys(reasons)),
            }
            if self.config.mode == "apply" and self.inventory_config.cancel_owned_sessions:
                try:
                    outcome = cancel_session(
                        self.inventory_config,
                        session,
                        attempt_id=session.attempt_id,
                    )
                    fact["outcome"] = outcome
                    self.journal.append("fleet.session.cancelled", fact, at=now)
                except Exception as exc:
                    fact["error_type"] = type(exc).__name__
                    fact["error"] = str(exc)
                    self.journal.append("fleet.session.cancel.refused", fact, at=now)
            else:
                fact["outcome"] = "cancel-proposed"
                self.journal.append("fleet.session.cancel.proposed", fact, at=now)
            actions.append(fact)
        return tuple(actions)

    def run_once(self) -> CycleResult:
        with ControllerLease(self.lease_path):
            self.reconcile_expired_attempts()
            observation, orders = self.observe()
            self.reconcile_sessions(head=observation.head, now=observation.at)
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
                    "inventory_observed": self.inventory_observation is not None,
                },
            )
            return result
