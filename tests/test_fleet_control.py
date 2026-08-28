from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fleet_control.controller import FleetController, FleetPolicy, PlanError
from fleet_control.journal import AppendOnlyJournal, JournalIntegrityError, project_live
from fleet_control.runtime import ApplyRefused, FleetRuntime, RuntimeConfig

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


def provider(provider_id: str, family: str, *, cost: str = "included", roles=None, quality: float = 0.9, reset_minutes: int = 240, remaining: float = 0.8, premium: bool = False) -> dict:
    return {
        "id": provider_id,
        "family": family,
        "cost_class": cost,
        "enabled": True,
        "roles": roles or ["architect", "implementer", "reviewer", "evidence"],
        "quality": quality,
        "premium": premium,
        "max_concurrency": 3,
        "windows": [{"remaining_fraction": remaining, "resets_at": (NOW + timedelta(minutes=reset_minutes)).isoformat()}],
    }


def task(task_id: str = "idol-195", **overrides) -> dict:
    row = {
        "id": task_id,
        "repo_id": "idol",
        "base_sha": SHA,
        "state": "productive_ready",
        "priority": "P0",
        "required_role": "implementer",
        "work_order": f"work-orders/{task_id}.json",
        "paths": ["src/sema.zig"],
        "semantic_boundaries": ["process-world"],
        "estimate_minutes": 60,
        "minimum_quality": 0.7,
        "review_required": True,
        "stop_conditions": ["multiple lawful repairs remain"],
        "evidence": ["negative control"],
    }
    row.update(overrides)
    return row


def snapshot(*, providers=None, agents=None, tasks=None, head=SHA) -> dict:
    return {
        "schema": "idol.fleet.snapshot.v1",
        "repositories": [{"id": "idol", "head_sha": head}],
        "providers": providers or [],
        "agents": agents or [],
        "tasks": tasks or [],
    }


class ControllerTests(unittest.TestCase):
    def controller(self, **policy) -> FleetController:
        return FleetController(FleetPolicy.from_dict(policy), now=NOW)

    def test_paygo_never_selected(self):
        plan = self.controller().plan(snapshot(
            providers=[provider("paid", "paid", cost="paygo", quality=1.0), provider("included", "openai", quality=0.8)],
            tasks=[task()],
        ))
        starts = [a for a in plan["actions"] if a["kind"] == "agent.start"]
        self.assertEqual(1, len(starts))
        self.assertEqual("included", starts[0]["payload"]["provider_id"])
        self.assertFalse(plan["paygo_allowed"])

    def test_policy_cannot_enable_paygo_or_auto_merge(self):
        with self.assertRaises(PlanError):
            FleetPolicy.from_dict({"allow_paygo": True})
        with self.assertRaises(PlanError):
            FleetPolicy.from_dict({"automatic_merge": True})

    def test_stale_sha_checkpoints_suspends_and_releases(self):
        agent = {
            "id": "a1", "status": "running", "task_id": "idol-195", "base_sha": SHA,
            "last_activity_at": NOW.isoformat(),
            "claims": {"paths": ["src/sema.zig"], "semantic_boundaries": ["process-world"]},
        }
        plan = self.controller().plan(snapshot(
            providers=[provider("included", "openai")], agents=[agent], tasks=[task()], head="b" * 40,
        ))
        self.assertEqual(["agent.checkpoint", "agent.suspend", "claim.release"], [a["kind"] for a in plan["actions"]])

    def test_duplicate_overlapping_implementers_are_reduced(self):
        common = {
            "status": "running", "task_id": "idol-195", "base_sha": SHA, "role": "implementer",
            "last_activity_at": NOW.isoformat(),
            "claims": {"paths": ["src/sema.zig"], "semantic_boundaries": ["process-world"]},
        }
        plan = self.controller(max_starts_per_tick=0).plan(snapshot(
            providers=[provider("included", "openai")],
            agents=[{"id": "low", "progress": 0.2, **common}, {"id": "high", "progress": 0.8, **common}],
            tasks=[task(state="running")],
        ))
        suspended = [a for a in plan["actions"] if a["kind"] == "agent.suspend"]
        self.assertEqual("low", suspended[0]["agent_id"])

    def test_reviewer_must_be_other_family(self):
        plan = self.controller().plan(snapshot(
            providers=[provider("claude", "anthropic", roles=["reviewer"], quality=1.0), provider("codex", "openai", roles=["reviewer"], quality=0.9)],
            tasks=[task(state="implemented", implementer_family="anthropic", reviews=[])],
        ))
        start = next(a for a in plan["actions"] if a["kind"] == "agent.start")
        self.assertEqual("codex", start["payload"]["provider_id"])
        self.assertEqual("reviewer", start["payload"]["role"])

    def test_task_that_cannot_checkpoint_before_reset_is_not_started(self):
        plan = self.controller().plan(snapshot(
            providers=[provider("codex", "openai", reset_minutes=50)], tasks=[task(estimate_minutes=40)],
        ))
        self.assertFalse(any(a["kind"] == "agent.start" for a in plan["actions"]))

    def test_local_capacity_is_preferred_for_evidence(self):
        plan = self.controller().plan(snapshot(
            providers=[provider("codex", "openai", roles=["evidence"], quality=0.99, premium=True), provider("local", "ollama", cost="local", roles=["evidence"], quality=0.6)],
            tasks=[task(state="evidence_ready", minimum_quality=0.4)],
        ))
        start = next(a for a in plan["actions"] if a["kind"] == "agent.start")
        self.assertEqual("local", start["payload"]["provider_id"])

    def test_active_task_requires_exact_work_order(self):
        bad = task()
        del bad["work_order"]
        with self.assertRaises(PlanError):
            self.controller().plan(snapshot(tasks=[bad]))


