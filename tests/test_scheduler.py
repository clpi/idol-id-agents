from __future__ import annotations

from dataclasses import replace
from itertools import product
from pathlib import Path
import unittest

from fleet_control.model import (
    AllowanceWindow,
    BillingClass,
    BillingProof,
    Route,
    WorkOrder,
)
from fleet_control.scheduler import Plan, build_plan, orders_conflict, path_overlap, semantic_overlap


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

    def test_exhausted_primary_falls_through_to_healthy_route(self) -> None:
        primary = replace(
            self.route("grok"),
            allowance=(AllowanceWindow("session", 0.0, self.now + 1800),),
        )
        fallback = self.route("fallback", family="minimax")
        order = self.order("continue", route_ids=("grok", "fallback"))
        plan = build_plan(
            base_sha="a" * 40,
            orders=(order,),
            routes=(primary, fallback),
            now=self.now,
        )
        self.assertEqual(plan.assignments[0].route.id, "fallback")
        grok = next(row for row in plan.rejections if row.route_id == "grok")
        self.assertIn("allowance-exhausted", grok.reasons)

    def test_measured_productivity_changes_route_choice(self) -> None:
        order = self.order("efficient", route_ids=("first", "second"))
        plan = build_plan(
            base_sha="a" * 40,
            orders=(order,),
            routes=(self.route("first"), self.route("second")),
            route_performance={"first": 0.6, "second": 1.4},
            now=self.now,
        )
        self.assertEqual(plan.assignments[0].route.id, "second")

    def capacity_plan(
        self,
        orders: tuple[WorkOrder, ...],
        routes: tuple[Route, ...],
        *,
        route_active: dict[str, int] | None = None,
    ) -> Plan:
        return build_plan(
            base_sha="a" * 40,
            orders=orders,
            routes=routes,
            max_assignments=len(orders),
            now=self.now,
            route_active=route_active,
        )

    def test_flexible_order_does_not_starve_restricted_order(self) -> None:
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("cheap", "fallback"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b"),
            ),
            (self.route("cheap"), self.route("fallback")),
        )
        self.assertEqual(
            {item.order.id: item.route.id for item in plan.assignments},
            {"a": "fallback", "b": "cheap"},
        )
        self.assertAlmostEqual(plan.assignments[0].score, plan.assignments[1].score * 90 / 50)

    def test_rebalance_can_follow_multiple_route_hops(self) -> None:
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("r1", "r2"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b", route_ids=("r2", "r3"), priority=80),
                self.order("c", path="lib/c.id", semantic="world/c", route_ids=("r1",)),
            ),
            (self.route("r1"), self.route("r2"), self.route("r3")),
        )
        self.assertEqual(
            {item.order.id: item.route.id for item in plan.assignments},
            {"a": "r2", "b": "r3", "c": "r1"},
        )

    def test_active_route_capacity_is_not_moved_or_overbooked(self) -> None:
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("cheap", "fallback"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b"),
            ),
            (self.route("cheap"), self.route("fallback")),
            route_active={"fallback": 1},
        )
        self.assertEqual([(item.order.id, item.route.id) for item in plan.assignments], [("a", "cheap")])

    def test_rebalance_never_uses_rejected_routes(self) -> None:
        forbidden = replace(self.route("paygo"), billing=BillingClass.PAYGO)
        forbidden = replace(forbidden, proof=replace(forbidden.proof, subject_hash=forbidden.subject_hash))
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("cheap", "paygo"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b"),
            ),
            (self.route("cheap"), forbidden),
        )
        self.assertEqual([(item.order.id, item.route.id) for item in plan.assignments], [("a", "cheap")])
        self.assertTrue(any("billing-paygo-forbidden" in row.reasons for row in plan.rejections))

    def test_rebalance_respects_multi_slot_capacity(self) -> None:
        cheap = replace(self.route("cheap"), max_parallel=2)
        cheap = replace(cheap, proof=replace(cheap.proof, subject_hash=cheap.subject_hash))
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("cheap", "fallback"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b", priority=80),
                self.order("c", path="lib/c.id", semantic="world/c"),
            ),
            (cheap, self.route("fallback")),
            route_active={"cheap": 1},
        )
        self.assertEqual({item.order.id: item.route.id for item in plan.assignments}, {"a": "fallback", "b": "cheap"})

    def test_rebalance_fills_many_independent_provider_slots(self) -> None:
        routes = tuple(self.route(f"route-{index:03}") for index in range(101))
        orders = tuple(
            self.order(
                f"order-{index:03}",
                path=f"lib/{index}.id",
                semantic=f"world/{index}",
                route_ids=(routes[index].id, routes[index + 1].id),
                priority=90,
            )
            for index in range(100)
        ) + (self.order("restricted", path="lib/restricted.id", semantic="world/restricted", route_ids=(routes[0].id,)),)
        plan = self.capacity_plan(orders, routes)
        self.assertEqual(len(plan.assignments), len(orders))
        self.assertEqual(len({item.order.id for item in plan.assignments}), len(orders))
        self.assertEqual(len({item.route.id for item in plan.assignments}), len(routes))

    def test_full_route_cycles_leave_selected_assignments_unchanged(self) -> None:
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("r1", "r2"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b", route_ids=("r1", "r2"), priority=80),
                self.order("c", path="lib/c.id", semantic="world/c", route_ids=("r1", "r2")),
            ),
            (self.route("r1"), self.route("r2")),
        )
        self.assertEqual({item.order.id: item.route.id for item in plan.assignments}, {"a": "r1", "b": "r2"})
        self.assertTrue(any(row.order_id == "c" and "route-parallel-capacity-full" in row.reasons for row in plan.rejections))

    def test_rebalance_preserves_independent_reviewer_family(self) -> None:
        plan = self.capacity_plan(
            (
                self.order("a", role="reviewer", route_ids=("cheap", "same"), reviewer_family="openai", priority=90),
                self.order("b", path="lib/b.id", semantic="world/b"),
            ),
            (self.route("cheap"), self.route("same", family="openai")),
        )
        self.assertEqual([(item.order.id, item.route.id) for item in plan.assignments], [("a", "cheap")])
        self.assertTrue(any("reviewer-family-not-independent" in row.reasons for row in plan.rejections))

    def test_greedy_preferences_are_preserved_when_capacity_is_sufficient(self) -> None:
        plan = self.capacity_plan(
            (
                self.order("a", route_ids=("cheap", "premium"), priority=90),
                self.order("b", path="lib/b.id", semantic="world/b", route_ids=("cheap", "fallback")),
            ),
            (self.route("cheap"), self.route("fallback"), self.route("premium", premium=True)),
        )
        self.assertEqual({item.order.id: item.route.id for item in plan.assignments}, {"a": "cheap", "b": "fallback"})

    def test_small_independent_route_graphs_match_capacity_oracle(self) -> None:
        routes = tuple(self.route(f"r{index}") for index in range(3))
        choices = tuple(
            tuple(route.id for index, route in enumerate(routes) if mask & (1 << index))
            for mask in range(1, 8)
        )
        for allowed in product(choices, repeat=3):
            with self.subTest(allowed=allowed):
                orders = tuple(
                    self.order(f"o{index}", path=f"lib/{index}.id", semantic=f"world/{index}", route_ids=ids)
                    for index, ids in enumerate(allowed)
                )
                expected = max(
                    sum(route is not None for route in assignment)
                    for assignment in product(*((None,) + ids for ids in allowed))
                    if len([route for route in assignment if route is not None])
                    == len({route for route in assignment if route is not None})
                )
                plan = self.capacity_plan(orders, routes)
                self.assertEqual(len(plan.assignments), expected)
                self.assertEqual(len({item.order.id for item in plan.assignments}), expected)
                self.assertEqual(len({item.route.id for item in plan.assignments}), expected)
                for item in plan.assignments:
                    self.assertIn(item.route.id, item.order.route_ids)


if __name__ == "__main__":
    unittest.main()
