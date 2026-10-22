from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import time
import unittest
from unittest import mock

from fleet_control.calibration import calibrate
from fleet_control.controller import FleetController, load_config
from fleet_control.coordination import (
    acknowledge, assignment_subject, canonical, order_mapping, route_subject,
)
from fleet_control.native_dispatch import evidence_environment
from tests.test_coordination import fixture


def two_route_fixture():
    """Two genuinely calibrated local routes; no native-policy substitute."""
    temporary, initial, order, route, order_file, claim_log = fixture()
    try:
        raw = json.loads(initial.config_path.read_text())
        alternative = deepcopy(raw["routes"][0])
        alternative["id"] = "aaa-preferred"
        raw["routes"].append(alternative)
        initial.config_path.write_text(json.dumps(raw))
        raw, parsed = load_config(initial.config_path)
        calibrate(raw_config=raw, routes=parsed.routes, output=parsed.calibration_file, ttl_seconds=600)
        controller = FleetController(config_path=initial.config_path)
        order = replace(order, route_ids=("local-test", "aaa-preferred"))
        order_file.write_bytes(canonical(order_mapping(order)) + b"\n")
        route = next(item for item in controller._routes() if item.id == "local-test")
        return temporary, controller, order, route, order_file, claim_log
    except BaseException:
        temporary.cleanup()
        raise


