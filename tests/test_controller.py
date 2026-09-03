from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from fleet_control.calibration import calibrate
from fleet_control.controller import ControllerError, FleetController, load_config
from fleet_control.gitops import GitRefusal, current_sha
from fleet_control.model import stable_hash


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

    def add_remote(self, root: Path, repo: Path) -> Path:
        origin = root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(origin)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        self.git(repo, "remote", "add", "origin", str(origin))
        self.git(repo, "push", "-u", "origin", "main")
        writer = root / "writer"
        subprocess.run(
            ["git", "clone", str(origin), str(writer)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        self.git(writer, "config", "user.name", "remote-test")
        self.git(writer, "config", "user.email", "remote@example.com")
        return writer

    def enable_remote_tracking(self, config_path: Path, *, auto_fast_forward: bool) -> None:
        raw = json.loads(config_path.read_text())
        raw["remote_name"] = "origin"
        raw["remote_head_required"] = True
        raw["auto_fast_forward"] = auto_fast_forward
        config_path.write_text(json.dumps(raw))
        if raw["mode"] == "apply":
            parsed_raw, parsed = load_config(config_path)
            calibrate(
                raw_config=parsed_raw,
                routes=parsed.routes,
                output=parsed.calibration_file,
                ttl_seconds=600,
            )

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

    def test_auto_fast_forward_rebinds_order_when_watched_paths_are_unchanged(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        with temporary:
            writer = self.add_remote(root, repo)
            order_path = state / "work-orders/t_controller_1.json"
            order = json.loads(order_path.read_text())
            old_sha = order["base_sha"]
            order["follow_remote_main"] = True
            order_path.write_text(json.dumps(order))
            self.enable_remote_tracking(config_path, auto_fast_forward=True)
            (writer / "src/unrelated.txt").write_text("remote\n")
            self.git(writer, "add", "src/unrelated.txt")
            self.git(writer, "commit", "-m", "remote unrelated change")
            self.git(writer, "push", "origin", "main")
            new_sha = current_sha(writer)

            controller = FleetController(config_path=config_path)
            controller.refresh_remote_base()

            self.assertNotEqual(old_sha, new_sha)
            self.assertEqual(current_sha(repo), new_sha)
            self.assertEqual(json.loads(order_path.read_text())["base_sha"], new_sha)
            event = controller.journal.verify()[-1]
            self.assertEqual(event["kind"], "fleet.base.fast-forwarded")
            self.assertEqual(event["fact"]["rebound_orders"], ["t_controller_1"])

    def test_auto_fast_forward_holds_order_when_claimed_path_changed(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        with temporary:
            writer = self.add_remote(root, repo)
            order_path = state / "work-orders/t_controller_1.json"
            order = json.loads(order_path.read_text())
            old_sha = order["base_sha"]
            order["follow_remote_main"] = True
            order_path.write_text(json.dumps(order))
            self.enable_remote_tracking(config_path, auto_fast_forward=True)
            (writer / "src/ok.txt").write_text("remote changed subject\n")
            self.git(writer, "add", "src/ok.txt")
            self.git(writer, "commit", "-m", "remote claimed change")
            self.git(writer, "push", "origin", "main")
            new_sha = current_sha(writer)

            controller = FleetController(config_path=config_path)
            controller.refresh_remote_base()

            self.assertEqual(current_sha(repo), new_sha)
            self.assertEqual(json.loads(order_path.read_text())["base_sha"], old_sha)
            event = controller.journal.verify()[-1]
            self.assertEqual(event["kind"], "fleet.base.fast-forwarded")
            self.assertEqual(event["fact"]["held_orders"], {"t_controller_1": "watched-path-changed"})

    def test_observe_plan_reports_remote_drift_without_mutating(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="observe-plan")
        with temporary:
            writer = self.add_remote(root, repo)
            self.enable_remote_tracking(config_path, auto_fast_forward=True)
            old_sha = current_sha(repo)
            (writer / "src/unrelated.txt").write_text("remote\n")
            self.git(writer, "add", "src/unrelated.txt")
            self.git(writer, "commit", "-m", "remote change")
            self.git(writer, "push", "origin", "main")

            result = FleetController(config_path=config_path).run_once()

            self.assertEqual(current_sha(repo), old_sha)
            self.assertFalse(result.plan.assignments)
            self.assertFalse(result.observation.remote_in_sync)
            reasons = [reason for row in result.plan.rejections for reason in row.reasons]
            self.assertIn("remote-head-mismatch", reasons)

    def test_required_remote_head_failure_blocks_dispatch(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="observe-plan")
        with temporary:
            self.enable_remote_tracking(config_path, auto_fast_forward=False)
            result = FleetController(config_path=config_path).run_once()
            self.assertFalse(result.plan.assignments)
            self.assertIsNotNone(result.observation.remote_error)
            reasons = [reason for row in result.plan.rejections for reason in row.reasons]
            self.assertIn("remote-head-unavailable", reasons)

    def test_remote_refresh_journals_local_subject_failure(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        with temporary:
            self.add_remote(root, repo)
            self.enable_remote_tracking(config_path, auto_fast_forward=True)
            controller = FleetController(config_path=config_path)
            with mock.patch("fleet_control.controller.current_sha", side_effect=GitRefusal("bad local subject")):
                controller.refresh_remote_base()
            event = controller.journal.verify()[-1]
            self.assertEqual(event["kind"], "fleet.base.refresh-refused")
            self.assertIsNone(event["fact"]["old_sha"])

    def test_remote_refresh_refuses_work_order_directory_race(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        with temporary:
            writer = self.add_remote(root, repo)
            self.enable_remote_tracking(config_path, auto_fast_forward=True)
            old_sha = current_sha(repo)
            (writer / "src/unrelated.txt").write_text("remote\n")
            self.git(writer, "add", "src/unrelated.txt")
            self.git(writer, "commit", "-m", "remote change")
            self.git(writer, "push", "origin", "main")
            controller = FleetController(config_path=config_path)
            with mock.patch.object(controller, "_load_work_orders", return_value=((), {})):
                controller.refresh_remote_base()
            self.assertEqual(current_sha(repo), old_sha)
            event = controller.journal.verify()[-1]
            self.assertEqual(event["kind"], "fleet.base.refresh-refused")
            self.assertIn("work order changed during remote refresh", event["fact"]["error"])

    def test_remote_rebind_rejects_changed_work_order_with_same_base(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        with temporary:
            order_path = state / "work-orders/t_controller_1.json"
            original = json.loads(order_path.read_text())
            expected_hash = stable_hash(original)
            changed = dict(original)
            changed["path_claims"] = ["src/other.txt"]
            order_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ControllerError, "changed before remote rebind"):
                FleetController._write_rebound_order(
                    order_path,
                    old_sha=original["base_sha"],
                    new_sha="f" * 40,
                    expected_hash=expected_hash,
                )

    def test_rebind_failure_after_fast_forward_is_recorded_per_order(self) -> None:
        temporary, root, repo, state, config_path, agent, claim_log = self.fixture(mode="apply")
        with temporary:
            writer = self.add_remote(root, repo)
            order_path = state / "work-orders/t_controller_1.json"
            order = json.loads(order_path.read_text())
            old_sha = order["base_sha"]
            order["follow_remote_main"] = True
            order_path.write_text(json.dumps(order))
            self.enable_remote_tracking(config_path, auto_fast_forward=True)
            (writer / "src/unrelated.txt").write_text("remote\n")
            self.git(writer, "add", "src/unrelated.txt")
            self.git(writer, "commit", "-m", "remote change")
            self.git(writer, "push", "origin", "main")
            new_sha = current_sha(writer)
            controller = FleetController(config_path=config_path)
            with mock.patch.object(
                controller,
                "_write_rebound_order",
                side_effect=ControllerError("simulated concurrent replacement"),
            ):
                controller.refresh_remote_base()

            self.assertEqual(current_sha(repo), new_sha)
            self.assertEqual(json.loads(order_path.read_text())["base_sha"], old_sha)
            event = controller.journal.verify()[-1]
            self.assertEqual(event["kind"], "fleet.base.fast-forwarded")
            self.assertEqual(event["fact"]["rebound_orders"], [])
            self.assertIn("t_controller_1", event["fact"]["rebind_refusals"])


if __name__ == "__main__":
    unittest.main()
