"""Test-only native port for existing physical-control unit tests.

This deliberately supplies no native-policy evidence. Native semantics and the
complete real adapter are exercised by native_dispatch_integration.py against
the exact compiled Live law; this fixture is never selected by configuration.
"""
from fleet_control.coordination import assignment_subject, digest, route_subject, scope_subject


class TransportOnlyGate:
    def __init__(self, *, journal, **unused):
        self.journal = journal

    def subject(self):
        return digest({"fixture": "physical-controller-unit-tests"})

    def decide(self, *, order, route, **unused):
        return {
            "assignment_subject": assignment_subject(order), "scope_subject": scope_subject(order),
            "dependency_subject": digest({"fixture_base": order.base_sha}),
            "mode": "deliberate", "hypothesis": "physical-unit-test", "recipient": "fixture-worker",
            "ack_event": digest({"fixture_ack": order.id}), "native_route_subject": route_subject(route),
            "native_subject": self.subject(), "native_evidence_sha256": digest({"fixture": "no-native-proof"}),
        }