class AckRoutePlanningTests(unittest.TestCase):
    def plan(self, controller, order, routes=None):
        observation, _ = controller.observe()
        return controller.plan(observation, (order,), controller._routes() if routes is None else routes)

    def test_acknowledged_route_precedes_score_and_lexical_ties(self):
        temporary, controller, order, route, order_file, claim_log = two_route_fixture()
        with temporary:
            routes = controller._routes()
            self.assertTrue(all(item.enabled and item.proof.trusted for item in routes))
            # Existing previews still rank all eligible routes before acceptance.
            self.assertEqual(self.plan(controller, order, routes).assignments[0].route.id, "aaa-preferred")
            accepted = acknowledge(journal=controller.journal, order=order, route=route, recipient="worker")
            for scores in ({}, {"local-test": 0.4, "aaa-preferred": 1.6}):
                with self.subTest(scores=scores), mock.patch("fleet_control.controller.route_factors", return_value=scores):
                    plan = self.plan(controller, order, routes)
                    assignment = plan.assignments[0]
                    self.assertEqual(assignment.route.id, "local-test")
                    self.assertIs(assignment.order, order)
                    self.assertEqual(assignment_subject(assignment.order), accepted["fact"]["assignment_subject"])
                    env, _ = evidence_environment(phase="reserve", order=assignment.order, route=assignment.route,
                        attempt_id="planned-attempt", rows=controller.journal.events(), now=int(plan.observed_at),
                        state_dir=controller.config.state_dir)
                    self.assertEqual(env["DISPATCH_ACK_EVENT"], accepted["hash"])
                    self.assertEqual(env["DISPATCH_ACK_ROUTE"], env["DISPATCH_ROUTE"])

    def test_latest_same_task_ack_is_used_without_falling_back_to_an_older_assignment(self):
        temporary, controller, order, route, order_file, claim_log = two_route_fixture()
        with temporary:
            alternate = next(item for item in controller._routes() if item.id == "aaa-preferred")
            acknowledge(journal=controller.journal, order=order, route=route, recipient="worker")
            unrelated = replace(order, id="other-order", task_id="other-task")
            acknowledge(journal=controller.journal, order=unrelated, route=alternate, recipient="worker")
            self.assertEqual(self.plan(controller, order).assignments[0].route.id, "local-test")
            changed = replace(order, id="new-order", required_outcome="a different accepted assignment")
            accepted = acknowledge(journal=controller.journal, order=changed, route=alternate, recipient="worker")
            plan = self.plan(controller, order)
            assignment = plan.assignments[0]
            self.assertEqual(assignment.route.id, "aaa-preferred")
            self.assertIs(assignment.order, order)
            env, _ = evidence_environment(phase="reserve", order=order, route=assignment.route,
                attempt_id="old-assignment", rows=controller.journal.events(), now=int(plan.observed_at),
                state_dir=controller.config.state_dir)
            self.assertEqual(env["DISPATCH_ACK_EVENT"], accepted["hash"])
            # Selection is not permission: native still sees the mismatched assignment.
            self.assertNotEqual(env["DISPATCH_ACK_ASSIGNMENT"], env["DISPATCH_ASSIGNMENT"])

    def test_malformed_or_mismatched_ack_route_never_falls_back(self):
        temporary, controller, order, route, order_file, claim_log = two_route_fixture()
        with temporary:
            alternate = next(item for item in controller._routes() if item.id == "aaa-preferred")
            accepted = acknowledge(journal=controller.journal, order=order, route=route, recipient="worker")
            for changed in (
                {"route_subject": "f" * 64}, {"route_subject": ""}, {"route_subject": None},
                {"route_subject": []}, {"route_id": "missing-route"},
                {"route_subject": route_subject(alternate)},
            ):
                with self.subTest(changed=changed):
                    controller.journal.append("assignment.ack", {**accepted["fact"], **changed})
                    plan = self.plan(controller, order)
                    self.assertEqual(plan.assignments, ())
                    self.assertTrue(any("acknowledged-route-unavailable" in item.reasons for item in plan.rejections))

    def test_unavailable_or_reconfigured_ack_route_never_falls_back(self):
        temporary, controller, order, route, order_file, claim_log = two_route_fixture()
        with temporary:
            alternate = next(item for item in controller._routes() if item.id == "aaa-preferred")
            acknowledge(journal=controller.journal, order=order, route=route, recipient="worker")
            changed_timeout = replace(route, timeout_seconds=route.timeout_seconds + 1)
            self.assertEqual(changed_timeout.subject_hash, route.subject_hash)
            self.assertNotEqual(route_subject(changed_timeout), route_subject(route))
            for label, routes, reason in (
                ("removed", (alternate,), "acknowledged-route-unavailable"),
                ("timeout", (changed_timeout, alternate), "acknowledged-route-unavailable"),
                ("disabled", (replace(route, enabled=False), alternate), "route-disabled"),
                ("expired-proof", (replace(route, proof=replace(route.proof, expires_at=1)), alternate),
                 "billing-proof-invalid-or-expired"),
                ("changed-role", (replace(route, roles=frozenset({"reviewer"})), alternate), "acknowledged-route-unavailable"),
            ):
                with self.subTest(label=label):
                    plan = self.plan(controller, order, routes)
                    self.assertEqual(plan.assignments, ())
                    self.assertTrue(any(reason in item.reasons for item in plan.rejections), plan.rejections)

    def test_ack_cannot_expand_work_order_route_set(self):
        temporary, controller, order, route, order_file, claim_log = two_route_fixture()
        with temporary:
            alternate = next(item for item in controller._routes() if item.id == "aaa-preferred")
            narrowed = replace(order, route_ids=("local-test",))
            acknowledge(journal=controller.journal, order=narrowed, route=alternate, recipient="worker")
            plan = self.plan(controller, narrowed)
            self.assertEqual(plan.assignments, ())
            self.assertTrue(any("acknowledged-route-unavailable" in item.reasons for item in plan.rejections))

    def test_expired_ack_remains_raw_evidence_for_native_refusal(self):
        temporary, controller, order, route, order_file, claim_log = two_route_fixture()
        with temporary:
            accepted = acknowledge(journal=controller.journal, order=order, route=route, recipient="worker",
                                   now=int(time.time()) - 31, ttl_seconds=30)
            plan = self.plan(controller, order)
            assignment = plan.assignments[0]
            self.assertEqual(assignment.route.id, "local-test")
            env, _ = evidence_environment(phase="reserve", order=order, route=assignment.route,
                attempt_id="expired-ack", rows=controller.journal.events(), now=int(plan.observed_at),
                state_dir=controller.config.state_dir)
            self.assertEqual(env["DISPATCH_ACK_EVENT"], accepted["hash"])
            self.assertLess(int(env["DISPATCH_ACK_EXPIRES"]), int(env["DISPATCH_NOW"]))
            self.assertEqual(controller.journal.events({"attempt.started"}), ())


if __name__ == "__main__":
    unittest.main()
