from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "allowance_plan", ROOT / "scripts" / "allowance_plan.py"
)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)

SHA = "a" * 40
NOW = "2026-08-28T05:00:00Z"


def provider(
    identity: str = "local",
    *,
    cost_class: str = "local",
    family: str = "local-family",
    source: str = "local_telemetry",
    model_verified: bool = True,
) -> dict:
    item = {
        "id": identity,
        "provider": identity,
        "family": family,
        "model": f"{identity}-exact-model",
        "model_verified": model_verified,
        "cost_class": cost_class,
        "enabled": True,
        "premium": False,
        "max_parallel": 4,
        "quality": {
            "implementation": 0.9,
            "observer": 0.9,
            "default": 0.8,
        },
        "telemetry_source": source,
        "windows": [],
    }
    if cost_class not in planner.FREE:
        item["windows"] = [
            {
                "name": "weekly",
                "remaining_tokens": 100000,
                "remaining_percent": 50,
                "reset_at": "2026-08-30T05:00:00Z",
                "period_seconds": 604800,
                "source": source,
            }
        ]
    return item


def task(
    identity: str = "task",
    *,
    path: str = "lib/a.id",
    boundary: str = "graph",
) -> dict:
    return {
        "id": identity,
        "state": "productive_ready",
        "blocked": False,
        "base_sha": SHA,
        "work_order_sha": SHA,
        "live_claim_verified": True,
        "role": "implementation",
        "value": 100,
        "estimated_tokens": 1000,
        "estimated_minutes": 10,
        "completion_probability": 0.9,
        "evidence_factor": 1.0,
        "minimum_role_fit": 0.5,
        "review_required": False,
        "evidence_path": "repo#1",
        "stop_conditions": ["HEAD changed"],
        "paths": [path],
        "semantic_boundaries": [boundary],
    }


def payload(
    *,
    providers: list[dict] | None = None,
    tasks: list[dict] | None = None,
) -> dict:
    return {
        "schema": "idol.allowance.input.v1",
        "observed_at": NOW,
        "current_sha": SHA,
        "paygo_approved": False,
        "providers": providers if providers is not None else [provider()],
        "tasks": tasks if tasks is not None else [task()],
    }


class PlannerTests(unittest.TestCase):
    def test_live_exact_evidence_can_be_execution_ready_without_auto_dispatch(
        self,
    ) -> None:
        result = planner.plan(payload())
        self.assertFalse(result["automatic_dispatch"])
        self.assertTrue(result["execution_ready"])
        self.assertEqual(result["assignments"][0]["execution_blockers"], [])

    def test_example_or_estimated_telemetry_never_becomes_execution_ready(
        self,
    ) -> None:
        result = planner.plan(
            payload(providers=[provider(source="example-not-live")])
        )
        self.assertEqual(len(result["assignments"]), 1)
        self.assertFalse(result["execution_ready"])
        self.assertIn(
            "telemetry-not-live:example-not-live",
            result["assignments"][0]["execution_blockers"],
        )

    def test_missing_token_estimate_is_rejected_instead_of_becoming_one_token(
        self,
    ) -> None:
        item = task()
        item.pop("estimated_tokens")
        result = planner.plan(payload(tasks=[item]))
        self.assertEqual(result["assignments"], [])
        self.assertEqual(
            result["rejected"][0]["reason"], "missing-token-estimate"
        )

    def test_paygo_approval_must_be_a_json_boolean(self) -> None:
        value = payload(providers=[provider(cost_class="paygo")])
        value["paygo_approved"] = "false"
        with self.assertRaisesRegex(ValueError, "JSON boolean"):
            planner.plan(value)

    def test_paygo_is_rejected_without_explicit_approval(self) -> None:
        value = payload(providers=[provider(cost_class="paygo")])
        result = planner.plan(value)
        self.assertEqual(result["assignments"], [])
        reasons = result["rejected"][0]["provider_reasons"]
        self.assertEqual(reasons["local"], "paygo-forbidden")

    def test_ancestor_and_descendant_paths_conflict(self) -> None:
        broad = task("broad", path="lib", boundary="one")
        narrow = task(
            "narrow",
            path="lib/compiler/parser.id",
            boundary="two",
        )
        result = planner.plan(payload(tasks=[broad, narrow]))
        self.assertEqual(len(result["assignments"]), 1)
        self.assertTrue(
            any(
                item["reason"].startswith("path-conflict:")
                for item in result["rejected"]
            )
        )

    def test_duplicate_provider_and_task_identities_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate provider"):
            planner.plan(payload(providers=[provider(), provider()]))
        with self.assertRaisesRegex(ValueError, "duplicate task"):
            planner.plan(payload(tasks=[task(), task()]))

    def test_reviewer_family_cannot_equal_implementer_family(self) -> None:
        item = task()
        item.update(
            {
                "requires_different_family": True,
                "implementer_family": "local-family",
            }
        )
        result = planner.plan(payload(tasks=[item]))
        self.assertEqual(result["assignments"], [])
        self.assertEqual(
            result["rejected"][0]["provider_reasons"]["local"],
            "reviewer-family-not-independent",
        )

    def test_window_must_fit_tokens_and_time(self) -> None:
        remote = provider(cost_class="subscription_included")
        remote["windows"][0]["remaining_tokens"] = 999
        result = planner.plan(payload(providers=[remote]))
        self.assertEqual(result["assignments"], [])
        self.assertEqual(
            result["rejected"][0]["provider_reasons"]["local"],
            "no-window-can-finish",
        )

    def test_head_drift_rejects_task(self) -> None:
        item = task()
        item["base_sha"] = "b" * 40
        result = planner.plan(payload(tasks=[item]))
        self.assertEqual(result["assignments"], [])
        self.assertEqual(
            result["rejected"][0]["reason"], "base-sha-mismatch"
        )

    def test_missing_claim_or_work_order_is_visible_not_inferred(self) -> None:
        item = task()
        item["live_claim_verified"] = False
        item["work_order_sha"] = None
        result = planner.plan(payload(tasks=[item]))
        blockers = result["assignments"][0]["execution_blockers"]
        self.assertIn("live-claim-unverified", blockers)
        self.assertIn("work-order-sha-unverified", blockers)
        self.assertFalse(result["execution_ready"])

    def test_model_identity_must_be_present_and_verified(self) -> None:
        missing = provider()
        missing["model"] = ""
        result = planner.plan(payload(providers=[missing]))
        self.assertEqual(result["assignments"], [])
        self.assertEqual(
            result["rejected"][0]["provider_reasons"]["local"],
            "missing-exact-model",
        )

        unverified = provider(model_verified=False)
        result = planner.plan(payload(providers=[unverified]))
        self.assertFalse(result["assignments"][0]["execution_ready"])
        self.assertIn(
            "model-identity-unverified",
            result["assignments"][0]["execution_blockers"],
        )


if __name__ == "__main__":
    unittest.main()
