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


class ClaimTests(unittest.TestCase):
    def test_scheduler_lease_is_exclusive_and_released(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = SchedulerLease(root, owner="one", ttl=60)
            second = SchedulerLease(root, owner="two", ttl=60)
            first.acquire()
            with self.assertRaises(ClaimConflict):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

    def test_semantic_claims_refuse_overlap_and_recover_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SemanticClaimStore(Path(td), ttl=60)
            store.acquire(owner="a", task="t1", targets=("world.process", "law.run"))
            with self.assertRaises(ClaimConflict):
                store.acquire(owner="b", task="t2", targets=("world.process.capture",))
            store.release(owner="a", targets=("world.process", "law.run"))
            store.acquire(owner="b", task="t2", targets=("world.process.capture",))
            store.release(owner="b", targets=("world.process.capture",))


class WorktreeTests(unittest.TestCase):
    def test_create_is_exact_sha_and_canonical_checkout_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, sha = init_repo(root)
            manager = WorktreeManager(root / "worktrees")
            info = manager.create(repository=repo, attempt_id="a1", base_sha=sha, branch="fleet/a1")
            self.assertEqual(git(info.path, "rev-parse", "HEAD"), sha)
            self.assertEqual(git(info.path, "branch", "--show-current"), "fleet/a1")
            self.assertEqual(git(repo, "status", "--porcelain"), "")
            manager.retire(info, require_pushed=False)
            self.assertFalse(info.path.exists())

    def test_retire_refuses_dirty_unique_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, sha = init_repo(root)
            manager = WorktreeManager(root / "worktrees")
            info = manager.create(repository=repo, attempt_id="a2", base_sha=sha, branch="fleet/a2")
            (info.path / "unique.txt").write_text("unique", encoding="utf-8")
            with self.assertRaises(WorktreeError):
                manager.retire(info, require_pushed=False)




if __name__ == "__main__":
    unittest.main()
