from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from fleet_control.calibration import (
    CalibrationError,
    apply_calibration,
    calibrate,
    load_calibration,
)
from fleet_control.model import BillingClass, BillingProof, Route
from fleet_control.policy import route_verdict


class CalibrationTests(unittest.TestCase):
    def route(self, script: Path) -> Route:
        return Route(
            id="local-proof",
            provider="local",
            model="test-model",
            provider_family="local",
            runtime="plain",
            command=("python3", "unused.py"),
            parser="plain-json",
            billing=BillingClass.LOCAL,
            proof=BillingProof(
                kind="local-process",
                subject_hash="",
                observed_at=0,
                expires_at=0,
                evidence_hash="",
                trusted=False,
            ),
            roles=frozenset({"mechanic"}),
            enabled=True,
            proof_command=("python3", str(script)),
            proof_expect=r"LOCAL MODEL READY",
            proof_subject_files=(script,),
        )

    def test_calibration_binds_route_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            route = self.route(script)
            raw = {"mode": "observe-plan", "routes": [{"id": route.id}]}
            path = root / "calibration.json"
            record = calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            loaded = load_calibration(path)
            calibrated = apply_calibration(routes=(route,), record=loaded, raw_config=raw, now=200)
            self.assertEqual(record.config_hash, loaded.config_hash)
            self.assertTrue(route_verdict(calibrated[0], now=200).allowed)
            self.assertEqual(calibrated[0].proof.subject_hash, calibrated[0].subject_hash)

    def test_calibration_persists_subject_hash_metadata_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            secret = "private-auth-material"
            script.write_text(f"# {secret}\nprint('LOCAL MODEL READY')")
            route = self.route(script)
            raw = {"routes": [{"id": route.id, "proof_subject_files": [str(script)]}]}
            path = root / "calibration.json"
            record = calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            metadata = record.routes[route.id]["subject_files"]
            self.assertEqual(metadata[0]["path"], str(script.resolve()))
            self.assertEqual(metadata[0]["size"], len(script.read_bytes()))
            self.assertEqual(len(metadata[0]["sha256"]), 64)
            self.assertNotIn(secret, path.read_text())

    def test_changed_config_invalidates_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            route = self.route(script)
            raw = {"mode": "observe-plan", "routes": [{"id": route.id}]}
            path = root / "calibration.json"
            calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            record = load_calibration(path)
            with self.assertRaises(CalibrationError):
                apply_calibration(
                    routes=(route,),
                    record=record,
                    raw_config={**raw, "mode": "apply"},
                    now=200,
                )

    def test_changed_proof_subject_disables_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            route = self.route(script)
            raw = {"routes": [{"id": route.id, "proof_subject_files": [str(script)]}]}
            path = root / "calibration.json"
            calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            script.write_text("print('LOCAL MODEL READY')\n# changed")
            calibrated = apply_calibration(
                routes=(route,),
                record=load_calibration(path),
                raw_config=raw,
                now=200,
            )
            self.assertFalse(calibrated[0].enabled)

    def test_proof_subject_permission_drift_disables_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            script.chmod(0o600)
            route = self.route(script)
            raw = {"routes": [{"id": route.id, "proof_subject_files": [str(script)]}]}
            path = root / "calibration.json"
            calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            script.chmod(0o640)
            calibrated = apply_calibration(
                routes=(route,),
                record=load_calibration(path),
                raw_config=raw,
                now=200,
            )
            self.assertFalse(calibrated[0].enabled)

    def test_proof_subject_mutation_during_probe_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text(
                "from pathlib import Path\n"
                f"path = Path({str(script)!r})\n"
                "path.write_text(path.read_text() + '# mutated\\n')\n"
                "print('LOCAL MODEL READY')\n"
            )
            route = self.route(script)
            output = root / "calibration.json"
            with self.assertRaisesRegex(CalibrationError, "changed during calibration"):
                calibrate(
                    raw_config={"routes": [{"id": route.id}]},
                    routes=(route,),
                    output=output,
                    ttl_seconds=600,
                )
            self.assertFalse(output.exists())

    def test_missing_proof_subject_disables_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            route = self.route(script)
            raw = {"routes": [{"id": route.id, "proof_subject_files": [str(script)]}]}
            path = root / "calibration.json"
            calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            script.unlink()
            calibrated = apply_calibration(
                routes=(route,),
                record=load_calibration(path),
                raw_config=raw,
                now=200,
            )
            self.assertFalse(calibrated[0].enabled)

    def test_non_file_proof_subject_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            subject = root / "auth-store"
            subject.mkdir()
            route = replace(self.route(script), proof_subject_files=(subject,))
            with self.assertRaisesRegex(CalibrationError, "not a regular file"):
                calibrate(
                    raw_config={"routes": [{"id": route.id}]},
                    routes=(route,),
                    output=root / "calibration.json",
                    ttl_seconds=600,
                )

    def test_symlink_proof_subject_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            alias = root / "proof-alias.py"
            alias.symlink_to(script)
            route = replace(self.route(script), proof_subject_files=(alias,))
            with self.assertRaisesRegex(CalibrationError, "symlink"):
                calibrate(
                    raw_config={"routes": [{"id": route.id}]},
                    routes=(route,),
                    output=root / "calibration.json",
                    ttl_seconds=600,
                )

    def test_expired_calibration_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            route = self.route(script)
            raw = {"routes": [{"id": route.id}]}
            path = root / "calibration.json"
            calibrate(raw_config=raw, routes=(route,), output=path, ttl_seconds=600, now=100)
            with self.assertRaises(CalibrationError):
                apply_calibration(
                    routes=(route,),
                    record=load_calibration(path),
                    raw_config=raw,
                    now=700,
                )

    def test_failed_proof_command_never_creates_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("raise SystemExit(1)")
            route = self.route(script)
            path = root / "calibration.json"
            with self.assertRaises(CalibrationError):
                calibrate(raw_config={"routes": []}, routes=(route,), output=path, ttl_seconds=600)
            self.assertFalse(path.exists())

    def test_calibration_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "proof.py"
            script.write_text("print('LOCAL MODEL READY')")
            route = self.route(script)
            path = root / "calibration.json"
            calibrate(raw_config={"routes": []}, routes=(route,), output=path, ttl_seconds=600)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_malformed_calibration_fields_raise_calibration_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            path.write_text(
                '{"routes":{},"controls":{},"observed_at":"not-a-number"}'
            )
            with self.assertRaises(CalibrationError):
                load_calibration(path)

    def test_failed_route_does_not_block_a_healthy_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            healthy_script = root / "healthy.py"
            healthy_script.write_text("print('LOCAL MODEL READY')")
            failed_script = root / "failed.py"
            failed_script.write_text("raise SystemExit(1)")
            healthy = self.route(healthy_script)
            failed = replace(self.route(failed_script), id="failed-proof")
            raw = {"routes": [{"id": healthy.id}, {"id": failed.id}]}
            path = root / "calibration.json"
            record = calibrate(
                raw_config=raw,
                routes=(failed, healthy),
                output=path,
                ttl_seconds=600,
            )
            self.assertIn(healthy.id, record.routes)
            self.assertIn(failed.id, record.route_refusals)
            calibrated = apply_calibration(
                routes=(failed, healthy),
                record=record,
                raw_config=raw,
            )
            self.assertFalse(calibrated[0].enabled)
            self.assertTrue(calibrated[1].enabled)

    def test_malformed_route_proof_does_not_block_a_healthy_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            healthy_script = root / "healthy.py"
            healthy_script.write_text("print('LOCAL MODEL READY')")
            broken_script = root / "broken.py"
            broken_script.write_text("print('LOCAL MODEL READY')")
            healthy = self.route(healthy_script)
            broken = replace(self.route(broken_script), id="broken-proof")
            raw = {"routes": [{"id": healthy.id}, {"id": broken.id}]}
            path = root / "calibration.json"
            record = calibrate(
                raw_config=raw,
                routes=(healthy, broken),
                output=path,
                ttl_seconds=600,
                now=100,
            )
            malformed = dict(record.routes[broken.id])
            malformed["subject_files"] = [
                {"path": str(broken_script), "size": float("inf"), "sha256": "x"}
            ]
            record = replace(record, routes={healthy.id: record.routes[healthy.id], broken.id: malformed})
            calibrated = apply_calibration(
                routes=(healthy, broken),
                record=record,
                raw_config=raw,
                now=200,
            )
            self.assertTrue(calibrated[0].enabled)
            self.assertFalse(calibrated[1].enabled)


if __name__ == "__main__":
    unittest.main()
