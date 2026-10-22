#!/usr/bin/env python3
"""Required real-direct native integration lane; never skips or falls back.

One canonical law is compiled and directly qualified. Isolated controllers
consume its returned proof unchanged. No model is invoked.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_control.cli import main as fleet_cli
from fleet_control.coordination import (
    acknowledge, assignment_subject, canonical, delegate, load_order, order_mapping, verify_recipe,
)
from fleet_control.controller import FleetController
from fleet_control.model import Assignment, stable_hash
from fleet_control.native_dispatch import (
    CONTROLS, LAW, NativeDispatchRefusal, calibrate_native, evidence_environment,
)
from tests.test_coordination import fixture as physical_fixture
from tests.test_ack_routing import two_route_fixture


NATIVE_CONFIG = None
PROVENANCE = {}


class NativeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if NATIVE_CONFIG is None:
            raise RuntimeError("real native integration requires explicit Live/compiler configuration")
        cls.artifact_directory = tempfile.TemporaryDirectory(prefix="idol-native-qualified-")
        cls.addClassCleanup(cls.artifact_directory.cleanup)
        now = time.time()
        # Qualify one production artifact for cases that can safely share it.
        cls.native_proof = calibrate_native(
            raw_config={"native_dispatch": NATIVE_CONFIG}, state_dir=Path(cls.artifact_directory.name),
            observed_at=now, expires_at=now + 3600,
        )
        PROVENANCE["native_proof"] = cls.native_proof

    def setup_controller(self, *, two_routes=False, isolated_artifact=False):
        temporary, initial, order, route, order_file, claim_log = (two_route_fixture if two_routes else physical_fixture)()
        self.addCleanup(temporary.cleanup)
        raw = json.loads(initial.config_path.read_text())
        raw["native_dispatch"] = NATIVE_CONFIG
        initial.config_path.write_text(json.dumps(raw))
        proof = self.native_proof
        if isolated_artifact:
            now = time.time()
            proof = calibrate_native(raw_config=raw, state_dir=initial.config.state_dir,
                                     observed_at=now, expires_at=now + 3600)
        calibration = json.loads(initial.config.calibration_file.read_text())
        calibration.update(config_hash=stable_hash(raw), native_dispatch=proof)
        initial.config.calibration_file.write_text(json.dumps(calibration))
        controller = FleetController(config_path=initial.config_path)
        self.assertEqual(canonical(controller.native.proof()), canonical(proof))
        route = next(item for item in controller._routes() if item.id == route.id)
        marker = initial.config.state_dir / "provider-launched"
        agent = Path(route.command[1])
        source = agent.read_text()
        agent.write_text(f"from pathlib import Path\nwith Path({str(marker)!r}).open('a') as marker: marker.write('launch\\n')\n" + source)
        environment = mock.patch.dict(os.environ, {"IDOL_TEST_CLAIM_LOG": str(claim_log)})
        environment.start()
        self.addCleanup(environment.stop)
        return controller, order, route, order_file, marker

    def cli(self, controller, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = fleet_cli(["--config", str(controller.config_path), *map(str, arguments)])
        self.assertEqual(status, 0, error.getvalue())
        return json.loads(output.getvalue())

    def accept(self, controller, order, route, order_file):
        order_file.write_bytes(canonical(order_mapping(order)) + b"\n")
        return self.cli(controller, "acknowledge", "--work-order", order_file,
                        "--recipient", order.coordination.recipient, "--route", route.id)

    def dispatch(self, controller, order, route):
        return controller.dispatch(Assignment(order=order, route=route, score=1.0, reason=("native-integration",)))

    def assert_never_started(self, controller, marker):
        self.assertEqual(controller.journal.events({"attempt.started"}), ())
        self.assertFalse(marker.exists())
        self.assertEqual(controller.semantic_claims.list(), ())
        self.assertEqual(controller.path_claims.list(), ())

    def test_missing_ack_refuses_before_started_claims_or_provider(self):
        controller, order, route, order_file, marker = self.setup_controller()
        result = self.dispatch(controller, order, route)
        self.assertEqual(result["error_type"], "NativeDispatchRefusal")
        self.assert_never_started(controller, marker)
        self.assertEqual(controller.journal.events({"dispatch.checked"})[-1]["fact"]["exit"], 8)

    def test_explicit_cli_ack_deliberate_positive(self):
        controller, order, route, order_file, marker = self.setup_controller()
        ack = self.accept(controller, order, route, order_file)
        result = self.dispatch(controller, order, route)
        self.assertIn("commit", result)
        self.assertEqual(marker.read_text(), "launch\n")
        start = controller.journal.events({"attempt.started"})[0]
        launching = controller.journal.events({"attempt.launching"})[0]
        self.assertEqual(start["fact"]["ack_event"], ack["hash"])
        self.assertEqual(start["fact"]["subject_hash"], launching["fact"]["subject_hash"])
        self.assertEqual([row["fact"]["phase"] for row in controller.journal.events({"dispatch.checked"})], ["reserve", "launch"])

    def plan(self, controller, order):
        observation, _ = controller.observe()
        return controller.plan(observation, (order,), controller._routes())

    def test_ack_route_survives_higher_scoring_route_and_dispatches_unchanged_order(self):
        controller, order, route, order_file, marker = self.setup_controller(two_routes=True)
        routes = controller._routes()
        self.assertEqual({item.id for item in routes}, {"local-test", "aaa-preferred"})
        self.assertTrue(all(item.enabled and item.proof.trusted for item in routes))
        accepted = self.accept(controller, order, route, order_file)
        with mock.patch("fleet_control.controller.route_factors", return_value={"local-test": 0.4, "aaa-preferred": 1.6}):
            plan = self.plan(controller, order)
        self.assertEqual(len(plan.assignments), 1)
        assignment = plan.assignments[0]
        self.assertEqual(assignment.route.id, "local-test")
        self.assertIs(assignment.order, order)
        self.assertEqual(assignment_subject(assignment.order), accepted["fact"]["assignment_subject"])
        self.assertIn("commit", controller.dispatch(assignment))
        self.assertEqual(marker.read_text(), "launch\n")
        self.assertEqual([row["fact"]["phase"] for row in controller.journal.events({"dispatch.checked"})], ["reserve", "launch"])

    def test_stale_or_changed_assignment_ack_routes_but_native_refuses(self):
        for changed in ({"expires_at": 1}, {"assignment_subject": "f" * 64}):
            with self.subTest(changed=changed):
                controller, order, route, order_file, marker = self.setup_controller(two_routes=True)
                accepted = self.accept(controller, order, route, order_file)
                controller.journal.append("assignment.ack", {**accepted["fact"], **changed})
                plan = self.plan(controller, order)
                self.assertEqual(len(plan.assignments), 1)
                assignment = plan.assignments[0]
                self.assertEqual(assignment.route.id, "local-test")
                self.assertIs(assignment.order, order)
                result = controller.dispatch(assignment)
                self.assertEqual(result["error_type"], "NativeDispatchRefusal")
                self.assert_never_started(controller, marker)
                self.assertEqual(controller.journal.events({"dispatch.checked"})[-1]["fact"]["exit"], 8)

    def test_new_route_ack_after_planning_refuses_the_old_assignment(self):
        controller, order, route, order_file, marker = self.setup_controller(two_routes=True)
        self.accept(controller, order, route, order_file)
        plan = self.plan(controller, order)
        self.assertEqual(len(plan.assignments), 1)
        assignment = plan.assignments[0]
        self.assertEqual(assignment.route.id, "local-test")
        alternate = next(item for item in controller._routes() if item.id == "aaa-preferred")
        self.accept(controller, order, alternate, order_file)
        result = controller.dispatch(assignment)
        self.assertEqual(result["error_type"], "NativeDispatchRefusal")
        self.assert_never_started(controller, marker)
        self.assertEqual(controller.journal.events({"dispatch.checked"})[-1]["fact"]["exit"], 8)

    def test_corrupt_raw_ack_fields_cannot_start_a_provider(self):
        for field, value in {
            "scope_subject": "f" * 64, "base_sha": "f" * 40, "recipient": "other-worker",
            "mode": "execute", "parent_subject": "f" * 64, "route_subject": "f" * 64,
            "dependency_subject": "f" * 64, "expires_at": 1,
        }.items():
            with self.subTest(field=field):
                controller, order, route, order_file, marker = self.setup_controller()
                ack = self.accept(controller, order, route, order_file)
                controller.journal.append("assignment.ack", {**ack["fact"], field: value})
                self.dispatch(controller, order, route)
                self.assert_never_started(controller, marker)

    def child(self, parent):
        return replace(parent, id="child-order", task_id="child-task", branch="fleet/child",
            coordination=replace(parent.coordination, recipient="child-worker",
                parent_task=parent.task_id, parent_subject=assignment_subject(parent)))

    def test_real_delegate_queue_and_child_ack_positive(self):
        controller, parent, route, parent_file, marker = self.setup_controller()
        self.accept(controller, parent, route, parent_file)
        child = self.child(parent)
        child_input = controller.config.state_dir / "child-input.json"
        child_input.write_text(json.dumps(order_mapping(child)))
        receipt = self.cli(controller, "delegate", "--parent-order", parent_file,
                           "--child-order", child_input, "--owner", parent.coordination.recipient)
        child_file = controller.config.work_orders_dir / "child-order.json"
        self.assertEqual(load_order(child_file), child)
        self.accept(controller, child, route, child_file)
        result = self.dispatch(controller, child, route)
        self.assertIn("commit", result)
        self.assertEqual(marker.read_text(), "launch\n")
        self.assertEqual(controller.journal.events({"assignment.delegated"})[0]["hash"], receipt["hash"])

    def test_child_without_actual_delegation_is_held(self):
        controller, parent, route, parent_file, marker = self.setup_controller()
        self.accept(controller, parent, route, parent_file)
        child = self.child(parent)
        self.accept(controller, child, route, parent_file)
        self.dispatch(controller, child, route)
        self.assert_never_started(controller, marker)

    def test_expired_parent_ack_recovers_only_after_fresh_delegation_and_ack(self):
        controller, parent, route, parent_file, marker = self.setup_controller()
        acknowledge(journal=controller.journal, order=parent, route=route, recipient="worker", now=int(time.time()) - 1000, ttl_seconds=30)
        child = self.child(parent)
        args = dict(journal=controller.journal, work_orders_dir=controller.config.work_orders_dir,
                    parent=parent, child=child, owner="worker")
        old = delegate(**args)
        child_file = controller.config.work_orders_dir / "child-order.json"
        self.accept(controller, child, route, child_file)
        self.dispatch(controller, child, route)
        self.assert_never_started(controller, marker)
        self.accept(controller, parent, route, parent_file)
        renewed = delegate(**args)
        self.assertNotEqual(old["hash"], renewed["hash"])
        self.accept(controller, child, route, child_file)
        self.assertIn("commit", self.dispatch(controller, child, route))
        self.assertEqual(marker.read_text(), "launch\n")

    def recipe(self, controller, order, *, actual_negative=7):
        executable = replace(order, coordination=replace(order.coordination, mode="execute", question="", artifact=""))
        spec = {
            "id": "contained-repair", "verifier": "independent-verifier", "artifact": "recipe.txt", "timeout_seconds": 5,
            "positive": {"command": [sys.executable, "-c", "from pathlib import Path; Path('recipe.txt').write_text('observed controls')"], "expected_exit": 0},
            "negative": {"command": [sys.executable, "-c", f"raise SystemExit({actual_negative})"], "expected_exit": 7},
        }
        row = verify_recipe(journal=controller.journal, control=controller.control, order=executable, specification=spec)
        return replace(executable, coordination=replace(executable.coordination, recipe=row["hash"]))

    def test_actual_recipe_controls_enable_execute(self):
        controller, order, route, order_file, marker = self.setup_controller()
        order = self.recipe(controller, order)
        self.accept(controller, order, route, order_file)
        self.assertIn("commit", self.dispatch(controller, order, route))
        self.assertEqual(marker.read_text(), "launch\n")

    def test_recipe_cannot_be_reused_for_changed_goal_or_failed_negative(self):
        for damage in ("goal", "negative", "artifact"):
            with self.subTest(damage=damage):
                controller, order, route, order_file, marker = self.setup_controller()
                order = self.recipe(controller, order, actual_negative=0 if damage == "negative" else 7)
                if damage == "goal":
                    order = replace(order, required_outcome="a different unverified goal")
                if damage == "artifact":
                    receipt = controller.journal.events({"recipe.observed"})[-1]
                    Path(receipt["fact"]["artifact_path"]).write_text("changed after verification")
                self.accept(controller, order, route, order_file)
                self.dispatch(controller, order, route)
                self.assert_never_started(controller, marker)
                self.assertEqual(controller.journal.events({"dispatch.checked"})[-1]["fact"]["exit"], 2)

    def test_same_permit_cannot_launch_twice(self):
        controller, order, route, order_file, marker = self.setup_controller()
        self.accept(controller, order, route, order_file)
        execute = controller.runtime.execute
        captured = {}
        def remember(**arguments):
            captured.update(arguments)
            return execute(**arguments)
        with mock.patch.object(controller.runtime, "execute", side_effect=remember):
            self.assertIn("commit", self.dispatch(controller, order, route))
        with self.assertRaises(NativeDispatchRefusal):
            execute(**captured)
        self.assertEqual(marker.read_text(), "launch\n")
        self.assertEqual(len(controller.journal.events({"attempt.launching"})), 1)

    def test_concurrent_dispatch_reserves_one_ack_once(self):
        controller, order, route, order_file, marker = self.setup_controller()
        self.accept(controller, order, route, order_file)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.dispatch(controller, order, route), range(2)))
        self.assertEqual(sum("commit" in result for result in results), 1)
        self.assertEqual(marker.read_text(), "launch\n")
        self.assertEqual(len(controller.journal.events({"attempt.started"})), 1)

    def test_ready_task_cannot_be_restarted_under_new_order_name(self):
        controller, order, route, order_file, marker = self.setup_controller()
        self.accept(controller, order, route, order_file)
        self.assertIn("commit", self.dispatch(controller, order, route))
        renamed = replace(order, id="renamed-order", branch="fleet/renamed",
                          coordination=replace(order.coordination, hypothesis="a different proposed approach"))
        self.accept(controller, renamed, route, order_file)
        result = self.dispatch(controller, renamed, route)
        self.assertEqual(result["error_type"], "NativeDispatchRefusal")
        self.assertEqual(controller.journal.events({"dispatch.checked"})[-1]["fact"]["exit"], 16)
        self.assertEqual(marker.read_text(), "launch\n")

    def test_failed_task_budget_survives_order_and_dependency_collection_changes(self):
        controller, order, route, order_file, marker = self.setup_controller()
        projections = []
        results = {}
        stage = "first"

        def observe_projection(**arguments):
            # Observe the exact raw inputs without replacing native decisions,
            # adjusting verdicts, or changing the locked transaction.
            env, facts = evidence_environment(**arguments)
            projections.append({
                "phase": arguments["phase"], "attempt_id": arguments["attempt_id"],
                "environment": dict(env),
            })
            return env, facts

        def context(stage):
            diagnostic = {
                "stage": stage, "results": results,
                "raw_projections": [
                    {**row, "evidence_sha256": hashlib.sha256(canonical(row["environment"])).hexdigest()}
                    for row in projections
                ],
            }
            for name, read in (
                ("provider_marker", lambda: marker.read_text() if marker.exists() else ""),
                ("native_proof", controller.native.proof),
                ("journal", lambda: [row for row in controller.journal.events()
                                     if row["kind"].startswith("attempt.")
                                     or row["kind"] in {"assignment.ack", "dispatch.checked", "dispatch.held"}]),
            ):
                try:
                    diagnostic[name] = read()
                except Exception as exc:
                    diagnostic[name] = {"diagnostic_error_type": type(exc).__name__, "diagnostic_error": str(exc)}
            return json.dumps(diagnostic, sort_keys=True, default=str)

        def assert_witness_failure(attempted, result):
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("error_type"), "ControllerError")
            self.assertTrue(result.get("error", "").startswith(f"witness failed for {attempted.id}: "))

        try:
            with mock.patch("fleet_control.native_dispatch.evidence_environment", side_effect=observe_projection):
                # Keep the original accept/dispatch ordering. Between attempts,
                # inspect only returned values: no extra proof, journal, marker,
                # or diagnostic serialization can warm caches or delay retry.
                failing = replace(order, witnesses=((sys.executable, "-c", "raise SystemExit(7)"),))
                self.accept(controller, failing, route, order_file)
                results["first"] = self.dispatch(controller, failing, route)
                assert_witness_failure(failing, results["first"])
                stage = "second"
                second = replace(failing, id="second-order", branch="fleet/second",
                    authority_files=tuple(reversed(failing.authority_files)) + (failing.authority_files[0],),
                    coordination=replace(failing.coordination, hypothesis="The revised boundary needs a narrower condition"))
                self.accept(controller, second, route, order_file)
                results["second"] = self.dispatch(controller, second, route)
                assert_witness_failure(second, results["second"])
                stage = "third"
                third = replace(failing, id="third-order", branch="fleet/third",
                    coordination=replace(failing.coordination, hypothesis="A third proposed condition does not reset the budget"))
                self.accept(controller, third, route, order_file)
                results["third"] = self.dispatch(controller, third, route)
                self.assertIsInstance(results["third"], dict)
                self.assertEqual(results["third"].get("error_type"), "NativeDispatchRefusal")

            stage = "completed sequence"
            rows = controller.journal.events()
            starts = [row for row in rows if row["kind"] == "attempt.started"]
            launches = [row for row in rows if row["kind"] == "attempt.launching"]
            checks = [row for row in rows if row["kind"] == "dispatch.checked"]
            expected_ids = [results["first"]["attempt_id"], results["second"]["attempt_id"]]
            self.assertNotEqual(*expected_ids)
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(launches), 2)
            self.assertEqual([row["fact"]["attempt_id"] for row in starts], expected_ids)
            self.assertEqual([row["fact"]["attempt_id"] for row in launches], expected_ids)
            self.assertEqual(starts[0]["fact"]["dependency_subject"], starts[1]["fact"]["dependency_subject"])
            self.assertEqual(checks[-1]["fact"]["exit"], 16)
            self.assertEqual(marker.read_text(), "launch\nlaunch\n")
        except Exception:
            # Failure-only physical observation preserves the original exception
            # and the actual dispatch sequence even if diagnostic IO also fails.
            try:
                print(context(stage), file=sys.stderr, flush=True)
            except Exception:
                pass
            raise

    def test_native_artifact_changed_after_reservation_prevents_provider(self):
        controller, order, route, order_file, marker = self.setup_controller(isolated_artifact=True)
        self.accept(controller, order, route, order_file)
        execute = controller.runtime.execute
        artifact = Path(controller.native.proof()["artifact"]["path"])
        def change_artifact(**arguments):
            artifact.chmod(0o700)
            artifact.write_bytes(b"changed native artifact")
            return execute(**arguments)
        with mock.patch.object(controller.runtime, "execute", side_effect=change_artifact):
            result = self.dispatch(controller, order, route)
        self.assertEqual(result["error_type"], "NativeDispatchRefusal")
        self.assertFalse(marker.exists())
        self.assertEqual(controller.semantic_claims.list(), ())

    def test_disabling_native_verdict_is_detected_by_sentinel_control(self):
        controller, order, route, order_file, marker = self.setup_controller()
        # The unchanged negative input has no ACK. First prove real refusal.
        self.dispatch(controller, order, route)
        self.assert_never_started(controller, marker)
        _, facts = evidence_environment(phase="reserve", order=order, route=route, attempt_id="mutation",
            rows=controller.journal.events(), now=int(time.time()), state_dir=controller.config.state_dir)
        # Deliberately damage only the native decision boundary. The same input
        # must now reach the harmless sentinel, proving the control is material.
        with mock.patch.object(controller.native, "decide", return_value=facts):
            result = self.dispatch(controller, order, route)
        self.assertIn("commit", result)
        self.assertEqual(marker.read_text(), "launch\n")


def main(argv=None):
    global NATIVE_CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, type=Path)
    parser.add_argument("--compiler", required=True, type=Path)
    args = parser.parse_args(argv)
    live = args.live.resolve(strict=True)
    compiler = args.compiler.resolve(strict=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=live, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10).stdout.strip()
    NATIVE_CONFIG = {
        "live_repository": str(live), "live_revision": revision,
        "source_sha256": hashlib.sha256((live / LAW).read_bytes()).hexdigest(),
        "controls_sha256": hashlib.sha256((live / CONTROLS).read_bytes()).hexdigest(),
        "compiler": str(compiler), "compiler_sha256": hashlib.sha256(compiler.read_bytes()).hexdigest(),
    }
    agents_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10).stdout.strip()
    PROVENANCE.update(agents_revision=agents_revision, live_revision=revision, native_config=NATIVE_CONFIG)
    print(json.dumps({"stage": "native-integration-start", **PROVENANCE}, sort_keys=True), flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeIntegrationTests))
    print(json.dumps({"stage": "native-integration-complete", "tests": result.testsRun,
                      "failures": len(result.failures), "errors": len(result.errors), "successful": result.wasSuccessful(),
                      **PROVENANCE}, sort_keys=True), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
