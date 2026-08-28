from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fleet_control.controller import FleetController, FleetPolicy


class StageTurnoverTests(unittest.TestCase):
    def test_terminal_claim_release_precedes_counterexample_start(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        sha = "a" * 40
        snapshot = {
            "schema": "idol.fleet.snapshot.v1",
            "repositories": [
                {"id": "idol", "head_sha": sha, "branch": "main", "dirty_count": 0}
            ],
            "providers": [
                {
                    "id": "local",
                    "family": "local-family",
                    "cost_class": "local",
                    "enabled": True,
                    "roles": ["counterexample"],
                    "quality": 1.0,
                    "max_concurrency": 1,
                    "windows": [],
                    "control_agent_id": "local-agent",
                }
            ],
            "agents": [
                {
                    "id": "architect-agent",
                    "repo_id": "idol",
                    "task_id": "task-1",
                    "role": "architect",
                    "status": "completed",
                    "base_sha": sha,
                    "claim_active": True,
                    "claims": {
                        "paths": ["src/x.zig"],
                        "semantic_boundaries": ["x"],
                    },
                }
            ],
            "tasks": [
                {
                    "id": "task-1",
                    "repo_id": "idol",
                    "base_sha": sha,
                    "state": "counterexample_ready",
                    "priority": "P0",
                    "required_role": "architect",
                    "minimum_quality": 0.5,
                    "estimate_minutes": 10,
                    "work_order": "registry://task-1",
                    "paths": ["src/x.zig"],
                    "semantic_boundaries": ["x"],
                    "excluded_families": ["architect-family"],
                }
            ],
        }
        plan = FleetController(
            FleetPolicy(max_agents_total=1, max_starts_per_tick=1), now=now
        ).plan(snapshot)
        self.assertEqual(
            ["claim.release", "claim.acquire", "agent.start"],
            [action["kind"] for action in plan["actions"]],
        )
        self.assertEqual("architect-agent", plan["actions"][0]["agent_id"])
        self.assertEqual("counterexample", plan["actions"][2]["payload"]["role"])


if __name__ == "__main__":
    unittest.main()