class JournalTests(unittest.TestCase):
    def test_hash_chain_redaction_and_live_frontier(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = AppendOnlyJournal(Path(temp) / "history.ndjson")
            first = journal.append(kind="agent.started", subject="a1", actor="controller", payload={"status": "running", "token": "secret"})
            second = journal.append(kind="task.accepted", subject="idol-195", actor="integrator", payload={"state": "accepted"}, accepted=True)
            events = journal.read()
            self.assertEqual(first["id"], second["parent"])
            self.assertEqual("[redacted]", events[0]["payload"]["token"])
            live = project_live(events)
            self.assertEqual([second["id"]], live["accepted_frontier"])
            self.assertEqual("running", live["agents"]["a1"]["status"])

    def test_tamper_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.ndjson"
            journal = AppendOnlyJournal(path)
            journal.append(kind="task.ready", subject="t", actor="c", payload={"state": "ready"})
            path.write_text(path.read_text().replace('"ready"', '"tampered"'))
            with self.assertRaises(JournalIntegrityError):
                journal.read()


class RuntimeTests(unittest.TestCase):
    def runtime(self, root: Path, apply_enabled: bool = False) -> FleetRuntime:
        generator = root / "snapshot.py"
        generator.write_text("import json\nprint(json.dumps({'schema':'idol.fleet.snapshot.v1','repositories':[{'id':'idol','head_sha':'a'*40}],'providers':[],'agents':[],'tasks':[]}))\n")
        return FleetRuntime(
            RuntimeConfig(state_dir=root / "state", snapshot_command=("python3", str(generator)), action_commands={}, apply_enabled=apply_enabled),
            FleetPolicy(),
        )

    def test_dry_run_persists_snapshot_plan_and_live(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.runtime(root).tick(apply=False)
            self.assertEqual([], result["results"])
            self.assertTrue((root / "state/snapshot.json").is_file())
            self.assertTrue((root / "state/plan.json").is_file())
            self.assertTrue((root / "state/live.json").is_file())

    def test_apply_requires_both_explicit_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = self.runtime(Path(temp), apply_enabled=True)
            with self.assertRaises(ApplyRefused):
                runtime.tick(apply=True)
            with mock.patch.dict(os.environ, {"IDOL_FLEET_APPLY": "1", "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY"}, clear=False):
                self.assertEqual([], runtime.tick(apply=True)["results"])


if __name__ == "__main__":
    unittest.main()
