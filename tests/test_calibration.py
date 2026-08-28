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


if __name__ == "__main__":
    unittest.main()
