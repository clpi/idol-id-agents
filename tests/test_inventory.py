from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from fleet_control.inventory import (
    InventoryConfig,
    InventoryRefusal,
    SessionFact,
    cancel_session,
    load_adoptions,
    observe_inventory,
)


class InventoryTests(unittest.TestCase):
    now = 1_000_000.0

    def config(self, root: Path, script: Path, *, cancel: Path | None = None) -> InventoryConfig:
        raw = {
            "enabled": True,
            "command": ["python3", str(script)],
            "cancel_command": ["python3", str(cancel), "{session_id}"] if cancel else [],
            "auth_env": [],
            "timeout_seconds": 10,
            "max_age_seconds": 300,
            "cancel_owned_sessions": cancel is not None,
            "adoptions_file": str(root / "adoptions.json"),
        }
        return InventoryConfig.from_mapping(raw, state_dir=root)

    def inventory_script(self, root: Path, payload: dict) -> Path:
        path = root / "inventory.py"
        path.write_text("import json\n" + f"print(json.dumps({payload!r}))\n")
        return path

    def payload(self) -> dict:
        return {
            "schema": "idol.fleet.inventory.v1",
            "observed_at": self.now,
            "source": "test",
            "sessions": [
                {
                    "id": "session-1",
                    "status": "running",
                    "last_activity": self.now,
                    "provider": "local",
                    "model": "model",
                    "attempt_id": "attempt-1",
                    "order_id": "order-1",
                    "task_id": "task-1",
                    "base_sha": "a" * 40,
                    "host": "host",
                    "actor": "agent",
                }
            ],
            "agents": [{"id": "agent", "status": "running", "provider": "local", "model": "model", "host": "host", "role": "mechanic"}],
        }

    def test_metadata_only_inventory_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root, self.inventory_script(root, self.payload()))
            observation = observe_inventory(config, now=self.now)
            self.assertEqual(observation.sessions[0].order_id, "order-1")
            self.assertEqual(observation.agents[0]["id"], "agent")

    def test_prompt_or_transcript_key_refuses_entire_observation(self) -> None:
        for forbidden in ("prompt", "messages", "content", "transcript", "api_key"):
            with self.subTest(forbidden=forbidden), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payload = self.payload()
                payload[forbidden] = "secret"
                config = self.config(root, self.inventory_script(root, payload))
                with self.assertRaises(InventoryRefusal):
                    observe_inventory(config, now=self.now)

    def test_unknown_session_field_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.payload()
            payload["sessions"][0]["workspace"] = "/secret/path"
            config = self.config(root, self.inventory_script(root, payload))
            with self.assertRaises(InventoryRefusal):
                observe_inventory(config, now=self.now)

    def test_adoption_requires_explicit_approval_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "adoptions.json"
            path.write_text(json.dumps([{
                "session_id":"session-1",
                "approved":True,
                "task_id":"task-1",
                "order_id":"order-1",
                "base_sha":"a"*40,
                "approved_by":"user:chris",
                "observed_at":self.now
            }]))
            adoption = load_adoptions(path)["session-1"]
            self.assertTrue(adoption.approved)
            self.assertEqual(adoption.approved_by, "user:chris")

    def test_cancel_result_must_match_exact_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.payload()
            inventory_script = self.inventory_script(root, payload)
            cancel = root / "cancel.py"
            cancel.write_text(
                "import json, sys\n"
                "print(json.dumps({'status':'cancelled','session_id':sys.argv[1]}))\n"
            )
            config = self.config(root, inventory_script, cancel=cancel)
            session = SessionFact.from_mapping(payload["sessions"][0], observed_at=self.now)
            result = cancel_session(config, session, attempt_id="attempt-1")
            self.assertEqual(result["status"], "cancelled")

    def test_cancel_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.payload()
            inventory_script = self.inventory_script(root, payload)
            cancel = root / "cancel.py"
            cancel.write_text("import json; print(json.dumps({'status':'cancelled','session_id':'other'}))\n")
            config = self.config(root, inventory_script, cancel=cancel)
            session = SessionFact.from_mapping(payload["sessions"][0], observed_at=self.now)
            with self.assertRaises(InventoryRefusal):
                cancel_session(config, session, attempt_id="attempt-1")


if __name__ == "__main__":
    unittest.main()
