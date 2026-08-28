from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from idol_fleet.claims import ClaimConflict, SchedulerLease, SemanticClaimStore
from idol_fleet.coordinator import AttemptState, Coordinator
from idol_fleet.journal import Journal
from idol_fleet.model import BillingClass, RepositoryPath, Route, WorkOrder
from idol_fleet.observe import observe_git_repository
from idol_fleet.runtime import HermesRuntime, OpenClawRuntime, RuntimePolicyViolation
from idol_fleet.worktree import WorktreeError, WorktreeManager


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def init_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


class ObserveTests(unittest.TestCase):
    def test_git_observation_has_exact_head_and_no_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, sha = init_repo(Path(td))
            (repo / "dirty.txt").write_text("private contents", encoding="utf-8")
            observation = observe_git_repository(repo, "clpi/idol")
            self.assertEqual(observation["head"], sha)
            self.assertEqual(observation["dirty_count"], 1)
            rendered = json.dumps(observation)
            self.assertNotIn("private contents", rendered)


class CoordinatorTests(unittest.TestCase):
    def test_state_transitions_are_append_only_and_apply_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = Journal(Path(td) / "events.jsonl")
            coordinator = Coordinator(journal=journal, mode="observe-plan")
            coordinator.transition("a", AttemptState.PROPOSED, AttemptState.VALIDATED, {"task": "t"})
            with self.assertRaises(PermissionError):
                coordinator.assert_dispatch_allowed()
            events = journal.read()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["fact"]["to"], "validated")

    def test_illegal_transition_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            coordinator = Coordinator(journal=Journal(Path(td) / "events.jsonl"), mode="apply")
            with self.assertRaises(ValueError):
                coordinator.transition("a", AttemptState.PROPOSED, AttemptState.READY, {})




if __name__ == "__main__":
    unittest.main()
