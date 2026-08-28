from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from idol_fleet.journal import Journal, JournalSecurityError
from idol_fleet.model import (
    AllowanceWindow,
    BillingClass,
    RepositoryPath,
    Route,
    Snapshot,
    Task,
    WorkOrder,
)
from idol_fleet.policy import Policy
from idol_fleet.process import run_command
from idol_fleet.scheduler import Scheduler
from idol_fleet.work_order import validate_work_order


class ModelTests(unittest.TestCase):
    def test_repository_path_refuses_parent_traversal(self) -> None:
        for value in ("../idol", "src/../law.md", "/tmp/x", "", "src//main.zig"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RepositoryPath(value)

    def test_repository_path_accepts_canonical_relative_path(self) -> None:
        self.assertEqual(str(RepositoryPath("src/semantic_graph.zig")), "src/semantic_graph.zig")

    def test_route_unknown_billing_is_not_included(self) -> None:
        route = Route(
            id="r",
            provider="x",
            model="m",
            runtime="openclaw",
            billing=BillingClass.UNKNOWN,
            proof="missing",
            roles=("observer",),
            max_concurrency=1,
        )
        self.assertFalse(route.included)

    def test_allowance_window_remaining_and_reset_fit(self) -> None:
        window = AllowanceWindow(label="session", remaining_fraction=0.25, reset_at=200.0)
        self.assertTrue(window.can_finish(now=100.0, estimated_seconds=90))
        self.assertFalse(window.can_finish(now=100.0, estimated_seconds=101))


class JournalTests(unittest.TestCase):
    def test_append_is_ordered_private_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            journal = Journal(path)
            journal.append({"id": "e1", "kind": "observed", "fact": {"sha": "a" * 40}})
            journal.append({"id": "e2", "kind": "planned", "fact": {"task": "t"}})
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual([event["id"] for event in journal.read()], ["e1", "e2"])
            with path.open("ab") as fh:
                fh.write(b'{"id":"partial"')
            self.assertEqual([event["id"] for event in journal.read()], ["e1", "e2"])

    def test_duplicate_event_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = Journal(Path(td) / "events.jsonl")
            event = {"id": "same", "kind": "observed", "fact": {}}
            journal.append(event)
            with self.assertRaises(ValueError):
                journal.append(event)

    def test_sensitive_or_transcript_fields_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = Journal(Path(td) / "events.jsonl")
            for key in ("api_key", "password", "prompt", "transcript", "reasoning", "content"):
                with self.subTest(key=key), self.assertRaises(JournalSecurityError):
                    journal.append({"id": f"e-{key}", "kind": "bad", "fact": {key: "secret"}})


class PolicyTests(unittest.TestCase):
    def policy(self, td: str) -> Policy:
        payload = {
            "version": 1,
            "mode": "observe-plan",
            "trusted_billing_proofs": ["native-subscription", "local-runtime"],
            "routes": [
                {
                    "id": "codex-sub",
                    "provider": "openai",
                    "model": "openai/gpt-5.6-sol",
                    "runtime": "openclaw-codex",
                    "billing": "included",
                    "proof": "native-subscription",
                    "roles": ["architect", "reviewer"],
                    "max_concurrency": 1,
                    "fallbacks": [],
                },
                {
                    "id": "ollama-local",
                    "provider": "ollama",
                    "model": "ollama/qwen3.5:9b",
                    "runtime": "openclaw",
                    "billing": "local",
                    "proof": "local-runtime",
                    "roles": ["observer", "mechanic", "evidence"],
                    "max_concurrency": 2,
                    "fallbacks": [],
                },
                {
                    "id": "openrouter-credit",
                    "provider": "openrouter",
                    "model": "anthropic/claude",
                    "runtime": "openclaw",
                    "billing": "purchased-credit",
                    "proof": "wallet",
                    "roles": ["implementer"],
                    "max_concurrency": 1,
                    "fallbacks": [],
                },
                {
                    "id": "fake-included",
                    "provider": "x",
                    "model": "x/y",
                    "runtime": "openclaw",
                    "billing": "included",
                    "proof": "untrusted-claim",
                    "roles": ["observer"],
                    "max_concurrency": 1,
                    "fallbacks": [],
                },
            ],
            "limits": {"global_editing": 1, "global_observer": 4},
        }
        path = Path(td) / "policy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return Policy.load(path)

    def test_only_local_or_proven_included_routes_are_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            policy = self.policy(td)
            self.assertTrue(policy.route_eligibility("codex-sub", "reviewer").eligible)
            self.assertTrue(policy.route_eligibility("ollama-local", "observer").eligible)
            self.assertFalse(policy.route_eligibility("openrouter-credit", "implementer").eligible)
            self.assertIn("billing-purchased-credit", policy.route_eligibility("openrouter-credit", "implementer").reasons)
            self.assertFalse(policy.route_eligibility("fake-included", "observer").eligible)
            self.assertIn("billing-proof-untrusted", policy.route_eligibility("fake-included", "observer").reasons)

    def test_role_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            policy = self.policy(td)
            result = policy.route_eligibility("codex-sub", "mechanic")
            self.assertFalse(result.eligible)
            self.assertIn("role-not-supported", result.reasons)


class ProcessTests(unittest.TestCase):
    def test_command_uses_argument_vector_and_captures_inner_status(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import sys; print('ok'); print('bad', file=sys.stderr); sys.exit(7)"],
            timeout=5,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "bad\n")
        self.assertFalse(result.timed_out)

    def test_shell_string_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            run_command("echo unsafe", timeout=1)  # type: ignore[arg-type]

    def test_timeout_is_terminal_and_bounded(self) -> None:
        started = time.monotonic()
        result = run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2, kill_grace=0.1)
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 3)

    def test_environment_is_allowlisted(self) -> None:
        old = os.environ.get("IDOL_FLEET_TEST_SECRET")
        os.environ["IDOL_FLEET_TEST_SECRET"] = "should-not-pass"
        try:
            result = run_command(
                [sys.executable, "-c", "import os; print(os.environ.get('IDOL_FLEET_TEST_SECRET', 'absent'))"],
                timeout=5,
                env={"SAFE": "1"},
            )
            self.assertEqual(result.stdout.strip(), "absent")
        finally:
            if old is None:
                os.environ.pop("IDOL_FLEET_TEST_SECRET", None)
            else:
                os.environ["IDOL_FLEET_TEST_SECRET"] = old


