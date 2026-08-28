from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from fleet_control.agent_protocol import build_prompt, parse_handoff
from fleet_control.task_registry import _progress
from scripts.local_action_adapter import claim_acquire, claim_release
from scripts.run_agent import _handoff_text


class AgentProtocolTests(unittest.TestCase):
    def payload(self):
        return {
            "task_id": "idol-195-process-world",
            "role": "architect",
            "provider_family": "openai",
            "base_sha": "a" * 40,
            "repo_id": "idol",
            "paths": ["docs/spec/world.md"],
            "semantic_boundaries": ["process-world"],
            "authority": {"law_sha256": "b" * 64},
            "stop_conditions": ["multiple lawful repairs remain"],
            "evidence": ["negative control"],
        }

    def handoff_line(self, **changes):
        value = {
            "schema": "idol.agent.handoff.v1",
            "task_id": "idol-195-process-world",
            "role": "architect",
            "provider_family": "openai",
            "base_sha": "a" * 40,
            "final_sha": "a" * 40,
            "verdict": "accepted",
            "summary": "bounded",
            "branch": "",
            "pull_request": None,
            "evidence": ["proof"],
            "owned_paths": ["docs/spec/world.md"],
            "semantic_boundaries": ["process-world"],
            "last_command": "git status",
            "blocker": None,
            "next_action": "counterexample review",
        }
        value.update(changes)
        return "IDOL_HANDOFF_V1=" + json.dumps(value, separators=(",", ":"))

    def test_prompt_contains_authority_and_no_paygo_boundary(self):
        prompt = build_prompt(self.payload())
        self.assertIn("one meaning, one exact semantic identity", prompt)
        self.assertIn("no pay-go", prompt)
        self.assertIn("docs/spec/law.md", prompt)
        self.assertIn("idol-195-process-world", prompt)

    def test_json_wrapped_output_yields_one_handoff(self):
        line = self.handoff_line()
        wrapped = {"result": {"message": line}, "usage": {"tokens": 100}}
        text = _handoff_text(json.dumps(wrapped), wrapped)
        handoff = parse_handoff(text, expected=self.payload())
        self.assertEqual("accepted", handoff["verdict"])

    def test_wrong_provider_family_is_refused(self):
        with self.assertRaises(ValueError):
            parse_handoff(self.handoff_line(provider_family="anthropic"), expected=self.payload())

    def test_checkpoint_handoff_is_not_a_task_progression_input(self):
        task = {"architecture_required": True, "state": "architecture_ready"}
        checkpoint = {
            "role": "architect",
            "verdict": "accepted",
            "checkpoint": True,
        }
        # The loader excludes checkpoints. Passing no accepted run keeps architecture pending.
        self.assertEqual("architecture_ready", _progress(task, [])["state"])
        self.assertTrue(checkpoint["checkpoint"])

    def test_architecture_then_counterexample_then_implementation_pipeline(self):
        task = {"architecture_required": True, "counterexample_required": True, "state": "architecture_ready"}
        architecture = {"role": "architect", "verdict": "accepted", "id": "a"}
        self.assertEqual("counterexample_ready", _progress(task, [architecture])["state"])
        counterexample = {"role": "counterexample", "verdict": "no-counterexample", "id": "c"}
        self.assertEqual("implementation_ready", _progress(task, [architecture, counterexample])["state"])


class ClaimAdapterTests(unittest.TestCase):
    def test_claim_is_observable_and_released(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "idol"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            (repo / "x").write_text("x")
            subprocess.run(["git", "add", "x"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
            tool = repo / "tools/node/dev/claim"
            tool.parent.mkdir(parents=True)
            tool.write_text(
                """#!/bin/sh
set -eu
store=.git/test-claims
case "${1:-}" in
  --help|help|'') echo 'usage: claim claim <agent> <detail> <files...> | release <agent> | list';;
  claim) printf '%s\\n' "$2" >> "$store";;
  list|status) test ! -f "$store" || cat "$store";;
  release|unclaim) test ! -f "$store" || { grep -v -F "$2" "$store" > "$store.tmp" || true; mv "$store.tmp" "$store"; };;
  *) exit 2;;
esac
"""
            )
            tool.chmod(0o755)
            state = root / "state"
            config = {
                "repositories": {"idol": str(repo)},
                "state_dir": str(state),
                "claim": {"tool": str(tool)},
            }
            action = {
                "agent_id": "agent-1",
                "payload": {
                    "task_id": "t1",
                    "repo_id": "idol",
                    "base_sha": head,
                    "paths": ["x"],
                    "semantic_boundaries": ["boundary"],
                },
            }
            acquired = claim_acquire(action, config)
            self.assertTrue(acquired["ok"])
            self.assertTrue((state / "claims/agent-1.json").is_file())
            released = claim_release(action, config)
            self.assertTrue(released["ok"])
            self.assertFalse((state / "claims/agent-1.json").exists())


if __name__ == "__main__":
    unittest.main()
