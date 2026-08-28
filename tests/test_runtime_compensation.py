from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_control.controller import FleetPolicy
from fleet_control.runtime import FleetRuntime, RuntimeConfig


class StartCompensationTests(unittest.TestCase):
    def test_failed_start_releases_claim_before_tick_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sha = "a" * 40
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "schema": "idol.fleet.snapshot.v1",
                        "repositories": [
                            {
                                "id": "idol",
                                "head_sha": sha,
                                "branch": "main",
                                "dirty_count": 0,
                            }
                        ],
                        "providers": [
                            {
                                "id": "included",
                                "family": "test-family",
                                "cost_class": "included",
                                "enabled": True,
                                "roles": ["implementer"],
                                "quality": 1.0,
                                "max_concurrency": 1,
                                "windows": [],
                                "control_agent_id": "worker",
                            }
                        ],
                        "agents": [],
                        "tasks": [
                            {
                                "id": "task-1",
                                "repo_id": "idol",
                                "base_sha": sha,
                                "state": "implementation_ready",
                                "priority": "P0",
                                "required_role": "implementer",
                                "work_order": "registry://task-1",
                                "paths": ["src/x.zig"],
                                "semantic_boundaries": ["x"],
                                "estimate_minutes": 10,
                                "minimum_quality": 0.5,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            snapshot_script = root / "snapshot.py"
            snapshot_script.write_text(
                "import pathlib,sys\nsys.stdout.write(pathlib.Path(sys.argv[1]).read_text())\n",
                encoding="utf-8",
            )
            operations = root / "operations.log"
            adapter = root / "adapter.py"
            adapter.write_text(
                """import json,pathlib,sys
operation=sys.argv[1]
pathlib.Path(sys.argv[3]).open('a').write(operation+'\\n')
print(json.dumps({'ok': operation != 'agent.start'}))
""",
                encoding="utf-8",
            )
            commands = {
                kind: (sys.executable, str(adapter), kind, "{action_file}", str(operations))
                for kind in ("claim.acquire", "agent.start", "claim.release")
            }
            runtime = FleetRuntime(
                RuntimeConfig(
                    state_dir=root / "state",
                    snapshot_command=(sys.executable, str(snapshot_script), str(snapshot_path)),
                    action_commands=commands,
                    apply_enabled=True,
                ),
                FleetPolicy(max_agents_total=1, max_starts_per_tick=1),
            )
            with mock.patch.dict(
                os.environ,
                {
                    "IDOL_FLEET_APPLY": "1",
                    "IDOL_FLEET_COST_BOUNDARY": "INCLUDED_ONLY",
                },
                clear=False,
            ):
                result = runtime.tick(apply=True)

            self.assertEqual(
                ["claim.acquire", "agent.start", "claim.release"],
                [row["kind"] for row in result["results"]],
            )
            self.assertEqual([True, False, True], [row["ok"] for row in result["results"]])
            self.assertEqual(
                ["claim.acquire", "agent.start", "claim.release"],
                operations.read_text(encoding="utf-8").splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