class WorkOrderTests(unittest.TestCase):
    def valid_order(self) -> WorkOrder:
        return WorkOrder(
            id="a1",
            task_id="t1",
            repository="clpi/idol",
            base_sha="a" * 40,
            branch="fleet/a1",
            role="mechanic",
            route_id="ollama-local",
            semantic_claims=("law.grammar.one",),
            path_claims=(RepositoryPath("docs/spec/grammar.md"),),
            goal="Remove duplicated prose counts without semantic changes.",
            required_outcome="One prose-only commit.",
            constraints=("No semantic source edits",),
            forbidden_repairs=("Do not weaken the gate",),
            witnesses=("sh gate/gap-145-consumer.sh",),
            stop_conditions=("Any semantic diff",),
            estimated_seconds=300,
            max_tokens=20000,
            risk="low",
            reviewer_family="openai",
        )

    def test_current_sha_and_required_fields_are_enforced(self) -> None:
        order = self.valid_order()
        snapshot = Snapshot(repository_heads={"clpi/idol": "b" * 40}, active_semantic_claims={}, active_path_claims={})
        result = validate_work_order(order, snapshot)
        self.assertFalse(result.valid)
        self.assertIn("stale-base-sha", result.reasons)

    def test_claim_overlap_is_refused(self) -> None:
        order = self.valid_order()
        snapshot = Snapshot(
            repository_heads={"clpi/idol": "a" * 40},
            active_semantic_claims={"law.grammar.one": "other"},
            active_path_claims={"docs/spec": "other"},
        )
        result = validate_work_order(order, snapshot)
        self.assertFalse(result.valid)
        self.assertIn("semantic-claim-conflict", result.reasons)
        self.assertIn("path-claim-conflict", result.reasons)

    def test_valid_order_passes_observation_gate(self) -> None:
        order = self.valid_order()
        snapshot = Snapshot(repository_heads={"clpi/idol": "a" * 40}, active_semantic_claims={}, active_path_claims={})
        self.assertTrue(validate_work_order(order, snapshot).valid)


