from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
import fleet_health_probe as health_probe


def response(code=0, output=""):
    return subprocess.CompletedProcess([], code, output, "")


def endpoint_runner(statuses, timeout_url=None):
    def runner(command, **kwargs):
        if command[-1] == timeout_url:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return response(0, str(statuses.get(command[-1], 500)))
    return runner


def state(at=100):
    return {
        "schema": "idol.health-probe.v1", "observed_at": at,
        "services": {"tailscaled": {"healthy": True, "status": "ok"}},
        "endpoints": {"idol": {"healthy": True, "status": "ok"}},
        "tailnet_mac_mini": {"healthy": True, "status": "online"},
        "primary_heartbeat": {"healthy": True, "status": "ok", "role": "standby",
                              "heartbeat_mtime_age_seconds": 4, "reported_epoch_age_seconds": 4},
        "calibrations": {"idol": {"healthy": True, "status": "ok"}},
        "journals": {"idol": {"healthy": True, "status": "ok",
                                "last_event": {"kind": "fleet.cycle.completed", "at": at, "sequence": 10}}},
        "overall": {"healthy": True, "status": "healthy", "reasons": []},
    }


def controller_fixture(root, expires_at=200, mode="apply"):
    from fleet_control import __version__
    from fleet_control.calibration import config_hash
    from fleet_control.controller import load_config

    root.mkdir(parents=True, exist_ok=True)
    subject = root / "proof-subject"
    subject.write_text("bound-account\n")
    calibration_file = root / "calibration.json"
    config_file = root / "fleet.json"
    raw = {
        "mode": mode, "repository": str(root / "repo"), "state_dir": str(root / "state"),
        "work_orders_dir": str(root / "orders"), "calibration_file": str(calibration_file),
        "routes": [{"id": "local", "provider": "local", "model": "fixed", "runtime": "openclaw",
                    "command": ["true"], "parser": "plain-json", "billing": "local",
                    "proof": {"kind": "local-process"}, "roles": ["observer"],
                    "proof_command": ["true"], "proof_expect": "ok",
                    "proof_subject_files": [str(subject)]}],
    }
    config_file.write_text(json.dumps(raw))
    loaded_raw, config = load_config(config_file)
    route = config.routes[0]
    subjects = route.proof_subject_metadata()
    proof = {"kind": "local-process", "subject_hash": route.subject_hash_for(subjects),
             "observed_at": 50, "expires_at": expires_at, "evidence_hash": "evidence",
             "trusted": True, "subject_files": [asdict(item) for item in subjects]}
    calibration_file.write_text(json.dumps({"version": "idol.fleet.calibration.v1",
                                             "config_hash": config_hash(loaded_raw),
                                             "controller_version": __version__, "observed_at": 50,
                                             "expires_at": expires_at, "routes": {"local": proof},
                                             "route_refusals": {},
                                             "controls": {key: True for key in (
                                                 "no_paygo", "proof_subject_binding", "stale_sha_refusal",
                                                 "semantic_overlap", "path_overlap", "bounded_termination")}}))
    return config_file, subject


class EndpointTests(unittest.TestCase):
    def test_services_use_two_bounded_systemctl_show_calls(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            names = [item for item in command if item.endswith(".service")]
            blocks = []
            for name in names:
                obsolete = name == "cloudflared.service"
                blocks.append(f"Id={name}\nLoadState=loaded\nUnitFileState={'disabled' if obsolete else 'enabled'}\n"
                              f"ActiveState={'inactive' if obsolete else 'active'}")
            return response(0, "\n\n".join(blocks))
        result = health_probe.services(runner)
        self.assertTrue(all(item["healthy"] for item in result.values()))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("show" in command and timeout == 8 for command, timeout in calls))

    def test_expected_statuses_and_hermes_alternatives(self):
        statuses = {"https://idol.id/": 200, "https://live.idol.id/": 200,
                    "https://hermes.idol.id/health": 503, "https://hermes.idol.id/": 302,
                    "https://claw.idol.id/": 200, "https://hermes-mm.idol.id/health": 200,
                    "https://hermes-mm.idol.id/": 200, "https://claw-mm.idol.id/": 200}
        result = health_probe.endpoints(endpoint_runner(statuses))
        self.assertTrue(all(item["healthy"] for item in result.values()))
        self.assertEqual(result["hermes"]["accepted_variant"], "root")

    def test_private_live_timeout_is_distinct_and_bounded(self):
        captured = {}
        def runner(command, **kwargs):
            if command[-1] == "https://live.idol.id/":
                captured["command"], captured["timeout"] = command, kwargs["timeout"]
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return response(0, "302" if command[-1].endswith("idol.id/") and "hermes" in command[-1] else "200")
        result = health_probe.endpoints(runner)
        self.assertEqual(result["live"]["status"], "private_live_timeout")
        self.assertEqual(captured["command"][captured["command"].index("--max-time") + 1], "12")
        self.assertEqual(captured["timeout"], 12)
        self.assertNotIn("-k", captured["command"])

    def test_effective_mode_is_reported_without_serializing_execstart(self):
        output = ("Id=idol-fleet-idol.service\nLoadState=loaded\nUnitFileState=enabled\n"
                  "ActiveState=active\nExecStart={ path=/usr/bin/python3 ; "
                  "argv[]=/usr/bin/python3 -m fleet_control.cli --config /private/config "
                  "serve --mode observe-plan ; }\n")
        item = ("idol_fleet_idol", "idol-fleet-idol.service", True, "enabled")
        result = health_probe.service_batch([item], True, lambda *args, **kw: response(output=output))
        self.assertEqual(result["idol_fleet_idol"]["mode"], "observe-plan")
        self.assertNotIn("/private/config", json.dumps(result))

    def test_obsolete_service_starting_or_unavailable_is_not_healthy(self):
        item = ("obsolete_cloudflared", "cloudflared.service", False, "obsolete")
        for active in ("activating", "failed", "unknown"):
            output = f"Id=cloudflared.service\nLoadState=loaded\nUnitFileState=disabled\nActiveState={active}"
            result = health_probe.service_batch([item], False, lambda *args, **kw: response(output=output))
            self.assertFalse(result[item[0]]["healthy"])

    def test_empty_peer_names_never_match_and_empty_config_fails_closed(self):
        runner = lambda *args, **kw: response(output=json.dumps({"Peer": {"id": {"Online": True}}}))
        with mock.patch.object(health_probe, "MM_NAMES", {"mm"}):
            self.assertEqual(health_probe.tailnet(runner)["status"], "not_found")
        with mock.patch.object(health_probe, "MM_NAMES", set()):
            self.assertEqual(health_probe.tailnet(runner)["status"], "invalid_config")


