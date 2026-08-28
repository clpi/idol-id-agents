from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from fleet_control.model import (
    AllowanceWindow,
    BillingClass,
    BillingProof,
    Route,
    WorkOrder,
)
from fleet_control.scheduler import build_plan, orders_conflict, path_overlap, semantic_overlap


class SchedulerTests(unittest.TestCase):
    now = 1_000_000.0

    def route(self, route_id: str, *, family: str = "local", premium: bool = False) -> Route:
        route = Route(
            id=route_id,
            provider=family,
            model=f"{family}-model",
            provider_family=family,
            runtime="plain",
            command=("python3", "fake.py"),
            parser="plain-json",
            billing=BillingClass.LOCAL,
            proof=BillingProof(
                kind="local-process",
                subject_hash="pending",
                observed_at=self.now - 10,
                expires_at=self.now + 10_000,
                evidence_hash="e",
                trusted=True,
            ),
            roles=frozenset({"mechanic", "reviewer", "architect"}),
            premium=premium,
            allowance=(
                AllowanceWindow(
                    label="session",
                    remaining_fraction=0.9,
                    resets_at=self.now + 1800,
                ),
            ),
        )
        return replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))

    def order(
        self,
        order_id: str,
        *,
        path: str = "src/a.zig",
        semantic: str = "graph/application",
        role: str = "mechanic",
        route_ids: tuple[str, ...] = ("cheap",),
        priority: int = 50,
        reviewer_family: str | None = None,
    ) -> WorkOrder:
        return WorkOrder(
            id=order_id,
            task_id=f"task-{order_id}",
            repository=Path("/tmp/idol"),
            base_sha="a" * 40,
            branch=f"fleet/{order_id}",
            role=role,
            required_outcome="bounded result",
            path_claims=(path,),
            semantic_claims=(semantic,),
            stop_conditions=("stop",),
            witnesses=(("true",),),
            route_ids=route_ids,
            authority_files=("docs/spec/law.md",),
            risk="low",
            priority=priority,
            estimated_seconds=300,
            estimated_tokens=1000,
            reviewer_family=reviewer_family,
        )

    def test_hierarchical_path_overlap(self) -> None:
        self.assertTrue(path_overlap("src", "src/sema.zig"))
        self.assertFalse(path_overlap("src", "lib"))

    def test_hierarchical_semantic_overlap(self) -> None:
        self.assertTrue(semantic_overlap("world/process", "world/process/run"))
        self.assertFalse(semantic_overlap("world/process", "graph/application"))

    def test_order_conflict_uses_both_claim_layers(self) -> None:
        left = self.order("a")
        right = self.order("b", path="lib/x.id", semantic="graph/application/result")
        self.assertTrue(orders_conflict(left, right))

    def test_stale_order_is_rejected(self) -> None:
        plan = build_plan(
            base_sha="b" * 40,
            orders=(self.order("a"),),
            routes=(self.route("cheap"),),
            now=self.now,
        )
        self.assertFalse(plan.assignments)
        self.assertIn("stale-base-sha", plan.rejections[0].reasons)

    def test_conflicting_orders_are_not_selected_together(self) -> None:
        plan = build_plan(
            base_sha="a" * 40,
            orders=(self.order("a"), self.order("b")),
            routes=(self.route("cheap"),),
            max_assignments=2,
            now=self.now,
        )
        self.assertEqual(len(plan.assignments), 1)

    def test_reviewer_family_must_be_independent(self) -> None:
        order = self.order(
            "review",
            role="reviewer",
            route_ids=("same",),
            reviewer_family="openai",
        )
        plan = build_plan(
            base_sha="a" * 40,
            orders=(order,),
            routes=(self.route("same", family="openai"),),
            now=self.now,
        )
        self.assertFalse(plan.assignments)
        self.assertIn("reviewer-family-not-independent", plan.rejections[0].reasons)

    def test_low_risk_mechanical_work_prefers_nonpremium_route(self) -> None:
        order = self.order("mechanic", route_ids=("cheap", "premium"))
        plan = build_plan(
            base_sha="a" * 40,
            orders=(order,),
            routes=(self.route("cheap"), self.route("premium", family="openai", premium=True)),
            now=self.now,
        )
        self.assertEqual(plan.assignments[0].route.id, "cheap")

    def test_task_that_cannot_finish_before_reset_is_rejected(self) -> None:
        order = replace(self.order("long"), estimated_seconds=5000)
        plan = build_plan(
            base_sha="a" * 40,
            orders=(order,),
            routes=(self.route("cheap"),),
            now=self.now,
        )
        self.assertFalse(plan.assignments)
        self.assertIn("cannot-finish-before-any-reset", plan.rejections[0].reasons)


if __name__ == "__main__":
    unittest.main()
