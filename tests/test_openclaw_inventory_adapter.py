from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
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

    def test_active_run_overrides_stored_terminal_status(self) -> None:
        for flag in ("hasActiveRun", "hasActiveSubagentRun"):
            row = adapter.session_row({"key": "one", "status": "completed", flag: True})
            self.assertEqual(row["status"], "running")

    def test_codex_coverage_uses_pid_and_start_time(self) -> None:
        process = SimpleNamespace(
            pid=12, start_time="new", identity=(12, "new"),
            arguments=("/bin/codex", "exec"), directory=Path("/unused"),
        )
        for covered, expected in ((frozenset({(12, "old")}), 1), (frozenset({(12, "new")}), 0)):
            observation = SimpleNamespace(processes=(process,), covered_processes=covered, sessions=())
            with mock.patch.object(adapter, "scan_processes", return_value=(process,)), mock.patch.object(
                adapter, "observe_codex", return_value=observation
            ), mock.patch.object(adapter, "selected_environment", return_value={}):
                result = adapter.process_sessions(10)
            self.assertEqual(len(result), expected)

    def test_active_codex_thread_still_uses_fast_fence(self) -> None:
        active = {"id": "codex-thread-one", "status": "running", "actor": "codex-cli"}
        observation = SimpleNamespace(processes=(), covered_processes=frozenset(), sessions=(active,))
        output = io.StringIO()
        with mock.patch.object(adapter, "scan_processes", return_value=()), mock.patch.object(
            adapter, "observe_codex", return_value=observation
        ), mock.patch.object(adapter, "call") as call, redirect_stdout(output):
            self.assertEqual(adapter.main(), 0)
        call.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["sessions"], [active])

    @staticmethod
    def page(keys, *, offset=0, total=None, more=False):
        return {
            "sessions": [{"key": key, "hasActiveRun": False} for key in keys],
            "count": len(keys), "totalCount": total if total is not None else offset + len(keys),
            "offset": offset, "limitApplied": 200, "hasMore": more,
            "nextOffset": offset + len(keys) if more else None,
        }

    def test_gateway_active_session_after_first_page_is_preserved(self) -> None:
        first = self.page([f"old-{i}" for i in range(200)], total=201, more=True)
        last = self.page(["active"], offset=200)
        last["sessions"][0].update(hasActiveRun=True, status="completed")
        with mock.patch.object(adapter, "call", side_effect=[first, last]) as call:
            result = adapter.visible_gateway_sessions()
        self.assertEqual([(row["id"], row["status"]) for row in result], [("active", "running")])
        self.assertEqual(call.call_args_list[1].args[1], {
            "limit": 200, "offset": 200, "includeGlobal": True, "includeUnknown": True,
        })

    def test_gateway_first_page_omits_zero_offset_but_later_pages_require_it(self) -> None:
        first = self.page(["one"], total=2, more=True)
        del first["offset"]
        last = self.page(["two"], offset=1)
        with mock.patch.object(adapter, "call", side_effect=[first, last]):
            self.assertEqual(adapter.visible_gateway_sessions(), [])
        del last["offset"]
        with mock.patch.object(adapter, "call", side_effect=[first, last]):
            with self.assertRaises(RuntimeError):
                adapter.visible_gateway_sessions()

    def test_gateway_refuses_missing_or_inconsistent_pagination(self) -> None:
        malformed = [
            {"sessions": []},
            {key: value for key, value in self.page([]).items() if key != "nextOffset"},
            self.page([], total=1, more=True),
            dict(self.page(["one"]), count=True),
            dict(self.page(["one"]), nextOffset=1),
            dict(self.page(["one"]), totalCount=2),
            dict(self.page(["one"]), limitApplied=True),
        ]
        for page in malformed:
            with self.subTest(page=page), mock.patch.object(adapter, "call", return_value=page):
                with self.assertRaises(RuntimeError):
                    adapter.visible_gateway_sessions()

    def test_gateway_refuses_reordered_duplicate_or_missing_activity(self) -> None:
        first = self.page(["one"], total=2, more=True)
        repeated = self.page(["one"], offset=1)
        missing = self.page(["one"])
        del missing["sessions"][0]["hasActiveRun"]
        for pages in ([first, repeated], [missing]):
            with mock.patch.object(adapter, "call", side_effect=pages):
                with self.assertRaises(RuntimeError):
                    adapter.visible_gateway_sessions()

    def test_gateway_pagination_has_a_wall_clock_bound(self) -> None:
        with mock.patch.object(adapter.time, "monotonic", side_effect=[0, 21]), mock.patch.object(
            adapter, "call"
        ) as call:
            with self.assertRaises(RuntimeError):
                adapter.visible_gateway_sessions()
        call.assert_not_called()

    def test_visible_idle_does_not_claim_complete_cron_coverage(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(adapter, "process_sessions", return_value=[]), mock.patch.object(
            adapter, "call", return_value=self.page([])
        ), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(adapter.main(), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("cron execution coverage is unavailable", errors.getvalue())

    def test_visible_unknown_gateway_work_fences_without_idle_claim(self) -> None:
        page = self.page(["active"])
        page["sessions"][0]["hasActiveSubagentRun"] = True
        output = io.StringIO()
        with mock.patch.object(adapter, "process_sessions", return_value=[]), mock.patch.object(
            adapter, "call", return_value=page
        ), redirect_stdout(output):
            self.assertEqual(adapter.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["source"], "openclaw-visible-work-fence")
        self.assertEqual(payload["sessions"][0]["status"], "running")

    def test_kimi_code_process_is_fenced(self) -> None:
        self.assertEqual(
            adapter.process_actor(["/home/clp/.local/bin/kimi-code", "--session", "one"]),
            "kimi-cli",
        )


if __name__ == "__main__":
    unittest.main()
