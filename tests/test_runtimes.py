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


class RuntimeTests(unittest.TestCase):
    def make_fake(self, directory: Path, name: str, payload: dict[str, object], rc: int = 0) -> Path:
        path = directory / name
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "capture = os.environ.get('FAKE_ARGV_CAPTURE')\n"
            "if capture: pathlib.Path(capture).write_text(json.dumps(sys.argv[1:]))\n"
            f"print(json.dumps({payload!r}))\n"
            f"raise SystemExit({rc})\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def order(self, root: Path, route_id: str = "route") -> WorkOrder:
        return WorkOrder(
            id="attempt",
            task_id="task",
            repository="clpi/idol",
            base_sha="a" * 40,
            branch="fleet/attempt",
            role="mechanic",
            route_id=route_id,
            semantic_claims=("law.test",),
            path_claims=(RepositoryPath("README.md"),),
            goal="mechanical test",
            required_outcome="structured success",
            constraints=("no extras",),
            forbidden_repairs=("no weakening",),
            witnesses=("true",),
            stop_conditions=("unexpected diff",),
            estimated_seconds=60,
            max_tokens=1000,
            risk="low",
        )

    def test_openclaw_command_is_pinned_and_result_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            capture = root / "argv.json"
            fake = self.make_fake(root, "openclaw", {
                "ok": True,
                "status": "ok",
                "final": "done",
                "usage": {"input": 10, "output": 2, "total": 12},
                "costUsd": 0,
                "model": "gpt",
                "provider": "openai",
                "sessionId": "secret-session",
                "toolSummary": {"calls": 1, "tools": ["read"]},
            })
            prompt = root / "task.md"
            prompt.write_text("do bounded work", encoding="utf-8")
            config = root / "route.json5"
            config.write_text("{}", encoding="utf-8")
            route = Route(
                id="route", provider="openai", model="openai/gpt", runtime="openclaw-codex",
                billing=BillingClass.INCLUDED, proof="native-subscription", roles=("mechanic",),
                max_concurrency=1, config_path=str(config), billing_proven=True,
            )
            runtime = OpenClawRuntime(executable=str(fake))
            result = runtime.execute(self.order(root), route, prompt, root, extra_env={"FAKE_ARGV_CAPTURE": str(capture)})
            argv = json.loads(capture.read_text())
            self.assertEqual(argv[:2], ["agent", "exec"])
            self.assertIn("--message-file", argv)
            self.assertIn("--config", argv)
            self.assertIn("--json", argv)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.usage_total, 12)
            self.assertNotEqual(result.session_hash, "secret-session")

    def test_positive_cost_on_included_route_is_policy_violation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = self.make_fake(root, "openclaw", {
                "ok": True, "status": "ok", "costUsd": 0.02,
                "model": "gpt", "provider": "openai",
            })
            prompt = root / "task.md"; prompt.write_text("x", encoding="utf-8")
            config = root / "route.json5"; config.write_text("{}", encoding="utf-8")
            route = Route(
                id="route", provider="openai", model="openai/gpt", runtime="openclaw-codex",
                billing=BillingClass.INCLUDED, proof="native-subscription", roles=("mechanic",),
                max_concurrency=1, config_path=str(config), billing_proven=True,
            )
            with self.assertRaises(RuntimePolicyViolation):
                OpenClawRuntime(executable=str(fake)).execute(self.order(root), route, prompt, root)

    def test_hermes_uses_usage_file_and_explicit_route(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            capture = root / "argv.json"
            fake = self.make_fake(root, "hermes", {"final": "done"})
            prompt = root / "task.md"; prompt.write_text("bounded", encoding="utf-8")
            route = Route(
                id="route", provider="ollama", model="qwen", runtime="hermes",
                billing=BillingClass.LOCAL, proof="local-runtime", roles=("mechanic",), max_concurrency=1, billing_proven=True,
            )
            HermesRuntime(executable=str(fake)).execute(
                self.order(root), route, prompt, root, extra_env={"FAKE_ARGV_CAPTURE": str(capture)}
            )
            argv = json.loads(capture.read_text())
            self.assertIn("--oneshot", argv)
            self.assertIn("--usage-file", argv)
            self.assertIn("--provider", argv)
            self.assertIn("--model", argv)




if __name__ == "__main__":
    unittest.main()
