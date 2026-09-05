from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from fleet_control.evidence import (
    EvidenceArtifactError,
    retain_candidate_evidence,
    validate_candidate_evidence,
)


class EvidenceArtifactTests(unittest.TestCase):
    def test_exclusive_retention_preserves_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-exclusive",
                content=b"first",
            )
            with self.assertRaises(EvidenceArtifactError):
                retain_candidate_evidence(
                    state_dir=state,
                    attempt_id="attempt-exclusive",
                    content=b"second",
                )
            self.assertEqual(Path(str(descriptor["path"])).read_bytes(), b"first")

    def test_refuses_empty_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(EvidenceArtifactError):
            retain_candidate_evidence(
                state_dir=Path(temporary) / "state",
                attempt_id="attempt-empty",
                content=b"",
            )

    def test_retains_and_validates_exact_private_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            content = b"bounded evidence\n"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-1",
                content=content,
            )
            self.assertEqual(set(descriptor), {"path", "sha256", "size_bytes"})
            self.assertEqual(descriptor["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(descriptor["size_bytes"], len(content))
            path = Path(str(descriptor["path"]))
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                validate_candidate_evidence(
                    descriptor,
                    state_dir=state,
                    attempt_id="attempt-1",
                ),
                descriptor,
            )

    def test_refuses_tampering_and_noncanonical_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-2",
                content=b"original\n",
            )
            path = Path(str(descriptor["path"]))
            path.write_bytes(b"changed\n")
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    descriptor,
                    state_dir=state,
                    attempt_id="attempt-2",
                )
            path.chmod(0o600)
            path.write_bytes(b"original\n")
            extra = dict(descriptor, secret="must-refuse")
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    extra,
                    state_dir=state,
                    attempt_id="attempt-2",
                )

    def test_refuses_symlink_and_fifo_without_following_or_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            directory = state / "candidate-evidence"
            directory.mkdir(parents=True, mode=0o700)
            target = state / "target"
            target.write_bytes(b"evidence")
            link = directory / "link.stdout"
            link.symlink_to(target)
            descriptor = {
                "path": str(link),
                "sha256": hashlib.sha256(b"evidence").hexdigest(),
                "size_bytes": 8,
            }
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    descriptor,
                    state_dir=state,
                    attempt_id="link",
                )

            fifo = directory / "fifo.stdout"
            os.mkfifo(fifo, 0o600)
            fifo_descriptor = {
                "path": str(fifo),
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "size_bytes": 1,
            }
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    fifo_descriptor,
                    state_dir=state,
                    attempt_id="fifo",
                )

    def test_refuses_artifacts_outside_the_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            outside = Path(temporary) / "outside"
            outside.write_bytes(b"evidence")
            outside.chmod(0o600)
            descriptor = {
                "path": str(outside),
                "sha256": hashlib.sha256(b"evidence").hexdigest(),
                "size_bytes": 8,
            }
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    descriptor,
                    state_dir=state,
                    attempt_id="outside",
                )

    def test_descriptor_is_bound_to_attempt_and_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-a",
                content=b"evidence",
            )
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    descriptor,
                    state_dir=state,
                    attempt_id="attempt-b",
                )
            oversized = dict(descriptor, size_bytes=8_000_001)
            with self.assertRaises(EvidenceArtifactError):
                validate_candidate_evidence(
                    oversized,
                    state_dir=state,
                    attempt_id="attempt-a",
                )
            with self.assertRaises(EvidenceArtifactError):
                retain_candidate_evidence(
                    state_dir=state,
                    attempt_id="attempt-oversized",
                    content=b"x" * 8_000_001,
                )

    def test_refuses_growth_after_the_initial_size_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-growing",
                content=b"evidence",
            )
            path = Path(str(descriptor["path"]))
            original_read = os.read
            grew = False

            def read_then_grow(file_descriptor: int, size: int) -> bytes:
                nonlocal grew
                block = original_read(file_descriptor, size)
                if not grew:
                    with path.open("ab") as handle:
                        handle.write(b"x")
                        handle.flush()
                        os.fsync(handle.fileno())
                    grew = True
                return block

            with mock.patch("fleet_control.evidence.os.read", side_effect=read_then_grow):
                with self.assertRaises(EvidenceArtifactError):
                    validate_candidate_evidence(
                        descriptor,
                        state_dir=state,
                        attempt_id="attempt-growing",
                    )

    def test_allows_concurrent_disjoint_artifact_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-read",
                content=b"evidence",
            )
            original_read = os.read
            created = False

            def read_then_create_sibling(file_descriptor: int, size: int) -> bytes:
                nonlocal created
                block = original_read(file_descriptor, size)
                if not created:
                    created = True
                    retain_candidate_evidence(
                        state_dir=state,
                        attempt_id="attempt-sibling",
                        content=b"other evidence",
                    )
                return block

            with mock.patch(
                "fleet_control.evidence.os.read",
                side_effect=read_then_create_sibling,
            ):
                self.assertEqual(
                    validate_candidate_evidence(
                        descriptor,
                        state_dir=state,
                        attempt_id="attempt-read",
                    ),
                    descriptor,
                )

    def test_refuses_directory_mode_change_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            descriptor = retain_candidate_evidence(
                state_dir=state,
                attempt_id="attempt-directory-mode",
                content=b"evidence",
            )
            directory = Path(str(descriptor["path"])).parent
            original_read = os.read
            changed = False

            def read_then_change_mode(file_descriptor: int, size: int) -> bytes:
                nonlocal changed
                block = original_read(file_descriptor, size)
                if not changed:
                    directory.chmod(0o755)
                    changed = True
                return block

            with mock.patch(
                "fleet_control.evidence.os.read",
                side_effect=read_then_change_mode,
            ):
                with self.assertRaises(EvidenceArtifactError):
                    validate_candidate_evidence(
                        descriptor,
                        state_dir=state,
                        attempt_id="attempt-directory-mode",
                    )


if __name__ == "__main__":
    unittest.main()