class SchedulerTests(unittest.TestCase):
    def routes(self) -> tuple[Route, ...]:
        return (
            Route(
                id="local",
                provider="ollama",
                model="qwen",
                runtime="openclaw",
                billing=BillingClass.LOCAL,
                proof="local-runtime",
                roles=("observer", "mechanic", "evidence"),
                max_concurrency=2, billing_proven=True,
                windows=(AllowanceWindow("session", 1.0, 10000.0),),
            ),
            Route(
                id="premium",
                provider="openai",
                model="gpt",
                runtime="codex",
                billing=BillingClass.INCLUDED,
                proof="native-subscription",
                roles=("architect", "reviewer"),
                max_concurrency=1, billing_proven=True,
                windows=(AllowanceWindow("weekly", 0.9, 10000.0),),
            ),
            Route(
                id="wallet",
                provider="openrouter",
                model="claude",
                runtime="openclaw",
                billing=BillingClass.PURCHASED_CREDIT,
                proof="wallet",
                roles=("architect",),
                max_concurrency=10,
                windows=(AllowanceWindow("credits", 1.0, 10000.0),),
            ),
        )

    def tasks(self) -> tuple[Task, ...]:
        return (
            Task(
                id="observe",
                role="observer",
                priority=50,
                criticality=20,
                estimated_seconds=60,
                ready=True,
                semantic_targets=("claims",),
                path_targets=(),
                resident_routes=("local",),
                risk="low",
                review_required=False,
            ),
            Task(
                id="architect",
                role="architect",
                priority=100,
                criticality=100,
                estimated_seconds=300,
                ready=True,
                semantic_targets=("world",),
                path_targets=(RepositoryPath("docs/spec/world.md"),),
                resident_routes=(),
                risk="high",
                review_required=True,
            ),
        )

    def test_routes_work_by_role_and_never_selects_purchased_credit(self) -> None:
        scheduler = Scheduler(now=lambda: 100.0)
        plan = scheduler.plan(tasks=self.tasks(), routes=self.routes(), active_attempts=())
        assignments = {a.task_id: a.route_id for a in plan.assignments}
        self.assertEqual(assignments["observe"], "local")
        self.assertEqual(assignments["architect"], "premium")
        self.assertNotIn("wallet", assignments.values())


    def test_unproven_included_route_is_never_selected(self) -> None:
        scheduler = Scheduler(now=lambda: 100.0)
        unproven = Route(
            id="unproven", provider="x", model="x", runtime="openclaw",
            billing=BillingClass.INCLUDED, proof="asserted", roles=("architect",),
            max_concurrency=1, windows=(AllowanceWindow("weekly", 1.0, 10000.0),),
        )
        plan = scheduler.plan(tasks=(self.tasks()[1],), routes=(unproven,), active_attempts=())
        self.assertEqual(plan.assignments, ())
        self.assertIn("no-eligible-route", plan.refusals[0].reasons)

    def test_duplicate_semantic_attempt_is_refused(self) -> None:
        scheduler = Scheduler(now=lambda: 100.0)
        plan = scheduler.plan(
            tasks=self.tasks(),
            routes=self.routes(),
            active_attempts=({"task_id": "other", "semantic_targets": ["world"], "path_targets": []},),
        )
        self.assertNotIn("architect", {a.task_id for a in plan.assignments})
        refusal = next(r for r in plan.refusals if r.task_id == "architect")
        self.assertIn("semantic-overlap", refusal.reasons)


if __name__ == "__main__":
    unittest.main()
