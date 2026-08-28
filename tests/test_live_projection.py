from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fleet_control.controller import FleetPolicy
from fleet_control.journal import AppendOnlyJournal, project_live
from fleet_control.runtime import FleetRuntime, RuntimeConfig


class LiveProjectionTests(unittest.TestCase):
    def test_snapshot_projects_all_operational_entities(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = AppendOnlyJournal(Path(temp) / "history.ndjson")
            journal.append(
                kind="fleet.snapshot.observed",
                subject="fleet",
                actor="controller",
                payload={
                    "observed_at": "2026-08-28T12:00:00+00:00",
                    "repositories": [{"id": "idol", "head_sha": "a" * 40}],
                    "agents": [{"id": "agent-1", "status": "running"}],
                    "tasks": [{"id": "task-1", "state": "implementation_ready"}],
                    "providers": [{"id": "included", "cost_class": "included"}],
                },
            )
            live = project_live(journal.read())
            self.assertEqual("a" * 40, live["repositories"]["idol"]["head_sha"])
            self.assertEqual("running", live["agents"]["agent-1"]["status"])
            self.assertEqual("implementation_ready", live["tasks"]["task-1"]["state"])
            self.assertEqual("included", live["providers"]["included"]["cost_class"])

    def test_identical_poll_does_not_append_snapshot_or_plan_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = {
                "schema": "idol.fleet.snapshot.v1",
                "observed_at": "2026-08-28T12:00:00+00:00",
                "repositories": [
                    {
                        "id": "idol",
                        "head_sha": "a" * 40,
                        "branch": "main",
                        "dirty_count": 0,
                    }
                ],
                "providers": [],
                "agents": [],
                "tasks": [],
            }
            payload = root / "snapshot.json"
            payload.write_text(json.dumps(snapshot), encoding="utf-8")
            command = root / "snapshot.py"
            command.write_text(
                "import pathlib,sys\nsys.stdout.write(pathlib.Path(sys.argv[1]).read_text())\n",
                encoding="utf-8",
            )
            runtime = FleetRuntime(
                RuntimeConfig(
                    state_dir=root / "state",
                    snapshot_command=(sys.executable, str(command), str(payload)),
                    action_commands={},
                ),
                FleetPolicy(),
            )
            runtime.tick(apply=False)
            first = AppendOnlyJournal(root / "state/history.ndjson").read()
            self.assertEqual(2, len(first))

            snapshot["observed_at"] = "2026-08-28T12:05:00+00:00"
            payload.write_text(json.dumps(snapshot), encoding="utf-8")
            runtime.tick(apply=False)
            second = AppendOnlyJournal(root / "state/history.ndjson").read()
            self.assertEqual(2, len(second))


if __name__ == "__main__":
    unittest.main()
