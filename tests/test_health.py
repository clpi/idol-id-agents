from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fleet_control.health import apply_circuits, circuit_state, record_failure, record_success
from fleet_control.journal import Journal
from fleet_control.model import BillingClass, BillingProof, Route


class HealthTests(unittest.TestCase):
    def route(self) -> Route:
        route = Route(
            id="route-one",
            provider="provider",
            model="model",
            provider_family="family",
            runtime="plain",
            command=("true",),
            parser="plain-json",
            billing=BillingClass.LOCAL,
            proof=BillingProof("local-process", "pending", 1, 10_000, "e", True),
            roles=frozenset({"mechanic"}),
        )
        return replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))

    def test_failure_opens_then_expires_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(Path(temporary) / "journal.jsonl")
            route = self.route()
            record_failure(journal, route, error_type="RuntimeRefusal", error="quota", at=100)
            routes, states = apply_circuits((route,), journal, now=399)
            self.assertFalse(routes[0].enabled)
            self.assertTrue(states[0].open(399))
            routes, _ = apply_circuits((route,), journal, now=400)
            self.assertTrue(routes[0].enabled)

    def test_success_closes_and_resets_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(Path(temporary) / "journal.jsonl")
            route = self.route()
            record_failure(journal, route, error_type="RuntimeRefusal", error="quota", at=100)
            record_success(journal, route, at=101)
            self.assertEqual(circuit_state(journal, route).consecutive_failures, 0)
            routes, _ = apply_circuits((route,), journal, now=102)
            self.assertTrue(routes[0].enabled)

    def test_circuit_scan_captures_dynamic_route_subject_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(Path(temporary) / "journal.jsonl")
            route = self.route()
            for observed_at in range(1, 6):
                journal.append(
                    "route.failed",
                    {"route_id": route.id, "route_subject": "bound", "error": "transient"},
                    at=observed_at,
                )
            with mock.patch.object(
                Route,
                "subject_hash",
                new_callable=mock.PropertyMock,
                return_value="bound",
            ) as subject_hash:
                state = circuit_state(journal, route)
            self.assertEqual(state.consecutive_failures, 5)
            self.assertEqual(subject_hash.call_count, 1)


if __name__ == "__main__":
    unittest.main()
