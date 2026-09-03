from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from fleet_control.controller import FleetController
from fleet_control.reconcile import reconcile_expired_attempts


class ReconcileTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        (repo / "a").write_text("a")
        subprocess.run(["git", "add", "a"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
        state = root / "state"
        orders = state / "orders"
        orders.mkdir(parents=True)
        config = {
            "mode": "observe-plan",
            "repository": str(repo),
            "state_dir": str(state),
            "work_orders_dir": str(orders),
            "calibration_file": str(state / "calibration.json"),
            "interval_seconds": 60,
            "max_assignments": 1,
            "claim_ttl_seconds": 60,
            "witness_timeout_seconds": 60,
            "base_branch": "main",
            "author_name": "test",
            "author_email": "test@example.com",
            "routes": [{
                "id": "disabled",
                "provider": "local",
                "model": "model",
                "provider_family": "local",
                "runtime": "plain",
                "command": ["true"],
                "parser": "plain-json",
                "billing": "local",
                "proof": {"kind":"local-process","subject_hash":"","observed_at":0,"expires_at":0,"evidence_hash":"","trusted":False},
                "roles": ["observer"],
                "auth_env": [],
                "timeout_seconds": 10,
                "max_parallel": 1,
                "premium": False,
                "enabled": False,
                "allowance": [],
                "proof_command": ["true"],
                "proof_expect": "unused"
            }]
        }
        path = root / "config.json"
        path.write_text(json.dumps(config))
        return temporary, FleetController(config_path=path)

    def test_expired_started_attempt_gets_cancel_fact(self) -> None:
        temporary, controller = self.fixture()
        with temporary:
            controller.journal.append(
                "attempt.started",
                {"order_id":"one","task_id":"task","attempt_id":"attempt","worktree":"/tmp/work"},
                at=10,
            )
            self.assertEqual(reconcile_expired_attempts(controller, now=71), ("one",))
            self.assertEqual(controller.journal.verify()[-1]["kind"], "attempt.cancelled")

    def test_unexpired_attempt_remains_active(self) -> None:
        temporary, controller = self.fixture()
        with temporary:
            controller.journal.append("attempt.started", {"order_id":"one"}, at=10)
            self.assertFalse(reconcile_expired_attempts(controller, now=69))

    def test_terminal_attempt_is_never_recancelled(self) -> None:
        temporary, controller = self.fixture()
        with temporary:
            controller.journal.append("attempt.started", {"order_id":"one"}, at=10)
            controller.journal.append("attempt.refused", {"order_id":"one"}, at=20)
            self.assertFalse(reconcile_expired_attempts(controller, now=100))

    def test_no_change_attempt_is_terminal(self) -> None:
        temporary, controller = self.fixture()
        with temporary:
            controller.journal.append("attempt.started", {"order_id":"one"}, at=10)
            controller.journal.append("attempt.no-change", {"order_id":"one"}, at=20)
            self.assertFalse(reconcile_expired_attempts(controller, now=100))


if __name__ == "__main__":
    unittest.main()
