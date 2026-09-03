from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fleet_control.journal import Journal, JournalError


class JournalTests(unittest.TestCase):
    def test_append_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(Path(temporary) / "history.jsonl")
            first = journal.append("one", {"value": 1}, at=1)
            second = journal.append("two", {"value": 2}, at=2)
            rows = journal.verify()
            self.assertEqual(len(rows), 2)
            self.assertEqual(second["previous"], first["hash"])

    def test_tampered_fact_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.jsonl"
            journal = Journal(path)
            journal.append("one", {"value": 1}, at=1)
            row = json.loads(path.read_text())
            row["fact"]["value"] = 9
            path.write_text(json.dumps(row) + "\n")
            with self.assertRaises(JournalError):
                journal.verify()

    def test_broken_sequence_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.jsonl"
            journal = Journal(path)
            row = journal.append("one", {"value": 1}, at=1)
            row["sequence"] = 2
            # Rehashing cannot make a broken sequence lawful.
            row["hash"] = Journal._digest({key: value for key, value in row.items() if key != "hash"})
            path.write_text(json.dumps(row) + "\n")
            with self.assertRaises(JournalError):
                journal.verify()

    def test_empty_line_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.jsonl"
            journal = Journal(path)
            journal.append("one", {"value": 1}, at=1)
            with path.open("a") as handle:
                handle.write("\n")
            with self.assertRaises(JournalError):
                journal.verify()


if __name__ == "__main__":
    unittest.main()
