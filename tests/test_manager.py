from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from fleet_control.calibration import calibrate
from fleet_control.controller import load_config
from fleet_control.gitops import current_sha
from fleet_control.manager import ManagedFleetController


class ManagerTests(unittest.TestCase):
    def git(self, repo: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    def fixture(
        self,
        *,
        mode: str = "observe-plan",
        managed_attempt: bool = False,
        cancel_enabled: bool = False,
    ):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        repo = root / "idol"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "test")
        self.git(repo, "config", "user.email", "test@example.com")
        (repo / "docs/spec").mkdir(parents=True)
        (repo / "docs/spec/law.md").write_text("law\n")
        (repo / "docs/spec/constitution.md").write_text("constitution\n")
        (repo / "docs/bootstrap.md").write_text("bootstrap\n")
        (repo / "src").mkdir()
        (repo / "src/a.zig").write_text("base\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "base")
        head = current_sha(repo)

        state = root / "state"
        orders = state / "orders"
        orders.mkdir(parents=True)
        inventory = root / "inventory.py"
        session = {
            "id": "session-one",
            "status": "running",
            "last_activity": time.time(),
            "provider": "local",
            "model": "model",
            "order_id": "order-one",
            "task_id": "task-one",
            "base_sha": "f" * 40,
            "host": "host",
            "actor": "agent",
        }
        if managed_attempt:
            session["attempt_id"] = "attempt-one"
        payload = {
            "schema": "idol.fleet.inventory.v1",
            "observed_at": time.time(),
            "source": "test",
            "sessions": [session],
            "agents": [{"id":"agent","status":"running","provider":"local","model":"model","host":"host","role":"mechanic"}],
        }
        inventory.write_text("import json\n" + f"print(json.dumps({payload!r}))\n")
        cancel_log = root / "cancel.log"
        cancel = root / "cancel.py"
        cancel.write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(cancel_log)!r}).write_text(sys.argv[1])\n"
            "print(json.dumps({'status':'cancelled','session_id':sys.argv[1]}))\n"
        )
        proof = root / "proof.py"
        proof.write_text("print('LOCAL ROUTE READY')\n")

        route = {
            "id":"local-test",
            "provider":"local",
            "model":"model",
            "provider_family":"local",
            "runtime":"plain",
            "command":["python3","-c","import json; print(json.dumps({'status':'ok','provider':'local','model':'model','costUsd':0}))"],
            "parser":"plain-json",
            "billing":"local",
            "proof":{"kind":"local-process","subject_hash":"","observed_at":0,"expires_at":0,"evidence_hash":"","trusted":False},
            "roles":["mechanic"],
            "auth_env":[],
            "timeout_seconds":30,
            "max_parallel":1,
            "premium":False,
            "enabled":mode == "apply",
            "allowance":[],
            "proof_command":["python3",str(proof)],
            "proof_expect":"LOCAL ROUTE READY",
            "usage_command":[],
            "usage_auth_env":[],
            "usage_timeout_seconds":30,
            "usage_max_age_seconds":300,
            "usage_required":False
        }
        config = {
            "mode":mode,
            "repository":str(repo),
            "state_dir":str(state),
            "work_orders_dir":str(orders),
            "calibration_file":str(state / "calibration.json"),
            "interval_seconds":60,
            "max_assignments":1,
            "claim_ttl_seconds":600,
            "witness_timeout_seconds":60,
            "base_branch":"main",
            "author_name":"test",
            "author_email":"test@example.com",
            "inventory":{
                "enabled":True,
                "command":["python3",str(inventory)],
                "cancel_command":["python3",str(cancel),"{session_id}"],
                "auth_env":[],
                "timeout_seconds":10,
                "max_age_seconds":300,
                "cancel_owned_sessions":cancel_enabled,
                "adoptions_file":str(state / "adoptions.json")
            },
            "routes":[route]
        }
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config))
        order = {
            "id":"order-one",
            "task_id":"task-one",
            "repository":str(repo),
            "base_sha":head,
            "branch":"fleet/task-one/order-one",
            "role":"mechanic",
            "required_outcome":"bounded change",
            "path_claims":["src/a.zig"],
            "semantic_claims":["evidence/task-one"],
            "stop_conditions":["stop"],
            "witnesses":[["git","diff","--check"]],
            "route_ids":["local-test"],
            "authority_files":["docs/spec/law.md","docs/spec/constitution.md","docs/bootstrap.md"],
            "risk":"low",
            "priority":50,
            "estimated_seconds":60,
            "estimated_tokens":1000,
            "publish_branch":False,
            "create_draft_pr":False
        }
        (orders / "order-one.json").write_text(json.dumps(order))
        if mode == "apply":
            raw, parsed = load_config(config_path)
            calibrate(raw_config=raw, routes=parsed.routes, output=parsed.calibration_file, ttl_seconds=600)
        return temporary, config_path, head, cancel_log

    def test_unmanaged_live_session_blocks_duplicate_but_is_not_cancelled(self) -> None:
        temporary, config_path, head, cancel_log = self.fixture()
        with temporary:
            controller = ManagedFleetController(config_path=config_path)
            result = controller.run_once()
            self.assertFalse(result.plan.assignments)
            reasons = [reason for row in result.plan.rejections for reason in row.reasons]
            self.assertIn("live-session-already-covers-task", reasons)
            self.assertFalse(cancel_log.exists())
            kinds = [row["kind"] for row in controller.journal.verify()]
            self.assertIn("fleet.session.unmanaged", kinds)
            self.assertNotIn("fleet.session.cancelled", kinds)

    def test_controller_owned_stale_session_only_proposes_cancel_in_observe_mode(self) -> None:
        temporary, config_path, head, cancel_log = self.fixture(managed_attempt=True)
        with temporary:
            controller = ManagedFleetController(config_path=config_path)
            controller.journal.append(
                "attempt.started",
                {"attempt_id":"attempt-one","order_id":"order-one","task_id":"task-one","route_id":"local-test","base_sha":head},
            )
            controller.run_once()
            self.assertFalse(cancel_log.exists())
            kinds = [row["kind"] for row in controller.journal.verify()]
            self.assertIn("fleet.session.cancel.proposed", kinds)
            self.assertNotIn("fleet.session.cancelled", kinds)

    def test_controller_owned_stale_session_can_cancel_only_in_calibrated_apply(self) -> None:
        temporary, config_path, head, cancel_log = self.fixture(
            mode="apply",
            managed_attempt=True,
            cancel_enabled=True,
        )
        with temporary:
            controller = ManagedFleetController(config_path=config_path)
            controller.journal.append(
                "attempt.started",
                {"attempt_id":"attempt-one","order_id":"order-one","task_id":"task-one","route_id":"local-test","base_sha":head},
            )
            controller.run_once()
            self.assertEqual(cancel_log.read_text(), "session-one")
            kinds = [row["kind"] for row in controller.journal.verify()]
            self.assertIn("fleet.session.cancelled", kinds)


if __name__ == "__main__":
    unittest.main()
