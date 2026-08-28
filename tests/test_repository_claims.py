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


class RepositoryClaimClientTests(unittest.TestCase):
    def test_acquire_rolls_back_when_later_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "log.jsonl"
            script = root / "claim"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"p=pathlib.Path({str(log)!r})\n"
                "with p.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                "if sys.argv[1]=='acquire' and sys.argv[3]=='bad.txt':\n"
                " print(json.dumps({'granted':False})); raise SystemExit(2)\n"
                "print(json.dumps({'granted':True,'released':True}))\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            client = RepositoryClaimClient(root, executable=script)
            with self.assertRaises(Exception):
                client.acquire("owner", (RepositoryPath("good.txt"), RepositoryPath("bad.txt")), "task")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(["release", "owner", "good.txt"], calls)




if __name__ == "__main__":
    unittest.main()
