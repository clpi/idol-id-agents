from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from fleet_control.cli import main
from fleet_control.coordination import (
    CoordinationError, acknowledge, assignment_subject, delegate, dependency_subject,
    load_order, premise_subject, recover_delegation, verify_recipe,
)
from fleet_control.controller import FleetController
from fleet_control.journal import JournalError
from fleet_control.model import Assignment, Coordination
from fleet_control.native_dispatch import evidence_environment
from tests import test_controller


def fixture():
    temporary, root, repo, state, config, agent, log = test_controller.ControllerTests().fixture(mode="apply")
    controller = FleetController(config_path=config)
    order_file = state / "work-orders/t_controller_1.json"
    raw = json.loads(order_file.read_text())
    raw["coordination"] = {
        "mode": "deliberate", "recipient": "worker", "hypothesis": "inspect the exact failing witness",
        "attempts": 2, "allowance": 60, "recovery": "coordinator",
        "question": "Which contained change satisfies the witness?", "artifact": "src/ok.txt",
    }
    order_file.write_text(json.dumps(raw))
    order = load_order(order_file)
    route = controller._routes()[0]
    return temporary, controller, order, route, order_file, log


class CoordinationTests(unittest.TestCase):
    def test_apply_without_native_configuration_holds_before_claims_or_launch(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            with mock.patch.object(controller.runtime, "execute", side_effect=AssertionError("provider must not run")) as execute:
                result = controller.dispatch(Assignment(order=order, route=route, score=1.0, reason=("negative-control",)))
            self.assertEqual(result["error_type"], "NativeDispatchRefusal")
            self.assertEqual(controller.journal.events({"attempt.started"}), ())
            self.assertEqual(len(controller.journal.events({"dispatch.held"})), 1)
            self.assertEqual(controller.semantic_claims.list(), ())
            self.assertEqual(controller.path_claims.list(), ())
            execute.assert_not_called()

    def test_frozen_nested_declaration_and_recipe_premise_bind_every_other_field(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            with self.assertRaises(Exception):
                order.coordination.mode = "execute"
            recipe = replace(order, coordination=replace(order.coordination, recipe="a" * 64))
            self.assertNotEqual(assignment_subject(order), assignment_subject(recipe))
            self.assertEqual(premise_subject(order), premise_subject(recipe))
            for changed in (
                replace(order, required_outcome="another goal"),
                replace(order, witnesses=(("false",),)),
                replace(order, coordination=replace(order.coordination, recipient="another")),
                replace(order, coordination=replace(order.coordination, hypothesis="a revised explanation")),
            ):
                self.assertNotEqual(premise_subject(order), premise_subject(changed))

    def test_ack_is_explicit_and_binds_selected_route_and_complete_assignment(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            self.assertEqual(controller.journal.events({"assignment.ack"}), ())
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(["--config", str(controller.config_path), "acknowledge", "--work-order", str(path),
                             "--recipient", "worker", "--route", route.id])
            self.assertEqual(code, 0, error.getvalue())
            row = json.loads(output.getvalue())
            self.assertEqual(row["fact"]["assignment_subject"], assignment_subject(order))
            self.assertEqual(row["fact"]["uid"], os.getuid())
            self.assertEqual(row["fact"]["source"], "local-cli")
            env, facts = evidence_environment(phase="reserve", order=order, route=route, attempt_id="attempt",
                rows=controller.journal.events(), now=row["fact"]["accepted_at"], state_dir=controller.config.state_dir)
            self.assertEqual(env["DISPATCH_ACK_EVENT"], row["hash"])
            self.assertEqual(env["DISPATCH_ACK_ROUTE"], env["DISPATCH_ROUTE"])
            changed_route = replace(route, timeout_seconds=route.timeout_seconds + 1)
            changed, _ = evidence_environment(phase="reserve", order=order, route=changed_route, attempt_id="attempt",
                rows=controller.journal.events(), now=row["fact"]["accepted_at"], state_dir=controller.config.state_dir)
            self.assertNotEqual(changed["DISPATCH_ACK_ROUTE"], changed["DISPATCH_ROUTE"])

    def test_dependency_uses_committed_authority_and_real_observation(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            expected = dependency_subject(order, (), observed=False)
            self.assertEqual(expected, dependency_subject(order, (), observed=True))
            (order.repository / order.authority_files[0]).write_text("changed physical authority\n")
            self.assertEqual(expected, dependency_subject(order, (), observed=False))
            self.assertNotEqual(expected, dependency_subject(order, (), observed=True))
            missing = replace(order, coordination=replace(order.coordination, prerequisites=("a" * 64,)))
            with self.assertRaises(CoordinationError):
                dependency_subject(missing, (), observed=False)

    def test_dependency_collection_permutations_do_not_reset_task_budget(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            first = controller.journal.append("attempt.admitted", {"task_id": "prerequisite-one", "commit": "a" * 40})
            second = controller.journal.append("attempt.admitted", {"task_id": "prerequisite-two", "commit": "b" * 40})
            original = replace(order, coordination=replace(order.coordination, prerequisites=(first["hash"], second["hash"])))
            permuted = replace(original,
                authority_files=tuple(reversed(original.authority_files)) + (original.authority_files[0],),
                coordination=replace(original.coordination, prerequisites=tuple(reversed(original.coordination.prerequisites))))
            original_ack = acknowledge(journal=controller.journal, order=original, route=route, recipient="worker")
            permuted_ack = acknowledge(journal=controller.journal, order=permuted, route=route, recipient="worker")
            self.assertNotEqual(original_ack["fact"]["assignment_subject"], permuted_ack["fact"]["assignment_subject"])
            self.assertEqual(original_ack["fact"]["dependency_subject"], permuted_ack["fact"]["dependency_subject"])
            rows = controller.journal.events()
            self.assertEqual(dependency_subject(original, rows, observed=True), dependency_subject(permuted, rows, observed=True))

    def test_journal_transaction_serializes_observation_and_reservation(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            def reserve(_):
                with controller.journal.transaction() as transaction:
                    if transaction.events({"reserved"}):
                        return False
                    transaction.append("reserved", {"ack": "same-event"})
                    return True
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(reserve, range(2))), [False, True])
            with controller.journal.transaction() as transaction:
                rows = transaction.events()
                rows[-1]["fact"]["ack"] = "mutated snapshot"
            self.assertEqual(controller.journal.events()[-1]["fact"]["ack"], "same-event")
            with self.assertRaises(JournalError):
                transaction.append("late", {})

    def child(self, parent):
        return replace(parent, id="child-order", task_id="child-task", branch="fleet/child",
                       coordination=replace(parent.coordination, recipient="child-worker",
                           parent_task=parent.task_id, parent_subject=assignment_subject(parent)))

    def test_delegation_requires_accepted_parent_and_never_overwrites_another_child(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            child = self.child(order)
            args = dict(journal=controller.journal, work_orders_dir=controller.config.work_orders_dir,
                        parent=order, child=child, owner="worker")
            with self.assertRaises(CoordinationError):
                delegate(**args)
            ack = acknowledge(journal=controller.journal, order=order, route=route, recipient="worker")
            row = delegate(**args)
            target = controller.config.work_orders_dir / "child-order.json"
            original = target.read_bytes()
            self.assertEqual(row["fact"]["parent_ack"], ack["hash"])
            self.assertEqual(delegate(**args)["hash"], row["hash"])
            self.assertEqual(target.read_bytes(), original)
            target.write_text("different bytes")
            with self.assertRaises(CoordinationError):
                recover_delegation(journal=controller.journal, work_orders_dir=controller.config.work_orders_dir, event=row["hash"])
            self.assertEqual(target.read_text(), "different bytes")

    def test_delegation_crash_has_durable_recoverable_intent(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            acknowledge(journal=controller.journal, order=order, route=route, recipient="worker")
            child = self.child(order)
            with mock.patch("fleet_control.coordination._publish_queue", side_effect=OSError("planted publication interruption")):
                with self.assertRaises(OSError):
                    delegate(journal=controller.journal, work_orders_dir=controller.config.work_orders_dir,
                             parent=order, child=child, owner="worker")
            intent = controller.journal.events({"assignment.delegated"})[-1]
            self.assertEqual(controller.journal.events({"assignment.queued"}), ())
            recover_delegation(journal=controller.journal, work_orders_dir=controller.config.work_orders_dir, event=intent["hash"])
            self.assertEqual(load_order(controller.config.work_orders_dir / "child-order.json"), child)

    def test_fresh_parent_ack_creates_new_delegation_without_changing_queue_bytes(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            child = self.child(order)
            args = dict(journal=controller.journal, work_orders_dir=controller.config.work_orders_dir,
                        parent=order, child=child, owner="worker")
            acknowledge(journal=controller.journal, order=order, route=route, recipient="worker", now=100, ttl_seconds=30)
            old = delegate(**args, now=101)
            target = controller.config.work_orders_dir / "child-order.json"
            original = target.read_bytes()
            fresh_ack = acknowledge(journal=controller.journal, order=order, route=route, recipient="worker", now=200)
            new = delegate(**args, now=201)
            self.assertNotEqual(old["hash"], new["hash"])
            self.assertEqual(new["fact"]["parent_ack"], fresh_ack["hash"])
            self.assertEqual(target.read_bytes(), original)

    def test_recipe_runs_real_controls_and_retains_raw_failure_evidence(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            spec = {
                "id": "contained-repair", "verifier": "independent", "artifact": "proof.txt", "timeout_seconds": 5,
                "positive": {"command": [sys.executable, "-c", "from pathlib import Path; Path('proof.txt').write_text('control evidence')"], "expected_exit": 0},
                "negative": {"command": [sys.executable, "-c", "raise SystemExit(7)"], "expected_exit": 7},
            }
            row = verify_recipe(journal=controller.journal, control=controller.control, order=order, specification=spec)
            self.assertEqual(row["kind"], "recipe.observed")
            self.assertEqual(row["fact"]["positive_actual"], 0)
            self.assertEqual(row["fact"]["negative_actual"], 7)
            self.assertEqual(Path(row["fact"]["artifact_path"]).read_text(), "control evidence")
            self.assertFalse((order.repository / "proof.txt").exists())
            spec["negative"]["expected_exit"] = 9
            mismatch = verify_recipe(journal=controller.journal, control=controller.control, order=order, specification=spec)
            self.assertEqual(mismatch["fact"]["negative_expected"], 9)
            self.assertEqual(mismatch["fact"]["negative_actual"], 7)

    def test_successful_recipe_leader_cannot_leave_a_mutating_descendant(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            child = (
                "from pathlib import Path; import time; "
                "Path('child-ready').write_text('ready'); time.sleep(0.4); "
                "Path('proof.txt').write_text('late descendant mutation'); "
                "Path('late-mutation').write_text('survived')"
            )
            leader = (
                "from pathlib import Path\nimport subprocess,sys,time\n"
                "Path('proof.txt').write_text('settled control evidence')\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "deadline=time.monotonic()+2\n"
                "while not Path('child-ready').exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                "assert Path('child-ready').exists()\n"
            )
            spec = {
                "id": "descendant-control", "verifier": "independent", "artifact": "proof.txt", "timeout_seconds": 5,
                "positive": {"command": [sys.executable, "-c", leader], "expected_exit": 0},
                "negative": {"command": [sys.executable, "-c", "import time; time.sleep(0.6); raise SystemExit(7)"], "expected_exit": 7},
            }
            row = verify_recipe(journal=controller.journal, control=controller.control, order=order, specification=spec)
            self.assertEqual(row["fact"]["positive_actual"], 0)
            self.assertEqual(row["fact"]["negative_actual"], 7)
            self.assertFalse(row["fact"]["positive"]["timed_out"])
            self.assertEqual(Path(row["fact"]["artifact_path"]).read_text(), "settled control evidence")
            self.assertFalse((Path(row["fact"]["evidence_directory"]) / "checkout/late-mutation").exists())

    def test_disabled_control_prevents_recipe_command_creation(self):
        temporary, controller, order, route, path, log = fixture()
        with temporary:
            controller.control.disable()
            spec = {"id": "recipe", "verifier": "reviewer", "artifact": "src/ok.txt",
                    "positive": {"command": ["true"], "expected_exit": 0},
                    "negative": {"command": ["false"], "expected_exit": 1}}
            with mock.patch("fleet_control.coordination.subprocess.Popen", wraps=__import__("subprocess").Popen) as launch:
                with self.assertRaises(Exception):
                    verify_recipe(journal=controller.journal, control=controller.control, order=order, specification=spec)
            # Exact-base read-only Git observations may run; no archive or control
            # process can pass the disabled maintenance boundary.
            commands = [call.args[0] for call in launch.call_args_list]
            self.assertFalse(any(command[0] in {"true", "false"} or command[:2] == ["git", "archive"] for command in commands))
            self.assertEqual(len(controller.journal.events({"recipe.interrupted"})), 1)


if __name__ == "__main__":
    unittest.main()
