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


class WorkOrderMaterializationTests(unittest.TestCase):
    def test_load_and_materialize_exact_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = [{
                "id": "a1", "task_id": "t1", "repository": "clpi/idol",
                "base_sha": "a" * 40, "branch": "fleet/a1", "role": "mechanic",
                "route_id": "local", "semantic_claims": ["law.one"],
                "path_claims": ["docs/spec/law.md"], "goal": "Remove duplicate prose.",
                "required_outcome": "One bounded patch.", "constraints": ["No law change"],
                "forbidden_repairs": ["No gate weakening"], "witnesses": ["true"],
                "stop_conditions": ["Unexpected path"], "estimated_seconds": 60,
                "max_tokens": 1000, "risk": "low", "reviewer_family": "openai",
            }]
            path = root / "orders.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            order = load_work_orders(path)[0]
            prompt = materialize_prompt(order, authority=(
                ("docs/spec/law.md", "law.one"),
                ("docs/bootstrap.md", "S0"),
            ))
            self.assertIn("AUTHORITY ORDER", prompt)
            self.assertIn("BASE SHA: " + "a" * 40, prompt)
            self.assertIn("docs/spec/law.md", prompt)
            self.assertNotIn("api_key", prompt.lower())

    def test_tasks_load_with_repository_paths_validated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tasks.json"
            path.write_text(json.dumps(task_payload()), encoding="utf-8")
            tasks = load_tasks(path)
            self.assertEqual(tasks[0].id, "observe-claims")


class CliTests(unittest.TestCase):
    def test_audit_and_plan_are_no_inference_and_write_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, sha = init_repo(root)
            policy = root / "policy.json"; policy.write_text(json.dumps(policy_payload()), encoding="utf-8")
            tasks = root / "tasks.json"; tasks.write_text(json.dumps(task_payload()), encoding="utf-8")
            state = root / "state"
            marker = root / "model-was-invoked"
            os.environ["IDOL_FLEET_MODEL_MARKER"] = str(marker)
            try:
                rc = main([
                    "run-once", "--policy", str(policy), "--tasks", str(tasks),
                    "--state", str(state), "--repository", f"clpi/idol={repo}",
                ])
            finally:
                os.environ.pop("IDOL_FLEET_MODEL_MARKER", None)
            self.assertEqual(rc, 0)
            self.assertFalse(marker.exists())
            self.assertTrue((state / "snapshots/latest.json").is_file())
            self.assertTrue((state / "plans/latest.json").is_file())
            self.assertEqual(stat.S_IMODE((state / "events.jsonl").stat().st_mode), 0o600)
            snapshot = json.loads((state / "snapshots/latest.json").read_text())
            self.assertEqual(snapshot["repositories"][0]["head"], sha)

    def test_enable_requires_complete_calibration_and_hash_binds_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"; config = root / "config"
            incomplete = root / "bad.json"; incomplete.write_text("{}", encoding="utf-8")
            self.assertNotEqual(main(["enable", "--calibration", str(incomplete), "--state", str(state), "--config-dir", str(config)]), 0)
            calibration = root / "calibration.json"
            payload = {
                "schema": "idol.fleet.calibration.v1",
                "no_paygo": True,
                "route_identity": True,
                "claim_control": True,
                "stale_sha_control": True,
                "overlap_control": True,
                "zero_edit_runtime": True,
                "bounded_mechanic": True,
                "positive_cost_detected": False,
            }
            calibration.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self.assertEqual(main(["enable", "--calibration", str(calibration), "--state", str(state), "--config-dir", str(config)]), 0)
            enabled = config / "apply-enabled"
            self.assertTrue(enabled.is_file())
            expected = hashlib.sha256(calibration.read_bytes()).hexdigest()
            self.assertEqual(enabled.read_text().strip(), expected)

    def test_status_output_contains_no_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            state.mkdir()
            Journal(state / "events.jsonl").append({"id": "e", "kind": "observed", "fact": {"route": "local"}})
            rc = main(["status", "--state", str(state)])
            self.assertEqual(rc, 0)


class InstallSurfaceTests(unittest.TestCase):
    def test_launchd_template_is_valid_and_has_no_secret_placeholders(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = root / "launchd/com.idol.fleet.plist"
        tree = ET.parse(template)
        rendered = template.read_text(encoding="utf-8").lower()
        self.assertEqual(tree.getroot().tag, "plist")
        for bad in ("api_key", "password", "token=", "secret="):
            self.assertNotIn(bad, rendered)
        self.assertIn("@@python@@", rendered)
        self.assertIn("@@policy@@", rendered)

    def test_install_script_never_enables_apply(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/install-fleet.sh").read_text(encoding="utf-8")
        self.assertNotIn("idol-fleet enable", script)
        self.assertIn("observe-plan", script)




if __name__ == "__main__":
    unittest.main()
