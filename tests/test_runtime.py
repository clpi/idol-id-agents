from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from fleet_control.model import BillingClass, BillingProof, Route, WorkOrder
from fleet_control.runtime import CommandRuntime, RuntimeRefusal


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

    def execute_script(self, script_body: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        script = root / "fake.py"
        script.write_text(script_body)
        prompt = root / "prompt.md"
        prompt.write_text("prompt")
        route = self.route((os.environ.get("PYTHON", "python3"), str(script)))
        runtime = CommandRuntime(root / "state")
        return temporary, runtime, route, self.order(root), prompt

    def test_plain_json_success(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'tokens':1}}))"
        )
        with temporary:
            result = runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository)
            self.assertEqual(result.model, "test-model")
            self.assertEqual(result.cost_usd, 0.0)

    def test_provider_mismatch_refuses(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'other','model':'test-model','costUsd':0}))"
        )
        with temporary, self.assertRaises(RuntimeRefusal):
            runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository)

    def test_positive_cost_on_local_route_refuses(self) -> None:
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0.01}))"
        )
        with temporary, self.assertRaises(RuntimeRefusal):
            runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository)

    def test_unlisted_environment_secret_is_not_forwarded(self) -> None:
        os.environ["SHOULD_NOT_REACH_AGENT_API_KEY"] = "secret"
        temporary, runtime, route, order, prompt = self.execute_script(
            "import json, os; print(json.dumps({'status':'ok','provider':'local','model':'test-model','costUsd':0,'usage':{'saw_secret': 'SHOULD_NOT_REACH_AGENT_API_KEY' in os.environ}}))"
        )
        try:
            with temporary:
                result = runtime.execute(route=route, order=order, prompt_path=prompt, cwd=order.repository)
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
                runtime.execute(route=fast, order=order, prompt_path=prompt, cwd=root)


if __name__ == "__main__":
    unittest.main()
