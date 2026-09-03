from __future__ import annotations

from dataclasses import replace
import unittest

from fleet_control.model import BillingClass, BillingProof, Route
from fleet_control.policy import route_verdict


class PolicyTests(unittest.TestCase):
    def route(self, billing: BillingClass = BillingClass.LOCAL) -> Route:
        route = Route(
            id="route-one",
            provider="local",
            model="model-one",
            provider_family="local",
            runtime="plain",
            command=("python3", "fake.py"),
            parser="plain-json",
            billing=billing,
            proof=BillingProof(
                kind="local-process" if billing is BillingClass.LOCAL else "subscription-oauth",
                subject_hash="pending",
                observed_at=1,
                expires_at=100,
                evidence_hash="e",
                trusted=True,
            ),
            roles=frozenset({"mechanic"}),
        )
        return replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))

    def test_local_route_with_bound_proof_is_allowed(self) -> None:
        self.assertTrue(route_verdict(self.route(), now=50).allowed)

    def test_paygo_is_never_allowed(self) -> None:
        route = self.route(BillingClass.PAYGO)
        verdict = route_verdict(route, now=50)
        self.assertFalse(verdict.allowed)
        self.assertIn("billing-paygo-forbidden", verdict.reasons)

    def test_untrusted_included_word_is_not_proof(self) -> None:
        route = self.route(BillingClass.INCLUDED)
        route = replace(route, proof=replace(route.proof, trusted=False))
        self.assertFalse(route_verdict(route, now=50).allowed)

    def test_expired_proof_is_refused(self) -> None:
        route = self.route()
        self.assertFalse(route_verdict(route, now=100).allowed)

    def test_subject_mismatch_is_refused(self) -> None:
        route = self.route()
        route = replace(route, proof=replace(route.proof, subject_hash="wrong"))
        self.assertFalse(route_verdict(route, now=50).allowed)

    def test_unknown_proof_kind_is_refused(self) -> None:
        route = self.route()
        route = replace(route, proof=replace(route.proof, kind="trust-me"))
        self.assertFalse(route_verdict(route, now=50).allowed)

    def test_exact_zero_cost_model_proof_is_allowed_for_included_route(self) -> None:
        route = self.route(BillingClass.INCLUDED)
        route = replace(route, proof=replace(route.proof, kind="zero-cost-model"))
        self.assertTrue(route_verdict(route, now=50).allowed)


if __name__ == "__main__":
    unittest.main()
