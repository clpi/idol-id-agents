from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import subprocess

from fleet_control.model import BillingClass, BillingProof, Route, WorkOrder
from fleet_control.control import ControlIntegrityError, ControlRefusal
from fleet_control.runtime import CommandRuntime, RuntimeRefusal
from tests.process_helpers import process_stopped


class RuntimeTests(unittest.TestCase):
    def route(self, command: tuple[str, ...], *, timeout: int = 10) -> Route:
        route = Route(
            id="local-test",
            provider="local",
            model="test-model",
            provider_family="local",
            runtime="plain",
            command=command,
            parser="plain-json",
            billing=BillingClass.LOCAL,
            proof=BillingProof(
                kind="local-process",
                subject_hash="pending",
                observed_at=1,
                expires_at=4_000_000_000,
                evidence_hash="e",
                trusted=True,
            ),
            roles=frozenset({"mechanic"}),
            timeout_seconds=timeout,
        )
        return replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))

    def order(self, repository: Path) -> WorkOrder:
        return WorkOrder(
            id="t_runtime_1",
            task_id="runtime-test",
            repository=repository,
            base_sha="0" * 40,
            branch="fleet/runtime-test/t-runtime-1",
            role="mechanic",
            required_outcome="test",
            path_claims=("a",),
            semantic_claims=("test/runtime",),
            stop_conditions=("stop",),
            witnesses=(("true",),),
            route_ids=("local-test",),
            authority_files=("law",),
            risk="low",
            priority=1,
            estimated_seconds=1,
            estimated_tokens=1,
        )

    def permit(self, runtime):
        runtime.control.enable(ttl_seconds=600)
        with runtime.control.transition_permit("claim") as guard:
            self.attempt_permit = guard.issue_attempt_permit("runtime-test-attempt")

    def execute_script(self, script_body: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        script = root / "fake.py"
        script.write_text(script_body)
        prompt = root / "prompt.md"
        prompt.write_text("prompt")
        route = self.route((os.environ.get("PYTHON", "python3"), str(script)))
        runtime = CommandRuntime(root / "state")
        self.permit(runtime)
        return temporary, runtime, route, self.order(root), prompt

    def test_plain_json_success(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'tokens':1}}))"
        )
        with temporary:
            result = runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)
            self.assertEqual(result.model, "test-model")
            self.assertEqual(result.cost_usd, 0.0)
            self.assertFalse(result.stdout_truncated)

    def test_disabled_control_refuses_before_provider_process_creation(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script("raise AssertionError('must not launch')")
        with temporary:
            runtime.control.disable()
            with mock.patch("fleet_control.runtime.subprocess.Popen") as launch:
                with self.assertRaises(ControlRefusal):
                    runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository,
                                    attempt_permit=self.attempt_permit)
            launch.assert_not_called()

    def test_guard_exit_failure_terminates_and_reaps_launched_provider(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script("import time; time.sleep(30)")
        with temporary:
            transition = runtime.control.transition_permit
            popen = subprocess.Popen
            children = []

            @contextmanager
            def failing_exit(boundary, token):
                with transition(boundary, token):
                    yield
                raise ControlIntegrityError("planted replacement after launch")

            def capture_child(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child

            with mock.patch.object(runtime.control, "transition_permit", side_effect=failing_exit), \
                 mock.patch("fleet_control.runtime.subprocess.Popen", side_effect=capture_child):
                with self.assertRaises(ControlIntegrityError):
                    runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository,
                                    attempt_permit=self.attempt_permit)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertTrue(children[0].stdout.closed)
            self.assertTrue(children[0].stderr.closed)

    def test_stdout_truncation_is_explicit(self) -> None:
        bounded, truncated = CommandRuntime._bounded_text("abcd", limit=3)
        self.assertEqual(bounded, "abc\n[controller-output-truncated]\n")
        self.assertTrue(truncated)

    def test_timeout_kills_descendant_after_provider_parent_exits(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import pathlib, subprocess\n"
            "child=subprocess.Popen(['sleep', '30'])\n"
            "pathlib.Path('descendant.pid').write_text(str(child.pid))\n"
        )
        with temporary:
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeRefusal, "exceeded"):
                runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository,
                                attempt_permit=self.attempt_permit)
            self.assertLess(time.monotonic() - started, route.timeout_seconds + 3)
            child_pid = int((order.repository / "descendant.pid").read_text())
            self.assertTrue(process_stopped(child_pid), "provider descendant survived timeout cleanup")

    def test_provider_mismatch_refuses(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'other','model':'test-model','costUsd':0}))"
        )
        with temporary, self.assertRaises(RuntimeRefusal):
            runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)

    def test_openclaw_identity_is_mandatory(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','costUsd':0}))"
        )
        route = replace(route, parser="openclaw-json")
        route = replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))
        with temporary, self.assertRaises(RuntimeRefusal):
            runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)

    def test_prompt_metacharacters_are_one_inert_argument(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json, sys; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'argument':sys.argv[1]}}))"
        )
        with temporary:
            marker = Path(temporary.name) / "must-not-exist"
            value = f"$(touch {marker})\n; touch {marker}"
            prompt.write_text(value)
            route = replace(route, command=(*route.command, "{prompt_text}"))
            route = replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))
            order = replace(order, route_ids=(route.id,))
            result = runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)
            self.assertEqual(result.usage["argument"], value)
            self.assertFalse(marker.exists())

    def test_positive_cost_on_local_route_refuses(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0.01}))"
        )
        with temporary, self.assertRaises(RuntimeRefusal):
            runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)

    def test_unlisted_environment_secret_is_not_forwarded(self) -> None:
        os.environ["SHOULD_NOT_REACH_AGENT_API_KEY"] = "secret"
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json, os; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'saw_secret': 'SHOULD_NOT_REACH_AGENT_API_KEY' in os.environ}}))"
        )
        try:
            with temporary:
                result = runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)
                self.assertFalse(result.usage["saw_secret"])
        finally:
            os.environ.pop("SHOULD_NOT_REACH_AGENT_API_KEY", None)

    def test_timeout_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.md"
            prompt.write_text("prompt")
            route = self.route(("python3", "-c", "import time; time.sleep(30)"), timeout=10)
            # Route validation enforces a ten-second minimum; shorten the runtime
            # object only for this planted timeout control.
            route = replace(route, timeout_seconds=10)
            runtime = CommandRuntime(root / "state")
            self.permit(runtime)
            order = self.order(root)
            with self.assertRaises(RuntimeRefusal):
                # Patch the test command to exit through RuntimeRefusal without
                # waiting for the full bound by using SIGALRM in the child.
                fast = replace(route, command=("python3", "-c", "import time,signal; signal.alarm(1); time.sleep(30)"))
                fast = replace(fast, proof=replace(fast.proof, subject_hash=fast.subject_hash))
                runtime.execute(route=fast, order=order, prompt_path=prompt, cwd=root, attempt_permit=self.attempt_permit)


if __name__ == "__main__":
    unittest.main()
