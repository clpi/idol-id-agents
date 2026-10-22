"""Physical calibration receipts with real files/processes, not Idol qualification."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from fleet_control.coordination import canonical
from fleet_control.journal import Journal
from fleet_control.model import Route, stable_hash
from fleet_control.native_dispatch import (
    CONTROLS, LAW, NativeDispatcher, NativeDispatchRefusal, calibrate_native,
)


ROOT = Path(__file__).resolve().parents[1]


class NativeCalibrationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="calibration-receipt-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.live = self.root / "live"
        self.live.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        for name in (LAW, CONTROLS):
            (self.live / name).parent.mkdir(parents=True, exist_ok=True)
        (self.live / LAW).write_text("physical receipt fixture; no native semantic claim\n")
        self.compiler = self.root / "compiler-fixture"
        executable = f"#!{sys.executable}\nraise SystemExit(0)\n".encode()
        self.compiler.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
            "assert sys.argv[1:4] == ['compile', '--backend=direct', '--no-cache']\n"
            f"Path(sys.argv[-1]).write_bytes({executable!r})\n"
            "Path(sys.argv[-1]).chmod(0o500)\n"
        )
        self.compiler.chmod(0o700)
        (self.live / CONTROLS).write_text(
            "from pathlib import Path\nimport hashlib,json,subprocess,sys\n"
            "root = Path(__file__).resolve().parents[1]\n"
            "binary = Path(sys.argv[sys.argv.index('--binary') + 1])\n"
            "result = subprocess.run([str(binary)], capture_output=True, timeout=5)\n"
            "sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()\n"
            "print(json.dumps({'version': 1, 'backend': 'direct', 'cases': 1,\n"
            " 'passed': int(result.returncode == 0), 'failures': [] if result.returncode == 0 else ['fixture failed'],\n"
            " 'source_sha256': sha(root / 'coordination/dispatch.id'),\n"
            " 'controls_sha256': sha(Path(__file__)), 'binary_sha256': sha(binary)}))\n"
        )
        self.git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.git_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.git("init", "-q")
        self.git("config", "user.name", "Calibration fixture")
        self.git("config", "user.email", "calibration@live.invalid")
        self.pin_sources()

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
             "-c", "init.templateDir=", *arguments],
            cwd=self.live, env=self.git_env, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        ).stdout.strip()

    def pin_sources(self):
        self.git("add", LAW, CONTROLS)
        self.git("commit", "-qm", "pin physical calibration fixture")
        self.raw = {"native_dispatch": {
            "live_repository": str(self.live), "live_revision": self.git("rev-parse", "HEAD"),
            "source_sha256": hashlib.sha256((self.live / LAW).read_bytes()).hexdigest(),
            "controls_sha256": hashlib.sha256((self.live / CONTROLS).read_bytes()).hexdigest(),
            "compiler": str(self.compiler), "compiler_sha256": hashlib.sha256(self.compiler.read_bytes()).hexdigest(),
        }}

    def calibrate(self):
        now = time.time()
        return calibrate_native(raw_config=self.raw, state_dir=self.state,
                                observed_at=now, expires_at=now + 300)

    def consumer(self, proof):
        path = self.state / "calibration.json"
        path.write_bytes(canonical({"config_hash": stable_hash(self.raw), "native_dispatch": proof}) + b"\n")
        return NativeDispatcher(raw_config=self.raw, calibration_file=path,
                                journal=Journal(self.state / "journal.jsonl"))

    def test_returned_proof_survives_actual_link_cleanup_without_rewriting(self):
        linked = {}
        original_link = os.link

        def observe_link(source, target):
            original_link(source, target)
            linked.update(source=Path(source), target=Path(target), stat=Path(target).stat())

        with mock.patch("fleet_control.native_dispatch.os.link", side_effect=observe_link):
            proof = self.calibrate()
        self.assertEqual(canonical(self.consumer(proof).proof()), canonical(proof))
        artifact = Path(proof["artifact"]["path"])
        self.assertEqual(linked["stat"].st_nlink, 2)
        self.assertEqual(artifact.stat().st_nlink, 1)
        self.assertEqual(linked["stat"].st_ino, artifact.stat().st_ino)
        self.assertFalse(linked["source"].exists())
        self.assertEqual(list(artifact.parent.glob("compile-*")), [])
        self.assertEqual(proof["artifact"], asdict(Route._capture_proof_subject(artifact)))

    def test_existing_artifact_is_reused_with_unchanged_consumable_proof(self):
        first = self.calibrate()
        second = self.calibrate()
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertEqual(canonical(self.consumer(first).proof()), canonical(first))
        self.assertEqual(canonical(self.consumer(second).proof()), canonical(second))

    def test_later_link_metadata_drift_remains_a_refusal(self):
        proof = self.calibrate()
        consumer = self.consumer(proof)
        self.assertEqual(canonical(consumer.proof()), canonical(proof))
        artifact = Path(proof["artifact"]["path"])
        alias = self.state / "later-link"
        os.link(artifact, alias)
        alias.unlink()
        current = asdict(Route._capture_proof_subject(artifact))
        self.assertNotEqual(current["ctime_ns"], proof["artifact"]["ctime_ns"])
        self.assertEqual({key: value for key, value in current.items() if key != "ctime_ns"},
                         {key: value for key, value in proof["artifact"].items() if key != "ctime_ns"})
        with self.assertRaisesRegex(NativeDispatchRefusal, "artifact changed after calibration"):
            consumer.proof()

    def test_later_artifact_byte_drift_remains_a_refusal(self):
        proof = self.calibrate()
        consumer = self.consumer(proof)
        self.assertEqual(canonical(consumer.proof()), canonical(proof))
        artifact = Path(proof["artifact"]["path"])
        artifact.chmod(0o700)
        artifact.write_bytes(b"changed after calibration")
        with self.assertRaisesRegex(NativeDispatchRefusal, "artifact changed after calibration"):
            consumer.proof()

    def test_source_drift_during_controls_still_prevents_publication(self):
        controls = self.live / CONTROLS
        controls.write_text(controls.read_text() + "(root / 'coordination/dispatch.id').write_text('changed during controls')\n")
        self.pin_sources()
        with self.assertRaisesRegex(NativeDispatchRefusal, "source/compiler digest differs from its explicit pin"):
            self.calibrate()
        self.assertEqual(list((self.state / "native-dispatch").glob("*.bin")), [])


if __name__ == "__main__":
    unittest.main()
