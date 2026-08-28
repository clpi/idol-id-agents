from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from fleet_control.workspace import WorkspaceRefused, ensure_workspace, verify_handoff


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "idol"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        (self.repo / "claimed.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "outside.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "claimed.txt", "outside.txt")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, role: str, agent: str, **extra):
        value = {
            "agent_id": agent,
            "task_id": "task-1",
            "repo_id": "idol",
            "role": role,
            "base_sha": self.base,
            "paths": ["claimed.txt"],
            "semantic_boundaries": ["boundary"],
        }
        value.update(extra)
        return value

    def test_read_only_candidate_is_detached_and_must_remain_unchanged(self):
        payload = self.payload("reviewer", "reviewer-1", candidate_sha=self.base)
        workspace = ensure_workspace(repo=self.repo, state_dir=self.state, payload=payload)
        self.assertTrue(workspace.read_only)
        self.assertIsNone(workspace.branch)
        self.assertEqual(self.base, git(workspace.path, "rev-parse", "HEAD"))
        handoff = {
            "verdict": "accepted",
            "candidate_sha": self.base,
            "final_sha": self.base,
            "branch": "",
        }
        verified = verify_handoff(workspace=workspace, payload=payload, handoff=handoff)
        self.assertEqual([], verified["verified_changed_paths"])

        (workspace.path / "claimed.txt").write_text("mutated\n", encoding="utf-8")
        with self.assertRaises(WorkspaceRefused):
            verify_handoff(workspace=workspace, payload=payload, handoff=handoff)

    def test_implementation_requires_a_clean_committed_claimed_path_change(self):
        payload = self.payload("implementer", "implementer-1")
        workspace = ensure_workspace(repo=self.repo, state_dir=self.state, payload=payload)
        self.assertFalse(workspace.read_only)
        self.assertTrue(workspace.branch.startswith("fleet/task-1/implementer-"))
        (workspace.path / "claimed.txt").write_text("implemented\n", encoding="utf-8")
        git(workspace.path, "add", "claimed.txt")
        git(workspace.path, "commit", "-m", "implement claimed change")
        final = git(workspace.path, "rev-parse", "HEAD")
        handoff = {
            "verdict": "ready-for-review",
            "candidate_sha": None,
            "final_sha": final,
            "branch": workspace.branch,
        }
        verified = verify_handoff(workspace=workspace, payload=payload, handoff=handoff)
        self.assertEqual(["claimed.txt"], verified["verified_changed_paths"])

    def test_unclaimed_path_change_is_refused_even_when_committed(self):
        payload = self.payload("implementer", "implementer-2")
        workspace = ensure_workspace(repo=self.repo, state_dir=self.state, payload=payload)
        (workspace.path / "outside.txt").write_text("wrong\n", encoding="utf-8")
        git(workspace.path, "add", "outside.txt")
        git(workspace.path, "commit", "-m", "touch outside")
        handoff = {
            "verdict": "ready-for-review",
            "candidate_sha": None,
            "final_sha": git(workspace.path, "rev-parse", "HEAD"),
            "branch": workspace.branch,
        }
        with self.assertRaises(WorkspaceRefused):
            verify_handoff(workspace=workspace, payload=payload, handoff=handoff)

    def test_successful_implementation_with_no_commit_is_refused(self):
        payload = self.payload("implementer", "implementer-3")
        workspace = ensure_workspace(repo=self.repo, state_dir=self.state, payload=payload)
        handoff = {
            "verdict": "ready-for-review",
            "candidate_sha": None,
            "final_sha": self.base,
            "branch": workspace.branch,
        }
        with self.assertRaises(WorkspaceRefused):
            verify_handoff(workspace=workspace, payload=payload, handoff=handoff)


if __name__ == "__main__":
    unittest.main()
