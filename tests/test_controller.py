from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from fleet_control.calibration import calibrate
from fleet_control.controller import FleetController, load_config
from fleet_control.gitops import current_sha


class ControllerTests(unittest.TestCase):
    def git(self, repo: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def fixture(self, *, mode: str, outside: bool = False):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        repo = root / "idol"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "test")
        self.git(repo, "config", "user.email", "test@example.com")
        (repo / "src").mkdir()
        (repo / "src" / "ok.txt").write_text("base\n")
        (repo / "docs/spec").mkdir(parents=True)
        (repo / "docs/spec/law.md").write_text("one meaning, one identity\n")
        (repo / "docs/spec/constitution.md").write_text("facts qualify identity\n")
        (repo / "docs/bootstrap.md").write_text("compiler B does not exist\n")
        claim = repo / "tools/node/dev/claim"
        claim.parent.mkdir(parents=True)
        claim.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >> \"${IDOL_TEST_CLAIM_LOG:?}\"\n"
            "exit 0\n"
        )
        claim.chmod(0o755)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "base")
        base = current_sha(repo)

        proof = root / "proof.py"
        proof.write_text("print('LOCAL TEST ROUTE READY')\n")
        agent = root / "agent.py"
        target = "outside.txt" if outside else "src/ok.txt"
        agent.write_text(
            "import json, pathlib, sys\n"
            "cwd=pathlib.Path(sys.argv[1])\n"
            f"p=cwd/{target!r}\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text('changed\\n')\n"
            "print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'tokens':1}}))\n"
        )
        state = root / "state"
        orders = state / "work-orders"
        orders.mkdir(parents=True)
        calibration_path = state / "calibration.json"
        config_path = root / "fleet.json"
        config = {
            "mode": mode,
            "repository": str(repo),
            "state_dir": str(state),
            "work_orders_dir": str(orders),
            "calibration_file": str(calibration_path),
            "interval_seconds": 60,
            "max_assignments": 1,
            "claim_ttl_seconds": 600,
            "witness_timeout_seconds": 60,
            "base_branch": "main",
            "author_name": "fleet-test",
            "author_email": "fleet@example.com",
            "routes": [
                {
                    "id": "local-test",
                    "provider": "local",
                    "model": "test-model",
                    "provider_family": "local",
                    "runtime": "plain",
                    "command": ["python3", str(agent), "{cwd}"],
                    "parser": "plain-json",
                    "billing": "local",
                    "proof": {
                        "kind": "local-process",
                        "subject_hash": "",
                        "observed_at": 0,
                        "expires_at": 0,
                        "evidence_hash": "",
                        "trusted": False,
                    },
                    "roles": ["mechanic"],
                    "auth_env": [],
                    "timeout_seconds": 30,
                    "max_parallel": 1,
                    "premium": False,
                    "enabled": True,
                    "allowance": [],
                    "proof_command": ["python3", str(proof)],
                    "proof_expect": "LOCAL TEST ROUTE READY",
                }
            ],
        }
        config_path.write_text(json.dumps(config))
        order = {
            "id": "t_controller_1",
            "task_id": "issue-test",
            "repository": str(repo),
            "base_sha": base,
            "branch": "fleet/issue-test/t-controller-1",
            "role": "mechanic",
            "required_outcome": "edit one claimed file",
            "path_claims": ["src/ok.txt"],
            "semantic_claims": ["evidence/controller/test"],
            "stop_conditions": ["stop on ambiguity"],
            "witnesses": [["python3", "-c", "from pathlib import Path; assert Path('src/ok.txt').read_text() == 'changed\\n'"]],
            "route_ids": ["local-test"],
            "authority_files": ["docs/spec/law.md", "docs/spec/constitution.md", "docs/bootstrap.md"],
            "risk": "low",
            "priority": 50,
            "estimated_seconds": 60,
            "estimated_tokens": 1000,
            "publish_branch": False,
            "create_draft_pr": False,
        }
        (orders / "t_controller_1.json").write_text(json.dumps(order))
        raw, parsed = load_config(config_path)
        if mode == "apply":
            calibrate(
                raw_config=raw,
                routes=parsed.routes,
                output=calibration_path,
                ttl_seconds=600,
            )
        claim_log = root / "claims.log"
        claim_log.write_text("")
        return temporary, root, repo, state, config_path, agent, claim_log

    def test_observe_plan_does_not_invoke_agent(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="observe-plan")
        with temporary:
            sentinel = root / "agent-ran"
            agent.write_text("from pathlib import Path; Path(%r).write_text('ran')\n" % str(sentinel))
            result = FleetController(config_path=config_path).run_once()
            self.assertEqual(result.mode, "observe-plan")
            self.assertFalse(sentinel.exists())
            self.assertFalse(result.attempts)

    def test_apply_edits_only_claimed_path_and_commits(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        old = os.environ.get("IDOL_TEST_CLAIM_LOG")
        os.environ["IDOL_TEST_CLAIM_LOG"] = str(claim_log)
        try:
            with temporary:
                result = FleetController(config_path=config_path).run_once()
                self.assertEqual(len(result.attempts), 1)
                attempt = result.attempts[0]
                self.assertIn("commit", attempt)
                self.assertEqual(tuple(attempt["paths"]), ("src/ok.txt",))
                self.assertTrue(Path(attempt["worktree"]).exists())
                log = claim_log.read_text()
                self.assertIn("acquire", log)
                self.assertIn("release", log)
                self.assertNotIn("--owner", log)
        finally:
            if old is None:
                os.environ.pop("IDOL_TEST_CLAIM_LOG", None)
            else:
                os.environ["IDOL_TEST_CLAIM_LOG"] = old

    def test_outside_path_edit_is_refused_and_preserved(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply", outside=True)
        old = os.environ.get("IDOL_TEST_CLAIM_LOG")
        os.environ["IDOL_TEST_CLAIM_LOG"] = str(claim_log)
        try:
            with temporary:
                result = FleetController(config_path=config_path).run_once()
                attempt = result.attempts[0]
                self.assertNotIn("commit", attempt)
                self.assertEqual(attempt["error_type"], "GitRefusal")
                self.assertTrue(attempt["worktree_preserved"])
        finally:
            if old is None:
                os.environ.pop("IDOL_TEST_CLAIM_LOG", None)
            else:
                os.environ["IDOL_TEST_CLAIM_LOG"] = old

    def test_stale_sha_never_invokes_agent(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="observe-plan")
        with temporary:
            order_path = state / "work-orders/t_controller_1.json"
            order = json.loads(order_path.read_text())
            order["base_sha"] = "f" * 40
            order_path.write_text(json.dumps(order))
            sentinel = root / "agent-ran"
            agent.write_text("from pathlib import Path; Path(%r).write_text('ran')\n" % str(sentinel))
            result = FleetController(config_path=config_path).run_once()
            self.assertFalse(result.plan.assignments)
            self.assertFalse(sentinel.exists())

    def test_duplicate_active_attempt_is_not_dispatched(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="observe-plan")
        with temporary:
            controller = FleetController(config_path=config_path)
            controller.journal.append("attempt.started", {"order_id": "t_controller_1"})
            result = controller.run_once()
            self.assertFalse(result.plan.assignments)
            reasons = [reason for row in result.plan.rejections for reason in row.reasons]
            self.assertIn("attempt-already-active", reasons)

    def test_retry_index_is_shared_across_fallback_routes(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="observe-plan")
        with temporary:
            controller = FleetController(config_path=config_path)
            controller.journal.append(
                "attempt.started",
                {"order_id": "t_controller_1", "route_id": "first-route"},
            )
            self.assertEqual(controller._attempt_index("t_controller_1"), 1)

    def test_witness_outside_claim_is_refused(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        order_path = state / "work-orders/t_controller_1.json"
        order = json.loads(order_path.read_text())
        order["witnesses"] = [[
            "python3",
            "-c",
            "from pathlib import Path; Path('outside-from-witness.txt').write_text('bad')",
        ]]
        order_path.write_text(json.dumps(order))
        old = os.environ.get("IDOL_TEST_CLAIM_LOG")
        os.environ["IDOL_TEST_CLAIM_LOG"] = str(claim_log)
        try:
            with temporary:
                result = FleetController(config_path=config_path).run_once()
                attempt = result.attempts[0]
                self.assertEqual(attempt["error_type"], "GitRefusal")
                self.assertNotIn("commit", attempt)
        finally:
            if old is None:
                os.environ.pop("IDOL_TEST_CLAIM_LOG", None)
            else:
                os.environ["IDOL_TEST_CLAIM_LOG"] = old


if __name__ == "__main__":
    unittest.main()
