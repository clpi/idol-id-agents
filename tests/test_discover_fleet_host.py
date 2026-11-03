from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("discover_fleet_host", ROOT / "scripts/discover-fleet-host.py")
assert SPEC and SPEC.loader
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


def observed(output: str, ok: bool = True) -> dict:
    return {"ok": ok, "returncode": 0 if ok else 1, "output": output,
            "output_hash": discovery.digest(output), "summary": discovery.clean(output)}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.home_patch = mock.patch.object(discovery.Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_canonical_checkouts_and_explicit_checkout_are_observed_once(self):
        canonical = self.home / "work/idol-main"
        live = self.home / "hermes-jobs/idol-live"
        extra = self.home / "separate/worktree"
        for path in (canonical, live, extra):
            path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(path)], check=True)
        alias = self.home / "idol"
        alias.symlink_to(canonical, target_is_directory=True)
        facts = discovery.find_repositories([extra, canonical])
        self.assertEqual({row["path"] for row in facts}, {str(path) for path in (canonical, live, extra)})
        self.assertEqual(len(facts), 3)
        self.assertTrue(all(row["head"] is None for row in facts))
        self.assertTrue(all(row["origin_hash"] is None for row in facts))

    def test_repository_identity_comes_from_observed_remote_not_directory_name(self):
        path = self.home / "work/idol-main"
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "git@github.com:clpi/idol.git"], check=True)
        self.assertEqual(discovery.repo_fact(path)["origin_repository"], "clpi/idol")
        subprocess.run(["git", "-C", str(path), "remote", "set-url", "origin", "https://github.com/elsewhere/example.git"], check=True)
        self.assertEqual(discovery.repo_fact(path)["origin_repository"], "elsewhere/example")

    def test_invalid_repository_candidate_does_not_abort_remaining_checkouts(self):
        loop = self.home / "loop"
        loop.symlink_to(loop)
        valid = self.home / "valid"
        valid.mkdir()
        subprocess.run(["git", "init", "-q", str(valid)], check=True)
        facts = discovery.find_repositories([loop, valid])
        self.assertEqual([row["path"] for row in facts], [str(valid)])

    def test_inaccessible_repository_candidate_does_not_abort_remaining_checkouts(self):
        blocked, valid = self.home / "blocked", self.home / "valid"
        def inspect(path):
            if path == blocked:
                raise PermissionError("unavailable")
            return {"path": str(path)} if path == valid else None
        with mock.patch.object(discovery, "repo_fact", side_effect=inspect):
            facts = discovery.find_repositories([blocked, valid])
        self.assertEqual(facts, [{"path": str(valid)}])

    def test_claim_discovery_hashes_unrelated_helper_without_executing_it(self):
        command = self.home / "unrelated/tools/node/dev/claim"
        command.parent.mkdir(parents=True)
        body = b'#!/bin/sh\nprintf "token=fixture-secret\\n"\nexit 0\n'
        command.write_bytes(body)
        command.chmod(0o755)
        repository = {"path": str(self.home / "unrelated"), "origin_repository": "unrelated/example"}
        with mock.patch.object(discovery, "run", side_effect=AssertionError("must not execute claim")) as run:
            fact = discovery.claim_fact([repository])
        run.assert_not_called()
        self.assertTrue(fact["found"])
        self.assertEqual(fact["sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(fact["inspection_only"])
        self.assertFalse(fact["compatibility_verified"])
        self.assertEqual(fact["verbs"], [])
        self.assertIsNone(fact["help_returncode"])
        self.assertIsNone(fact["help_hash"])
        self.assertIsNone(fact["help_summary"])
        self.assertNotIn("fixture-secret", json.dumps(fact))

    def test_unreadable_claim_identity_stays_unverified_without_source_error(self):
        command = self.home / "tools/node/dev/claim"
        command.parent.mkdir(parents=True)
        command.write_text("private fixture")
        with mock.patch.object(discovery, "file_sha", side_effect=PermissionError("private fixture")), \
             mock.patch.object(discovery, "run") as run:
            fact = discovery.claim_fact([{"path": str(self.home)}])
        run.assert_not_called()
        self.assertTrue(fact["found"])
        self.assertFalse(fact["compatibility_verified"])
        self.assertIsNone(fact["sha256"])
        self.assertEqual(fact["reason"], "file-identity-unavailable")
        self.assertNotIn("private fixture", json.dumps(fact))

    def test_missing_commands_do_not_attempt_execution(self):
        with mock.patch.object(discovery.shutil, "which", return_value=None), \
             mock.patch.object(discovery.Path, "is_file", return_value=False), \
             mock.patch.object(discovery, "run") as run:
            self.assertEqual(discovery.command_fact("hermes"), {"found": False})
            self.assertEqual(discovery.tailscale_fact(), {"found": False})
        run.assert_not_called()

    def test_tailnet_fields_are_allowlisted_and_online_is_not_reachability(self):
        raw = {"BackendState": "Running", "AuthURL": "https://secret.example/?token=do-not-copy",
               "User": {"1": {"LoginName": "private@example.com"}},
               "Self": {"ID": "nself", "HostName": "local", "OS": "darwin", "TailscaleIPs": ["100.64.0.1"], "Online": True},
               "Peer": {"nodekey:secret": {"ID": "npeer", "HostName": "r16", "OS": "linux",
                                            "TailscaleIPs": ["100.64.0.2"], "Online": False,
                                            "PublicKey": "privatekey", "SSH_HostKeys": ["secret-host-key"]}}}
        with mock.patch.object(discovery.shutil, "which", return_value="/usr/bin/tailscale"), \
             mock.patch.object(discovery, "run", side_effect=[observed("ping failed", False), observed(json.dumps(raw))]) as run:
            fact = discovery.tailscale_fact()
        self.assertTrue(fact["status_ok"])
        self.assertFalse(fact["r16_reachable"])
        self.assertIs(fact["peers"][0]["online_reported"], False)
        self.assertEqual(fact["compute_authorization"], "unverified")
        self.assertNotIn("reachable", fact["peers"][0])
        serialized = json.dumps(fact)
        for value in ("do-not-copy", "private@example.com", "privatekey", "secret-host-key", "nodekey:secret"):
            self.assertNotIn(value, serialized)
        self.assertEqual(run.call_args_list[1].args[0], ("/usr/bin/tailscale", "status", "--json"))

    def test_malformed_or_missing_tailnet_data_does_not_become_empty_inventory(self):
        malformed = ["not json", "{}", "[]", '{"BackendState":"Running"}',
                     json.dumps({"BackendState": "Running", "Self": None, "Peer": []}),
                     json.dumps({"BackendState": "Running", "Self": None, "Peer": {"x": {"TailscaleIPs": [False]}}})]
        for body in malformed:
            with self.subTest(body=body), mock.patch.object(discovery.shutil, "which", return_value="tailscale"), \
                 mock.patch.object(discovery, "run", side_effect=[observed("failed", False), observed(body)]):
                fact = discovery.tailscale_fact()
            self.assertFalse(fact["status_ok"])
            self.assertNotIn("peers", fact)

    def ollama(self, body, status=200):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = status
        response.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
        with mock.patch.object(discovery.http.client, "HTTPConnection", return_value=connection) as http:
            fact = discovery.ollama_fact()
        http.assert_called_once_with("127.0.0.1", 11434, timeout=3)
        connection.request.assert_called_once_with("GET", "/api/tags", headers={"Accept": "application/json"})
        connection.close.assert_called_once()
        return fact

    def test_model_metadata_preserves_identity_without_loading_or_auth_claims(self):
        raw = {"models": [{"name": "example:4b", "digest": "a" * 64, "size": 12345,
                           "api_key": "never-copy", "remote_model": "hosted-example",
                           "details": {"quantization_level": "Q4_K_M", "parameter_size": "4B", "format": "gguf",
                                       "secret": "also-never-copy"}}]}
        fact = self.ollama(raw)
        self.assertTrue(fact["ok"])
        self.assertEqual(fact["models"], ["example:4b"])
        self.assertEqual(fact["inventory"][0]["digest"], "a" * 64)
        self.assertEqual(fact["inventory"][0]["size_bytes"], 12345)
        self.assertFalse(fact["inference_verified"])
        self.assertNotIn("never-copy", json.dumps(fact))
        self.assertNotIn("billing", fact)

    def test_malformed_large_and_redirected_models_are_explicit_failures(self):
        for body in (b"not json", [], {}, {"models": {}}, {"models": [False]},
                     {"models": [{"name": "x", "size": True, "digest": "a" * 64}]},
                     {"models": [{"name": "x", "size": 1, "digest": "invalid"}]},
                     b" " * (discovery.MAX_METADATA + 1)):
            with self.subTest(body_type=type(body).__name__):
                self.assertFalse(self.ollama(body)["ok"])
        fact = self.ollama({"Location": "https://paid-provider.example"}, status=302)
        self.assertFalse(fact["ok"])
        self.assertNotIn("paid-provider", json.dumps(fact))

    def test_missing_model_server_is_unknown_not_zero_models(self):
        with mock.patch.object(discovery.http.client.HTTPConnection, "request", side_effect=ConnectionRefusedError):
            fact = discovery.ollama_fact()
        self.assertFalse(fact["ok"])
        self.assertNotIn("models", fact)

    def test_absent_external_drive_does_not_report_parent_capacity_or_create_path(self):
        missing = self.home / "unmounted/models"
        with mock.patch.dict(os.environ, {}, clear=True):
            facts = discovery.storage_fact([missing, missing])
        selected = [row for row in facts if row["path"] == "~/unmounted/models"]
        self.assertEqual(len(selected), 1)
        self.assertFalse(selected[0]["exists"])
        self.assertNotIn("capacity_bytes", selected[0])
        self.assertFalse(missing.exists())

    def test_existing_model_path_reports_actual_filesystem_capacity(self):
        path = self.home / "models"
        path.mkdir()
        facts = discovery.storage_fact([path])
        selected = next(row for row in facts if row["path"] == "~/models")
        self.assertTrue(selected["exists"])
        self.assertGreater(selected["capacity_bytes"], 0)
        self.assertFalse(selected["is_mount"])

    def test_hermes_configuration_reports_routes_without_secret_values(self):
        config = self.home / ".hermes/config.yaml"
        config.parent.mkdir()
        config.write_text('''model:
  provider: nous
  default: Hermes-example
  base_url: https://user:private@provider.example?token=secret
providers:
  local:
    api_key: do-not-copy
    key_cmd: echo private-command
fallback_providers:
  - provider: kilo
    model: kilo-auto/free
    api_key: fallback-secret
auxiliary:
  vision:
    provider: nous
    model: image-example
    api_key: image-secret
mcp_servers:
  n8n:
    command: secret-command
    env:
      TOKEN: mcp-secret
''')
        fact = discovery.hermes_fact()
        self.assertTrue(fact["ok"])
        self.assertEqual(fact["model"], {"provider": "nous", "default": "Hermes-example"})
        self.assertEqual(fact["mcp_servers"], ["n8n"])
        self.assertFalse(fact["credentials_checked"])
        self.assertTrue(fact["configuration_only"])
        text = json.dumps(fact)
        for secret in ("private", "secret", "do-not-copy", "command", "TOKEN"):
            self.assertNotIn(secret, text)

    def test_invalid_hermes_configuration_never_echoes_source_diagnostics(self):
        config = self.home / ".hermes/config.yaml"
        config.parent.mkdir()
        self.assertEqual(discovery.hermes_fact()["reason"], "configuration-absent")
        for body in ("false", "[]", "[token: sensitive secret", "model: [bad: api_key: sensitive]"):
            with self.subTest(body=body):
                config.write_text(body)
                fact = discovery.hermes_fact()
                self.assertFalse(fact["ok"])
                self.assertNotIn("sensitive", json.dumps(fact))

    def test_redaction_removes_common_tokens_quoted_values_and_credential_urls(self):
        for text in ('Authorization: Bearer abc.def-secret', 'api_key="private value with spaces"',
                     '{"access_token": "private value with spaces"}', 'password=private-value',
                     'https://user:private-password@example.com/path?x=y', 'github_pat_abcdef12345'):
            with self.subTest(text=text):
                cleaned = discovery.clean(text)
                self.assertNotIn("private", cleaned)
                self.assertNotIn("abc.def-secret", cleaned)
                self.assertNotIn("abcdef12345", cleaned)

    def test_memory_absence_and_linux_observation_remain_distinct(self):
        with mock.patch.object(discovery.platform, "system", return_value="Linux"), \
             mock.patch("builtins.open", mock.mock_open(read_data="MemTotal:       2048 kB\n")):
            self.assertEqual(discovery.hardware_fact()["memory_bytes"], 2048 * 1024)
        with mock.patch.object(discovery.platform, "system", return_value="Linux"), \
             mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(discovery.hardware_fact()["memory_bytes"])


if __name__ == "__main__":
    unittest.main()