class DeployedFormatTests(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 10), "fleet_control requires Python 3.10+")
    def test_native_controller_validation_rejects_stale_and_mutated_proofs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_config, subject = controller_fixture(root)
            valid = health_probe.calibration(str(valid_config), 100, "apply")
            self.assertTrue(valid["calibration_ready"])
            self.assertEqual(valid["bound_proof_file_count"], 1)
            subject.write_text("mutated-account\n")
            mutated = health_probe.calibration(str(valid_config), 100, "apply")
            self.assertFalse(mutated["calibration_ready"])
            self.assertEqual(mutated["status"], "route_proof_invalid")
            stale_config, _ = controller_fixture(root / "stale", expires_at=99)
            stale = health_probe.calibration(str(stale_config), 100, "apply")
            self.assertEqual(stale["status"], "invalid_or_stale")

    @unittest.skipIf(sys.version_info < (3, 10), "fleet_control requires Python 3.10+")
    def test_service_observe_override_wins_over_apply_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _ = controller_fixture(Path(directory), mode="apply")
            result = health_probe.calibration(str(config), 100, "observe-plan")
        self.assertEqual(result["mode"], "observe-plan")
        self.assertEqual(result["status"], "observation_only")
        self.assertTrue(result["calibration_ready"])
        self.assertFalse(result["healthy"])

    @unittest.skipIf(sys.version_info < (3, 10), "fleet_control requires Python 3.10+")
    def test_controller_journal_hash_and_bounded_tail(self):
        from fleet_control.journal import Journal
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet-history.jsonl"
            writer = Journal(path)
            writer.append("fleet.observed", {"padding": "x" * 70_000}, at=90)
            writer.append("fleet.cycle.completed", {"secret": "not returned"}, at=100)
            result = health_probe.journal(path)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["last_event"]["kind"], "fleet.cycle.completed")
        self.assertNotIn("secret", json.dumps(result))

    def test_plain_role_and_mtime_fencing_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            role, heartbeat = Path(directory) / "role", Path(directory) / "primary-heartbeat"
            role.write_text("standby\n"); heartbeat.write_text("1\n"); os.utime(heartbeat, (46, 46))
            with mock.patch.object(health_probe, "ROLE", role), mock.patch.object(health_probe, "HEARTBEAT", heartbeat):
                self.assertTrue(health_probe.heartbeat(100)["healthy"])
                os.utime(heartbeat, (44, 44))
                self.assertFalse(health_probe.heartbeat(100)["healthy"])
                role.write_text("active\n"); heartbeat.unlink()
                result = health_probe.heartbeat(100)
            self.assertTrue(result["healthy"])
            self.assertEqual(result["status"], "local_primary")

    def test_load_json_reads_limit_plus_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"
            path.write_bytes(b"{" + b" " * 64 + b"}")
            with self.assertRaisesRegex(ValueError, "too_large"):
                health_probe.load_json(path, 64)

    def test_unknown_role_content_is_not_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            role = Path(directory) / "role"
            role.write_text("private-unexpected-content")
            with mock.patch.object(health_probe, "ROLE", role):
                result = health_probe.heartbeat(100)
        self.assertFalse(result["healthy"])
        self.assertIsNone(result["role"])
        self.assertNotIn("private-unexpected-content", json.dumps(result))


class StateTests(unittest.TestCase):
    def test_no_change_suppresses_transition_and_state_is_private(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(health_probe, "STATE", Path(directory)):
            self.assertTrue(health_probe.persist(state(100)))
            log = Path(directory) / "transitions.jsonl"; before = log.read_text()
            second = state(400)
            second["primary_heartbeat"].update({"heartbeat_mtime_age_seconds": 9,
                                                 "reported_epoch_age_seconds": 999})
            second["journals"]["idol"]["last_event"].update({"at": 400, "sequence": 11})
            self.assertFalse(health_probe.persist(second)); self.assertEqual(log.read_text(), before)
            for name in ("current.json", "transitions.jsonl", "health-probe.lock"):
                self.assertEqual(stat.S_IMODE((Path(directory) / name).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            self.assertEqual(list(Path(directory).glob(".current.*")), [])


if __name__ == "__main__":
    unittest.main()
