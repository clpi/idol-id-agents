from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "openclaw_inventory_adapter",
    ROOT / "scripts" / "openclaw-inventory-adapter.py",
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


class OpenClawInventoryAdapterTests(unittest.TestCase):
    def test_gateway_call_uses_one_bounded_documented_form(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"sessions":[]}',
            stderr=None,
        )
        with mock.patch.object(adapter, "openclaw_command", return_value=["openclaw"]), mock.patch.object(
            adapter.subprocess,
            "run",
            return_value=result,
        ) as run:
            self.assertEqual(adapter.call("sessions.list", {"limit": 10}), {"sessions": []})
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["openclaw", "gateway", "call", "sessions.list"])
        self.assertIn("--params", command)
        self.assertEqual(command[-2:], ["--timeout", "5000"])
        self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_active_gateway_session_without_status_is_running(self) -> None:
        row = adapter.session_row(
            {
                "sessionId": "run-one",
                "hasActiveRun": True,
                "modelProvider": "zai",
                "model": "glm-5",
            }
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["provider"], "zai")

    def test_unidentified_process_uses_fast_fence_without_gateway(self) -> None:
        process = {
            "id": "process-1-2",
            "status": "running",
            "host": "r16",
            "actor": "codex-cli",
        }
        output = io.StringIO()
        with mock.patch.object(adapter, "process_sessions", return_value=[process]), mock.patch.object(
            adapter,
            "call",
        ) as call, redirect_stdout(output):
            self.assertEqual(adapter.main(), 0)
        call.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["source"], "local-process-fast-fence")
        self.assertEqual(payload["sessions"], [process])


if __name__ == "__main__":
    unittest.main()
