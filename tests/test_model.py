from __future__ import annotations

from dataclasses import replace
import pathlib
import tempfile
import unittest

from fleet_control.model import BillingClass, BillingProof, Route, WorkOrder


class ModelTests(unittest.TestCase):
    def proof(self, *, trusted: bool = False) -> BillingProof:
        return BillingProof(
            kind="local-process",
            subject_hash="x",
            observed_at=1,
            expires_at=10,
            evidence_hash="e",
            trusted=trusted,
        )

    def route(self) -> Route:
        proof = self.proof()
        route = Route(
            id="local-test",
            provider="local",
            model="test-model",
            provider_family="local",
            runtime="plain",
            command=("python3", "fake.py"),
            parser="plain-json",
            billing=BillingClass.LOCAL,
            proof=proof,
            roles=frozenset({"mechanic"}),
        )
        return route

    def order(self, repository: pathlib.Path) -> WorkOrder:
        return WorkOrder(
            id="t_test_1",
            task_id="issue-1",
            repository=repository,
            base_sha="0" * 40,
            branch="fleet/issue-1/t-test-1",
            role="mechanic",
            required_outcome="Change one claimed file.",
            path_claims=("src/example.id",),
            semantic_claims=("evidence/example",),
            stop_conditions=("Stop on ambiguity.",),
            witnesses=(("git", "diff", "--check"),),
            route_ids=("local-test",),
            authority_files=("docs/spec/law.md",),
            risk="low",
            priority=50,
            estimated_seconds=60,
            estimated_tokens=1000,
        )

    def test_route_subject_does_not_depend_on_proof(self) -> None:
        route = self.route()
        other = replace(route, proof=self.proof(trusted=True))
        self.assertEqual(route.subject_hash, other.subject_hash)

    def test_work_order_requires_exact_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            order = self.order(pathlib.Path(temporary))
            self.assertEqual(order.base_sha, "0" * 40)
            with self.assertRaises(ValueError):
                replace(order, base_sha="main")

    def test_work_order_rejects_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            order = self.order(pathlib.Path(temporary))
            values = {field: getattr(order, field) for field in order.__dataclass_fields__}
            values["path_claims"] = ("../secret",)
            with self.assertRaises(ValueError):
                WorkOrder(**values)

    def test_no_change_is_limited_to_evidence_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            order = self.order(pathlib.Path(temporary))
            with self.assertRaisesRegex(ValueError, "evidence-producing roles"):
                replace(order, allow_no_change=True)
            evidence = replace(order, role="evidence", allow_no_change=True)
            self.assertTrue(evidence.allow_no_change)

    def test_route_rejects_unknown_parser(self) -> None:
        route = self.route()
        values = {field: getattr(route, field) for field in route.__dataclass_fields__}
        values["parser"] = "guess"
        with self.assertRaises(ValueError):
            Route(**values)

    def test_billing_proof_has_expiry(self) -> None:
        proof = self.proof(trusted=True)
        self.assertTrue(proof.valid(now=5))
        self.assertFalse(proof.valid(now=10))


if __name__ == "__main__":
    unittest.main()
