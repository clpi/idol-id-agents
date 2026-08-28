from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from idol_fleet.cli import main
from idol_fleet.journal import Journal
from idol_fleet.model import RepositoryPath, Snapshot, WorkOrder
from idol_fleet.work_order import load_tasks, load_work_orders, materialize_prompt


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def init_repo(root: Path) -> tuple[Path, str]:
    repo = root / "idol"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "docs").mkdir()
    (repo / "docs/spec").mkdir()
    (repo / "docs/spec/law.md").write_text("law.one\n", encoding="utf-8")
    (repo / "docs/spec/constitution.md").write_text("constitution.one\n", encoding="utf-8")
    (repo / "docs/bootstrap.md").write_text("S0\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def policy_payload() -> dict[str, object]:
    return {
        "version": 1,
        "mode": "observe-plan",
        "trusted_billing_proofs": ["local-runtime"],
        "routes": [
            {
                "id": "local",
                "provider": "ollama",
                "model": "ollama/qwen",
                "runtime": "openclaw",
                "billing": "local",
                "proof": "local-runtime",
                "roles": ["observer", "mechanic", "evidence"],
                "max_concurrency": 2,
                "fallbacks": [],
            }
        ],
        "limits": {"global_editing": 1, "global_observer": 4},
    }


def task_payload() -> list[dict[str, object]]:
    return [
        {
            "id": "observe-claims",
            "role": "observer",
            "priority": 50,
            "criticality": 60,
            "estimated_seconds": 60,
            "ready": True,
            "semantic_targets": ["claims"],
            "path_targets": [],
            "resident_routes": ["local"],
            "risk": "low",
            "review_required": False,
        }
    ]


class ApplyCycleTests(unittest.TestCase):
    def test_run_once_dispatches_only_after_hash_bound_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, sha = init_repo(root)
            (repo / "allowed.txt").write_text("one\n", encoding="utf-8")
            git(repo, "add", "allowed.txt")
            git(repo, "commit", "-q", "-m", "add allowed")
            sha = git(repo, "rev-parse", "HEAD")

            claim = repo / "tools/node/dev/claim"
            claim.parent.mkdir(parents=True)
            claim.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                "print(json.dumps({'granted':True,'released':True}))\n",
                encoding="utf-8",
            )
            claim.chmod(0o700)

            openclaw = root / "openclaw"
            openclaw.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,sys\n"
                "args=sys.argv[1:]\n"
                "cwd=pathlib.Path(args[args.index('--cwd')+1])\n"
                "(cwd/'allowed.txt').write_text('two\\n')\n"
                "print(json.dumps({'ok':True,'status':'ok','usage':{'total':5},'costUsd':0,'provider':'ollama','model':'ollama/qwen'}))\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o700)
            route_config = root / "route.json5"; route_config.write_text("{}", encoding="utf-8")
            policy_data = policy_payload()
            policy_data["mode"] = "apply"
            policy_data["routes"][0]["config_path"] = str(route_config)  # type: ignore[index]
            policy_data["routes"][0]["executable"] = str(openclaw)  # type: ignore[index]
            policy = root / "policy.json"; policy.write_text(json.dumps(policy_data), encoding="utf-8")
            tasks_data = [{
                "id": "bounded", "role": "mechanic", "priority": 100, "criticality": 100,
                "estimated_seconds": 60, "ready": True, "semantic_targets": ["law.test"],
                "path_targets": ["allowed.txt"], "resident_routes": ["local"],
                "risk": "low", "review_required": False,
            }]
            tasks = root / "tasks.json"; tasks.write_text(json.dumps(tasks_data), encoding="utf-8")
            orders_data = [{
                "id": "attempt-bounded", "task_id": "bounded", "repository": "clpi/idol",
                "base_sha": sha, "branch": "fleet/attempt-bounded", "role": "mechanic",
                "route_id": "auto", "semantic_claims": ["law.test"], "path_claims": ["allowed.txt"],
                "goal": "Change one claimed file.", "required_outcome": "One bounded commit.",
                "constraints": ["No outside files"], "forbidden_repairs": ["No weakening"],
                "witnesses": ["python3 -c 'raise SystemExit(0)'"],
                "stop_conditions": ["Outside path"], "estimated_seconds": 60,
                "max_tokens": 1000, "risk": "low"
            }]
            orders = root / "orders.json"; orders.write_text(json.dumps(orders_data), encoding="utf-8")
            state = root / "state"; config = root / "config"
            calibration = root / "calibration.json"
            calibration.write_text(json.dumps({
                "schema": "idol.fleet.calibration.v1", "no_paygo": True,
                "route_identity": True, "claim_control": True, "stale_sha_control": True,
                "overlap_control": True, "zero_edit_runtime": True, "bounded_mechanic": True,
                "positive_cost_detected": False,
            }, sort_keys=True), encoding="utf-8")
            self.assertEqual(main(["enable", "--calibration", str(calibration), "--state", str(state), "--config-dir", str(config)]), 0)
            rc = main([
                "run-once", "--policy", str(policy), "--tasks", str(tasks),
                "--orders", str(orders), "--calibration", str(calibration),
                "--config-dir", str(config), "--state", str(state),
                "--repository", f"clpi/idol={repo}",
            ])
            self.assertEqual(rc, 0)
            worktree = state / "worktrees/attempt-bounded"
            self.assertTrue(worktree.is_dir())
            self.assertEqual(git(worktree, "show", "--name-only", "--format=", "HEAD"), "allowed.txt")
            terminal = [e for e in Journal(state / "events.jsonl").read() if e["kind"] == "attempt-ready"]
            self.assertEqual(len(terminal), 1)




if __name__ == "__main__":
    unittest.main()
