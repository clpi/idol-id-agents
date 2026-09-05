from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from fleet_control.evidence import retain_candidate_evidence
from fleet_control.journal import Journal
from fleet_control.performance import (
    OutcomeReceipt,
    OutcomeRefusal,
    load_receipt,
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

    def no_change_journal(
        self,
        root: Path,
        *,
        ready_overrides: dict[str, object] | None = None,
        include_executed: bool = True,
        executed_overrides: dict[str, object] | None = None,
        candidate_attempt_id: str = "attempt-one",
    ) -> tuple[Journal, dict[str, object]]:
        journal = Journal(root / "history.jsonl")
        identity = {
            "attempt_id":"attempt-one",
            "order_id":"order-one",
            "task_id":"task-one",
            "route_id":"route-one",
            "base_sha":"a" * 40,
        }
        journal.append("attempt.started", identity, at=1)
        descriptor = retain_candidate_evidence(
            state_dir=root,
            attempt_id=candidate_attempt_id,
            content=b'{"review":"no source change required"}\n',
        )
        if include_executed:
            executed = {
                **identity,
                "provider_family":"openai",
                "stdout_hash":descriptor["sha256"],
            }
            executed.update(executed_overrides or {})
            journal.append("attempt.executed", executed, at=2)
        ready = {
            **identity,
            "commit":"a" * 40,
            "no_change":True,
            "paths":(),
            "candidate_evidence":descriptor,
        }
        ready.update(ready_overrides or {})
        journal.append("attempt.ready", ready, at=3)
        return journal, descriptor

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

    def test_mapping_preserves_candidate_evidence_hash(self) -> None:
        raw = asdict(self.receipt())
        raw["candidate_evidence_sha256"] = "b" * 64
        receipt = OutcomeReceipt.from_mapping(raw)
        self.assertEqual(receipt.candidate_evidence_sha256, "b" * 64)

    def test_mapping_refuses_invalid_candidate_evidence_hash(self) -> None:
        for value in ("", "B" * 64, "g" * 64, "b" * 63, 123):
            with self.subTest(value=value):
                raw = asdict(self.receipt())
                raw["candidate_evidence_sha256"] = value
                with self.assertRaises(OutcomeRefusal):
                    OutcomeReceipt.from_mapping(raw)

    def test_mapping_refuses_non_string_accepted_commit(self) -> None:
        for value in (123, [], {}):
            with self.subTest(value=value):
                raw = asdict(self.receipt(verdict="rejected"))
                raw["accepted_commit"] = value
                with self.assertRaises(OutcomeRefusal):
                    OutcomeReceipt.from_mapping(raw)

    def test_no_change_outcome_binds_retained_candidate_evidence(self) -> None:
        for verdict in ("admitted", "rejected", "reverted"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as temporary:
                    journal, descriptor = self.no_change_journal(Path(temporary))
                    journal = Journal(journal.path)
                    row = record_outcome(
                        journal,
                        self.receipt(
                            verdict=verdict,
                            accepted_commit="a" * 40 if verdict == "admitted" else None,
                            candidate_evidence_sha256=descriptor["sha256"],
                        ),
                        route_families={"route-one":"openai"},
                    )
                    self.assertIs(row["fact"]["no_change"], True)
                    self.assertEqual(
                        row["fact"]["candidate_evidence_sha256"],
                        descriptor["sha256"],
                    )

    def test_no_change_outcome_requires_matching_candidate_evidence_hash(self) -> None:
        cases = (
            (verdict, candidate_hash)
            for verdict in ("admitted", "rejected", "reverted")
            for candidate_hash in (None, "b" * 64)
        )
        for verdict, candidate_hash in cases:
            with self.subTest(verdict=verdict, candidate_hash=candidate_hash):
                with tempfile.TemporaryDirectory() as temporary:
                    journal, descriptor = self.no_change_journal(Path(temporary))
                    if candidate_hash == descriptor["sha256"]:
                        candidate_hash = "c" * 64
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(
                                verdict=verdict,
                                accepted_commit="a" * 40 if verdict == "admitted" else None,
                                candidate_evidence_sha256=candidate_hash,
                            ),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_no_change_rejection_does_not_claim_an_accepted_commit(self) -> None:
        for verdict in ("rejected", "reverted"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as temporary:
                    journal, descriptor = self.no_change_journal(Path(temporary))
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(
                                verdict=verdict,
                                accepted_commit="a" * 40,
                                candidate_evidence_sha256=descriptor["sha256"],
                            ),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_no_change_rejection_loads_null_accepted_commit(self) -> None:
        for verdict in ("rejected", "reverted"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    journal, descriptor = self.no_change_journal(root)
                    raw = asdict(
                        self.receipt(
                            verdict=verdict,
                            accepted_commit=None,
                            candidate_evidence_sha256=descriptor["sha256"],
                        )
                    )
                    receipt_path = root / "outcome.json"
                    receipt_path.write_text(json.dumps(raw), encoding="utf-8")
                    receipt = load_receipt(receipt_path)
                    self.assertIsNone(receipt.accepted_commit)
                    row = record_outcome(
                        journal,
                        receipt,
                        route_families={"route-one":"openai"},
                    )
                    self.assertEqual(row["kind"], f"attempt.{verdict}")

    def test_no_change_outcome_refuses_another_attempt_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal, descriptor = self.no_change_journal(
                Path(temporary),
                candidate_attempt_id="attempt-other",
            )
            with self.assertRaises(OutcomeRefusal):
                record_outcome(
                    journal,
                    self.receipt(
                        accepted_commit="a" * 40,
                        candidate_evidence_sha256=descriptor["sha256"],
                    ),
                    route_families={"route-one":"openai"},
                )
            self.assertEqual(route_factors(journal), {})

    def test_no_change_outcome_requires_matching_executed_output(self) -> None:
        cases = (
            {"include_executed":False},
            {"executed_overrides":{"stdout_hash":"b" * 64}},
        )
        for journal_options in cases:
            with self.subTest(journal_options=journal_options):
                with tempfile.TemporaryDirectory() as temporary:
                    journal, descriptor = self.no_change_journal(
                        Path(temporary),
                        **journal_options,
                    )
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(
                                accepted_commit="a" * 40,
                                candidate_evidence_sha256=descriptor["sha256"],
                            ),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_no_change_outcome_refuses_missing_or_modified_artifact(self) -> None:
        for mutation in ("missing", "tampered", "truncated"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    journal, descriptor = self.no_change_journal(Path(temporary))
                    artifact = Path(str(descriptor["path"]))
                    if mutation == "missing":
                        artifact.unlink()
                    elif mutation == "tampered":
                        content = artifact.read_bytes()
                        artifact.write_bytes(bytes([content[0] ^ 1]) + content[1:])
                    else:
                        artifact.write_bytes(b"x")
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(
                                accepted_commit="a" * 40,
                                candidate_evidence_sha256=descriptor["sha256"],
                            ),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_no_change_outcome_requires_exact_ready_subject(self) -> None:
        cases = (
            {"no_change":False},
            {"no_change":1},
            {"commit":"b" * 40},
            {"base_sha":"b" * 40},
            {"paths":("src/changed.duo",)},
        )
        for ready_overrides in cases:
            with self.subTest(ready_overrides=ready_overrides):
                with tempfile.TemporaryDirectory() as temporary:
                    journal, descriptor = self.no_change_journal(
                        Path(temporary),
                        ready_overrides=ready_overrides,
                    )
                    with self.assertRaises(OutcomeRefusal):
                        record_outcome(
                            journal,
                            self.receipt(
                                accepted_commit="a" * 40,
                                candidate_evidence_sha256=descriptor["sha256"],
                            ),
                            route_families={"route-one":"openai"},
                        )
                    self.assertEqual(route_factors(journal), {})

    def test_source_change_outcome_refuses_unexplained_candidate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.journal(Path(temporary))
            with self.assertRaises(OutcomeRefusal):
                record_outcome(
                    journal,
                    self.receipt(candidate_evidence_sha256="b" * 64),
                    route_families={"route-one":"openai"},
                )
            self.assertEqual(route_factors(journal), {})

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
            row = record_outcome(
                journal,
                self.receipt(),
                route_families={"route-one":"openai"},
            )
            self.assertNotIn("no_change", row["fact"])
            self.assertNotIn("candidate_evidence_sha256", row["fact"])
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
