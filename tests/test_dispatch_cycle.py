from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from idol_fleet.claims import RepositoryClaimClient, SemanticClaimStore
from idol_fleet.coordinator import DispatchError, Dispatcher
from idol_fleet.journal import Journal
from idol_fleet.model import BillingClass, RepositoryPath, Route, WorkOrder
from idol_fleet.runtime import RunResult
from idol_fleet.worktree import WorktreeManager


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def init_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "allowed.txt").write_text("one\n", encoding="utf-8")
    (repo / "outside.txt").write_text("stable\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def route() -> Route:
    return Route(
        id="local",
        provider="ollama",
        model="qwen",
        runtime="openclaw",
        billing=BillingClass.LOCAL,
        proof="local-runtime",
        roles=("mechanic",),
        max_concurrency=1,
        billing_proven=True,
    )


def order(sha: str) -> WorkOrder:
    return WorkOrder(
        id="attempt1",
        task_id="task1",
        repository="clpi/idol",
        base_sha=sha,
        branch="fleet/attempt1",
        role="mechanic",
        route_id="local",
        semantic_claims=("law.test",),
        path_claims=(RepositoryPath("allowed.txt"),),
        goal="Change only the claimed file.",
        required_outcome="Bounded claimed-path change",
        constraints=("No unrelated edits",),
        forbidden_repairs=("Do not weaken the witness",),
        witnesses=("true",),
        stop_conditions=("Any outside-claim edit",),
        estimated_seconds=60,
        max_tokens=1000,
        risk="low",
    )


class FakeRuntime:
    def __init__(self, *, outside: bool = False, ok: bool = True) -> None:
        self.outside = outside
        self.ok = ok

    def execute(self, order, route, prompt_path, cwd, **kwargs):
        (Path(cwd) / "allowed.txt").write_text("two\n", encoding="utf-8")
        if self.outside:
            (Path(cwd) / "outside.txt").write_text("bad\n", encoding="utf-8")
        return RunResult(
            status="ok" if self.ok else "error",
            ok=self.ok,
            provider=route.provider,
            model=route.model,
            usage_input=10,
            usage_output=2,
            usage_total=12,
            cost_usd=0,
            session_hash="hash",
            tool_calls=1,
            returncode=0 if self.ok else 1,
            timed_out=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
        )


class FakeRepositoryClaims:
    def __init__(self) -> None:
        self.held: set[str] = set()

    def acquire(self, owner: str, paths: tuple[RepositoryPath, ...], work: str) -> None:
        self.held.update(map(str, paths))

    def release(self, owner: str, paths: tuple[RepositoryPath, ...]) -> None:
        self.held.difference_update(map(str, paths))


class DispatchTests(unittest.TestCase):
    def test_dispatch_requires_apply_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo, sha = init_repo(root)
            dispatcher = Dispatcher(
                journal=Journal(root / "events.jsonl"),
                semantic_claims=SemanticClaimStore(root / "claims"),
                repository_claims=FakeRepositoryClaims(),
                worktrees=WorktreeManager(root / "worktrees"),
                apply_enabled=False,
            )
            with self.assertRaises(PermissionError):
                dispatcher.dispatch(order(sha), route(), repo, FakeRuntime(), authority=())

    def test_dispatch_commits_only_claimed_paths_and_releases_claims(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo, sha = init_repo(root)
            repo_claims = FakeRepositoryClaims()
            dispatcher = Dispatcher(
                journal=Journal(root / "events.jsonl"),
                semantic_claims=SemanticClaimStore(root / "claims"),
                repository_claims=repo_claims,
                worktrees=WorktreeManager(root / "worktrees"),
                apply_enabled=True,
            )
            outcome = dispatcher.dispatch(order(sha), route(), repo, FakeRuntime(), authority=())
            self.assertEqual(outcome.state, "ready")
            self.assertNotEqual(outcome.commit_sha, sha)
            self.assertEqual(repo_claims.held, set())
            self.assertEqual(dispatcher.semantic_claims.list(), ())
            self.assertEqual(git(outcome.worktree, "show", "--name-only", "--format=", "HEAD"), "allowed.txt")

    def test_outside_claim_change_is_held_and_worktree_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo, sha = init_repo(root)
            dispatcher = Dispatcher(
                journal=Journal(root / "events.jsonl"),
                semantic_claims=SemanticClaimStore(root / "claims"),
                repository_claims=FakeRepositoryClaims(),
                worktrees=WorktreeManager(root / "worktrees"),
                apply_enabled=True,
            )
            with self.assertRaises(DispatchError):
                dispatcher.dispatch(order(sha), route(), repo, FakeRuntime(outside=True), authority=())
            self.assertTrue((root / "worktrees/attempt1/outside.txt").exists())




if __name__ == "__main__":
    unittest.main()
