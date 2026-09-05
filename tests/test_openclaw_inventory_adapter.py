from __future__ import annotations

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "openclaw_inventory_adapter", ROOT / "scripts" / "openclaw-inventory-adapter.py"
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)

NOW = 1_788_577_200.0
COUNTERS = (
    "queueSize", "pendingReplies", "embeddedRuns", "backgroundExecSessions",
    "cronRuns", "activeTasks", "rootRequests", "sessionAdmissions",
    "sessionMutations", "chatRuns", "queuedTurns", "terminalPersistence",
    "terminalSessions",
)


def snapshot(**active):
    counts = {key: active.get(key, 0) for key in COUNTERS}
    counts["totalActive"] = sum(counts.values())
    return {
        "schema": "idol.openclaw.active-work", "version": 1,
        "openclawVersion": "2026.8.1-beta.3",
        "observedAt": datetime.fromtimestamp(NOW, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "idle": counts["totalActive"] == 0, "counts": counts,
    }


class OpenClawInventoryAdapterTests(unittest.TestCase):
    @contextmanager
    def isolated_main_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            isolated_home = Path(temporary) / "home"
            isolated_home.mkdir()
            default_state = isolated_home / ".local/state/idol-fleet-inventory"
            with mock.patch.dict(os.environ, {"HOME": str(isolated_home)}), mock.patch.object(
                adapter, "inventory_snapshot_lock", return_value=nullcontext()
            ):
                yield
            self.assertFalse(default_state.exists())

    def test_gateway_call_uses_one_bounded_form(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(snapshot()))
        with mock.patch.object(adapter, "openclaw_command", return_value=["openclaw"]), mock.patch.object(
            adapter.subprocess, "run", return_value=result
        ) as run, mock.patch.object(adapter, "observe_local_gateway", return_value="same-listener") as observe:
            self.assertEqual(adapter.call(adapter.ACTIVE_WORK_METHOD, {}), snapshot())
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["openclaw", "gateway", "call", "idol.fleet.activeWork.snapshot"])
        self.assertEqual(command[4:8], ["--port", "18789", "--params", "{}"])
        self.assertEqual(command[-2:], ["--timeout", "5000"])
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.assertEqual(observe.call_args_list, [mock.call(port=18789), mock.call(port=18789)])

    def test_listener_change_refuses_even_a_valid_idle_response(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(snapshot()))
        with mock.patch.object(adapter, "openclaw_command", return_value=["openclaw"]), mock.patch.object(
            adapter.subprocess, "run", return_value=result
        ), mock.patch.object(adapter, "observe_local_gateway", side_effect=["old-listener", "new-listener"]):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                adapter.call(adapter.ACTIVE_WORK_METHOD, {})

    def test_unbound_listener_never_starts_the_gateway_client(self):
        with mock.patch.object(adapter, "openclaw_command", return_value=["openclaw"]), mock.patch.object(
            adapter.subprocess, "run"
        ) as run, mock.patch.object(adapter, "observe_local_gateway", side_effect=RuntimeError("unbound listener")):
            with self.assertRaisesRegex(RuntimeError, "unbound listener"):
                adapter.call(adapter.ACTIVE_WORK_METHOD, {})
        run.assert_not_called()

    def run_main(self, raw, processes=()):
        output, errors = io.StringIO(), io.StringIO()
        with self.isolated_main_lock():
            with mock.patch.object(adapter, "process_sessions", return_value=list(processes)), mock.patch.object(
                adapter, "call", return_value=raw
            ) as call, mock.patch.object(adapter.time, "time", return_value=NOW), redirect_stdout(
                output
            ), redirect_stderr(errors):
                code = adapter.main()
        return code, output.getvalue(), errors.getvalue(), call

    def test_complete_idle_snapshot_emits_only_process_metadata(self):
        process = {"id": "owned", "status": "running", "order_id": "order-one"}
        code, output, errors, call = self.run_main(snapshot(), [process])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        call.assert_called_once_with("idol.fleet.activeWork.snapshot", {})
        payload = json.loads(output)
        self.assertEqual(payload, {
            "schema": "idol.fleet.inventory.v1", "observed_at": NOW,
            "source": "openclaw-active-work-snapshot", "sessions": [process], "agents": [],
        })

    def test_every_execution_source_blocks_admission(self):
        for counter in COUNTERS:
            with self.subTest(counter=counter):
                code, output, errors, _ = self.run_main(snapshot(**{counter: 1}))
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("execution is active", errors)

    def test_overlapping_counters_are_not_reported_as_distinct_jobs(self):
        code, output, errors, _ = self.run_main(snapshot(embeddedRuns=1, cronRuns=1, activeTasks=1))
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn("3", errors)
        self.assertNotIn("sessions", errors)

    def test_unknown_or_changed_contract_refuses(self):
        base = snapshot()
        cases = []
        for key in base:
            raw = copy.deepcopy(base)
            del raw[key]
            cases.append(raw)
        cases += [
            dict(base, schema="other"), dict(base, version=True), dict(base, version=2),
            dict(base, openclawVersion="2026.8.2"), dict(base, idle=1),
            dict(base, blockers=["private-session-content"]),
            {"sessions": [], "hasMore": False},
        ]
        for raw in cases:
            with self.subTest(keys=list(raw)):
                code, output, errors, _ = self.run_main(raw)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertNotIn("private-session-content", errors)

    def test_missing_extra_or_malformed_counters_refuse(self):
        cases = []
        for key in (*COUNTERS, "totalActive"):
            raw = snapshot()
            del raw["counts"][key]
            cases.append(raw)
        for value in (True, -1, 0.0, "0", None, 2**53):
            raw = snapshot()
            raw["counts"]["cronRuns"] = value
            cases.append(raw)
        raw = snapshot()
        raw["counts"]["futureCounter"] = 0
        cases.extend([raw, dict(snapshot(), counts=[]), dict(snapshot(), idle=False)])
        raw = snapshot(cronRuns=1)
        raw["idle"] = True
        cases.append(raw)
        raw = snapshot()
        raw["counts"]["totalActive"] = 1
        cases.append(raw)
        for raw in cases:
            code, output, _, _ = self.run_main(raw)
            self.assertEqual(code, 2)
            self.assertEqual(output, "")

    def test_stale_future_or_malformed_observation_time_refuses(self):
        for stamp in (
            "2020-01-01T00:00:00.000Z", "2099-01-01T00:00:00.000Z",
            "2026-99-99T00:00:00.000Z", "2026-09-05T00:00:00", 42, None,
            snapshot()["observedAt"].replace("Z", "+00:00"),
        ):
            with self.subTest(stamp=stamp):
                code, output, _, _ = self.run_main(dict(snapshot(), observedAt=stamp))
                self.assertEqual(code, 2)
                self.assertEqual(output, "")

    def test_clock_reversal_refuses(self):
        with mock.patch.object(adapter, "call", return_value=snapshot()), mock.patch.object(
            adapter.time, "time", side_effect=[NOW, NOW - 1]
        ):
            with self.assertRaisesRegex(RuntimeError, "observation window"):
                adapter.require_gateway_idle()

    def test_unknown_method_timeout_and_error_refuse_without_fallback(self):
        for failure in (RuntimeError("unknown method"), RuntimeError("gateway timeout")):
            output, errors = io.StringIO(), io.StringIO()
            with self.isolated_main_lock():
                with mock.patch.object(adapter, "process_sessions", return_value=[]), mock.patch.object(
                    adapter, "call", side_effect=failure
                ) as call, redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(adapter.main(), 2)
            call.assert_called_once_with("idol.fleet.activeWork.snapshot", {})
            self.assertEqual(output.getvalue(), "")

    def test_unidentified_process_uses_fast_fence_without_gateway(self):
        process = {"id": "process-1-2", "status": "running", "host": "r16", "actor": "codex-cli"}
        code, output, _, call = self.run_main(snapshot(), [process])
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertEqual(json.loads(output)["source"], "local-process-fast-fence")
        self.assertEqual(json.loads(output)["sessions"], [process])

    def test_codex_coverage_uses_pid_and_start_time(self):
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

    def test_active_codex_thread_still_uses_fast_fence(self):
        active = {"id": "codex-thread-one", "status": "running", "actor": "codex-cli"}
        observation = SimpleNamespace(processes=(), covered_processes=frozenset(), sessions=(active,))
        output = io.StringIO()
        with self.isolated_main_lock():
            with mock.patch.object(adapter, "scan_processes", return_value=()), mock.patch.object(
                adapter, "observe_codex", return_value=observation
            ), mock.patch.object(adapter, "call") as call, redirect_stdout(output):
                self.assertEqual(adapter.main(), 0)
        call.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["sessions"], [active])

    def test_kimi_code_process_is_fenced(self):
        self.assertEqual(adapter.process_actor(["/home/clp/.local/bin/kimi-code", "--session", "one"]), "kimi-cli")

    def test_overlapping_full_observations_are_serialized(self):
        active = 0
        maximum = 0
        guard = threading.Lock()
        start = threading.Barrier(3)

        def observed_processes(observed_at):
            nonlocal active, maximum
            self.assertEqual(observed_at, NOW)
            with guard:
                active += 1
                maximum = max(maximum, active)
            return []

        def observed_snapshot(method, params):
            nonlocal active
            self.assertEqual((method, params), (adapter.ACTIVE_WORK_METHOD, {}))
            time.sleep(0.05)
            with guard:
                active -= 1
            return snapshot()

        def observe(results):
            start.wait()
            results.append(json.loads(adapter.observe_inventory()))

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve() / "shared-inventory"
            results = []
            with mock.patch.dict(
                os.environ,
                {"IDOL_FLEET_INVENTORY_STATE_DIR": str(state)},
            ), mock.patch.object(adapter, "process_sessions", side_effect=observed_processes), mock.patch.object(
                adapter, "call", side_effect=observed_snapshot
            ) as call, mock.patch.object(adapter.time, "time", return_value=NOW):
                threads = [threading.Thread(target=observe, args=(results,)) for _ in range(2)]
                for thread in threads:
                    thread.start()
                start.wait()
                for thread in threads:
                    thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(maximum, 1)
        self.assertEqual(call.call_count, 2)
        self.assertEqual([row["source"] for row in results], [
            "openclaw-active-work-snapshot", "openclaw-active-work-snapshot",
        ])


if __name__ == "__main__":
    unittest.main()
