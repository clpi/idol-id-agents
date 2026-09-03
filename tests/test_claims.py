from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fleet_control.claims import ClaimConflict, ControllerLease, SemanticClaimStore


class ClaimTests(unittest.TestCase):
    def test_parent_and_child_semantic_claims_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(
                owner="one",
                task_id="task-one",
                targets=("world/process",),
                ttl_seconds=60,
                now=10,
            )
            with self.assertRaises(ClaimConflict):
                store.acquire(
                    owner="two",
                    task_id="task-two",
                    targets=("world/process/run",),
                    ttl_seconds=60,
                    now=11,
                )

    def test_disjoint_semantic_claims_can_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(
                owner="one",
                task_id="task-one",
                targets=("world/process",),
                ttl_seconds=60,
                now=10,
            )
            store.acquire(
                owner="two",
                task_id="task-two",
                targets=("graph/application",),
                ttl_seconds=60,
                now=11,
            )
            self.assertEqual(len(store.list(now=12)), 2)

    def test_expired_claim_is_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(
                owner="one",
                task_id="task-one",
                targets=("world/process",),
                ttl_seconds=60,
                now=10,
            )
            self.assertFalse(store.list(now=70))

    def test_release_removes_only_matching_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticClaimStore(Path(temporary))
            store.acquire(owner="one", task_id="one", targets=("a",), ttl_seconds=60, now=1)
            store.acquire(owner="two", task_id="two", targets=("b",), ttl_seconds=60, now=1)
            store.release(owner="one", task_id="one")
            rows = store.list(now=2)
            self.assertEqual(tuple(row.target for row in rows), ("b",))

    def test_controller_lease_is_singleton(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.lock"
            with ControllerLease(path):
                with self.assertRaises(ClaimConflict):
                    with ControllerLease(path):
                        self.fail("second lease should not be acquired")


if __name__ == "__main__":
    unittest.main()
