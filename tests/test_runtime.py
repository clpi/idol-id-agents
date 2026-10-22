from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock
import subprocess

from fleet_control.model import BillingClass, BillingProof, Route, WorkOrder
from fleet_control.control import ControlIntegrityError, ControlRefusal
from fleet_control.runtime import CommandRuntime, ExecutionBindingRefusal, RuntimeRefusal, execution_subject
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

    def permit(self, runtime, route, order, prompt, *, cwd=None, attempt_id="runtime-test-attempt"):
        if not runtime.control.status().permitted:
            runtime.control.enable(ttl_seconds=600)
        with runtime.control.transition_permit("claim") as guard:
            self.attempt_permit = guard.issue_attempt_permit(
                attempt_id,
                subject_hash=execution_subject(
                    order=order, route=route, attempt_id=attempt_id,
                    prompt=prompt.read_bytes(), cwd=order.repository if cwd is None else cwd,
                ),
            )

    def execute_script(self, script_body: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        script = root / "fake.py"
        script.write_text(script_body)
        prompt = root / "prompt.md"
        prompt.write_text("prompt")
        route = self.route((os.environ.get("PYTHON", "python3"), str(script)))
        runtime = CommandRuntime(root / "state")
        order = self.order(root)
        self.permit(runtime, route, order, prompt)
        return temporary, runtime, route, order, prompt

    def test_plain_json_success(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'tokens':1}}))"
        )
        with temporary:
            result = runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository, attempt_permit=self.attempt_permit)
            self.assertEqual(result.model, "test-model")
            self.assertEqual(result.cost_usd, 0.0)
            self.assertFalse(result.stdout_truncated)
            self.assertEqual(list(runtime.state_dir.glob("idol-fleet-prompt-*")), [])

    def test_permit_refuses_every_swapped_work_order_field_before_launch(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json, pathlib\n"
            "pathlib.Path(__file__).with_name('provider-ran').write_text('ran')\n"
            "print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0}))\n"
        )
        with temporary:
            route = replace(route, roles=frozenset({"mechanic", "evidence"}))
            route = replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))
            order = replace(order, role="evidence")
            self.permit(runtime, route, order, prompt)
            replacements = {
                "id": "another-order",
                "task_id": "another-task",
                "repository": order.repository / "other-repository",
                "base_sha": "f" * 40,
                "branch": "fleet/another-branch",
                "role": "mechanic",
                "required_outcome": "a different outcome",
                "path_claims": ("different-path",),
                "semantic_claims": ("different/scope",),
                "stop_conditions": ("a different stop",),
                "witnesses": (("false",),),
                "route_ids": (route.id, "another-route"),
                "authority_files": ("different-law",),
                "risk": "high",
                "priority": 2,
                "estimated_seconds": 2,
                "estimated_tokens": 2,
                "reviewer_family": "independent",
                "issue": "issue-2",
                "publish_branch": False,
                "create_draft_pr": False,
                "follow_remote_main": True,
                "allow_no_change": True,
            }
            self.assertEqual(set(replacements), set(order.__dataclass_fields__))
            for field, value in replacements.items():
                with self.subTest(field=field):
                    changed = replace(order, **{field: value})
                    with mock.patch("fleet_control.runtime.subprocess.Popen", wraps=subprocess.Popen) as launch:
                        with self.assertRaises(ExecutionBindingRefusal):
                            runtime.execute(route=route, order=changed, prompt_path=prompt,
                                            cwd=order.repository, attempt_permit=self.attempt_permit)
                    launch.assert_not_called()
                    self.assertFalse((order.repository / "provider-ran").exists())

    def test_permit_refuses_an_independently_allowed_route_substitution(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import pathlib; pathlib.Path(__file__).with_name('provider-ran').write_text('ran')"
        )
        with temporary:
            order = replace(order, route_ids=(route.id, "another-local"))
            self.permit(runtime, route, order, prompt)
            for changed in (
                replace(route, id="another-local"),
                replace(route, command=(*route.command, "changed-argv")),
                replace(route, model="another-model"),
                replace(route, timeout_seconds=route.timeout_seconds + 1),
            ):
                changed = replace(changed, proof=replace(changed.proof, subject_hash=changed.subject_hash))
                with self.subTest(route=changed.id, command=changed.command, model=changed.model,
                                  timeout_seconds=changed.timeout_seconds):
                    with mock.patch("fleet_control.runtime.subprocess.Popen", wraps=subprocess.Popen) as launch:
                        with self.assertRaises(ExecutionBindingRefusal):
                            runtime.execute(route=changed, order=order, prompt_path=prompt,
                                            cwd=order.repository, attempt_permit=self.attempt_permit)
                    launch.assert_not_called()
            self.assertFalse((order.repository / "provider-ran").exists())

    def test_permit_refuses_a_changed_working_directory(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script("raise AssertionError('must not launch')")
        with temporary:
            other = order.repository / "other-worktree"
            other.mkdir()
            with mock.patch("fleet_control.runtime.subprocess.Popen", wraps=subprocess.Popen) as launch:
                with self.assertRaises(ExecutionBindingRefusal):
                    runtime.execute(route=route, order=order, prompt_path=prompt, cwd=other,
                                    attempt_permit=self.attempt_permit)
            launch.assert_not_called()

    def test_subject_cannot_be_rebound_to_another_authenticated_attempt(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script("raise AssertionError('must not launch')")
        with temporary:
            with runtime.control.transition_permit("claim") as guard:
                other = guard.issue_attempt_permit(
                    "different-attempt", subject_hash=self.attempt_permit.subject_hash,
                )
            with mock.patch("fleet_control.runtime.subprocess.Popen", wraps=subprocess.Popen) as launch:
                with self.assertRaises(ExecutionBindingRefusal):
                    runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository,
                                    attempt_permit=other)
            launch.assert_not_called()

    def test_changed_prompt_is_refused_inside_the_process_guard(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script("raise AssertionError('must not launch')")
        with temporary:
            transition = runtime.control.transition_permit
            prompt.write_bytes(b"changed after admission")
            inside_guard = False

            @contextmanager
            def track_guard(boundary, token):
                nonlocal inside_guard
                with transition(boundary, token):
                    inside_guard = True
                    try:
                        yield
                    finally:
                        inside_guard = False

            def verify_at_guard(**arguments):
                self.assertTrue(inside_guard)
                return execution_subject(**arguments)

            with mock.patch.object(runtime.control, "transition_permit", side_effect=track_guard), \
                 mock.patch("fleet_control.runtime.execution_subject", side_effect=verify_at_guard), \
                 mock.patch("fleet_control.runtime.subprocess.Popen", wraps=subprocess.Popen) as launch:
                with self.assertRaises(ExecutionBindingRefusal):
                    runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository,
                                    attempt_permit=self.attempt_permit)
            launch.assert_not_called()
            self.assertEqual(list(runtime.state_dir.glob("idol-fleet-prompt-*")), [])

    def test_both_prompt_arguments_use_the_validated_snapshot(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json, pathlib, sys\n"
            "print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,"
            "'usage':{'file_hex':pathlib.Path(sys.argv[1]).read_bytes().hex(),'text':sys.argv[2]}}))\n"
        )
        with temporary:
            content = "validated café\r\n$(inert)".encode("utf-8")
            prompt.write_bytes(content)
            route = replace(route, command=(*route.command, "{prompt}", "{prompt_text}"))
            route = replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))
            self.permit(runtime, route, order, prompt)
            popen = subprocess.Popen
            snapshots = []

            def change_original_before_launch(command, *args, **kwargs):
                snapshot = Path(command[-2])
                self.assertNotEqual(snapshot, prompt)
                self.assertEqual(snapshot.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
                snapshots.append(snapshot)
                prompt.write_bytes(b"changed after validation")
                return popen(command, *args, **kwargs)

            with mock.patch("fleet_control.runtime.subprocess.Popen", side_effect=change_original_before_launch):
                result = runtime.execute(route=route, order=order, prompt_path=prompt,
                                         cwd=order.repository, attempt_permit=self.attempt_permit)
            self.assertEqual(result.usage["file_hex"], content.hex())
            self.assertEqual(result.usage["text"], content.decode("utf-8"))
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].exists())

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
            self.assertEqual(list(runtime.state_dir.glob("idol-fleet-prompt-*")), [])

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
        self.permit(runtime, route, order, prompt)
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
            self.permit(runtime, route, order, prompt)
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
            order = self.order(root)
            with self.assertRaises(RuntimeRefusal):
                # Patch the test command to exit through RuntimeRefusal without
                # waiting for the full bound by using SIGALRM in the child.
                fast = replace(route, command=("python3", "-c", "import time,signal; signal.alarm(1); time.sleep(30)"))
                fast = replace(fast, proof=replace(fast.proof, subject_hash=fast.subject_hash))
                self.permit(runtime, fast, order, prompt)
                runtime.execute(route=fast, order=order, prompt_path=prompt, cwd=root, attempt_permit=self.attempt_permit)


if __name__ == "__main__":
    unittest.main()
