from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from fleet_control.journal import Journal
from fleet_control.performance import (
    OutcomeReceipt,
    OutcomeRefusal,
    record_outcome,
    route_factors,
)


class PerformanceTests(unittest.TestCase):
    def receipt(self, **overrides) -> OutcomeReceipt:
        values = {
            "schema": "idol.fleet.outcome.v1",
            "attempt_id": "attempt-one",
            "order_id": "order-one",
            "task_id": "task-one",
            "route_id": "route-one",
            "verdict": "admitted",
            "accepted_commit": "a" * 40,
            "reviewer_families": ("anthropic",),
            "semantic_increment": 2.0,
            "accepted_tokens": 10_000,
            "defects": 0,
            "observed_at": 20.0,
            "evidence": "review-and-gate-receipt",
        }
        values.update(overrides)
        return OutcomeReceipt(**values)

    def journal(self, root: Path) -> Journal:
        journal = Journal(root / "history.jsonl")
        journal.append(
            "attempt.started",
            {
                "attempt_id":"attempt-one",
                "order_id":"order-one",
                "task_id":"task-one",
                "route_id":"route-one"
            },
            at=1,
        )
        journal.append(
            "attempt.ready",
            {
                "attempt_id":"attempt-one",
                "order_id":"order-one",
                "task_id":"task-one",
                "route_id":"route-one",
                "commit":"a" * 40,
            },
            at=2,
        )
        return journal

    def test_admitted_outcome_requires_independent_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.journal(Path(temporary))
            with self.assertRaises(OutcomeRefusal):
                record_outcome(
                    journal,
                    self.receipt(reviewer_families=("openai",)),
                    route_families={"route-one":"openai"},
                )

    def test_mapping_refuses_invalid_reviewer_family_names(self) -> None:
        reviewer_sets = (
            ("",),
            ("   ",),
            ("anthropic", "\t"),
            (" openai ",),
            (123,),
            (None,),
            ({},),
        )
        for reviewer_families in reviewer_sets:
            with self.subTest(reviewer_families=reviewer_families):
                with self.assertRaises(OutcomeRefusal):
                    OutcomeReceipt.from_mapping(
                        asdict(self.receipt(reviewer_families=reviewer_families))
                    )

    def test_record_outcome_refuses_invalid_reviewer_family_names(self) -> None:
        reviewer_sets = (
            ("",),
            ("   ",),
            ("anthropic", "\t"),
            (" openai ",),
            "anthropic",
        )
        for reviewer_families in reviewer_sets:
            with self.subTest(reviewer_families=reviewer_families):
                with tempfile.TemporaryDirectory() as temporary:
                    journal = self.journal(Path(temporary))
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(reviewer_families=reviewer_families),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_mapping_refuses_non_finite_measurements(self) -> None:
        for field, value in (
            ("semantic_increment", float("nan")),
            ("semantic_increment", float("inf")),
            ("observed_at", float("nan")),
            ("observed_at", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(OutcomeRefusal):
                    OutcomeReceipt.from_mapping(asdict(self.receipt(**{field: value})))

    def test_record_outcome_refuses_non_finite_measurements(self) -> None:
        for field, value in (
            ("semantic_increment", float("nan")),
            ("semantic_increment", float("inf")),
            ("observed_at", float("nan")),
            ("observed_at", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                with tempfile.TemporaryDirectory() as temporary:
                    journal = self.journal(Path(temporary))
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(**{field: value}),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_admitted_outcome_changes_route_factor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.journal(Path(temporary))
            record_outcome(
                journal,
                self.receipt(),
                route_families={"route-one":"openai"},
            )
            self.assertGreater(route_factors(journal)["route-one"], 0.4)

    def test_duplicate_terminal_outcome_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.journal(Path(temporary))
            record_outcome(journal, self.receipt(), route_families={"route-one":"openai"})
            with self.assertRaises(OutcomeRefusal):
                record_outcome(
                    journal,
                    self.receipt(verdict="rejected", accepted_commit=None, semantic_increment=0, accepted_tokens=0),
                    route_families={"route-one":"openai"},
                )

    def test_unknown_attempt_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(Path(temporary) / "history.jsonl")
            with self.assertRaises(OutcomeRefusal):
                record_outcome(journal, self.receipt())

    def test_admitted_outcome_requires_matching_ready_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.journal(Path(temporary))
            with self.assertRaises(OutcomeRefusal):
                record_outcome(
                    journal,
                    self.receipt(accepted_commit="b" * 40),
                    route_families={"route-one":"openai"},
                )

    def test_outcome_requires_ready_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(Path(temporary) / "history.jsonl")
            journal.append(
                "attempt.started",
                {
                    "attempt_id":"attempt-one",
                    "order_id":"order-one",
                    "task_id":"task-one",
                    "route_id":"route-one",
                },
                at=1,
            )
            with self.assertRaises(OutcomeRefusal):
                record_outcome(journal, self.receipt())

    def test_reversion_penalizes_route(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            good = self.journal(Path(first))
            bad = self.journal(Path(second))
            record_outcome(good, self.receipt(), route_families={"route-one":"openai"})
            record_outcome(
                bad,
                self.receipt(
                    verdict="reverted",
                    accepted_commit=None,
                    reviewer_families=(),
                    semantic_increment=0,
                    accepted_tokens=0,
                    defects=2,
                ),
                route_families={"route-one":"openai"},
            )
            self.assertGreater(route_factors(good)["route-one"], route_factors(bad)["route-one"])


if __name__ == "__main__":
    unittest.main()
