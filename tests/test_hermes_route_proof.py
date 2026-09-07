from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hermes_route_proof", ROOT / "scripts/hermes-route-proof.py")
assert SPEC and SPEC.loader
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


class HermesRouteProofTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.config = self.home / ".hermes/config.yaml"
        self.config.parent.mkdir()
        self.home_patch = mock.patch.object(proof.Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_empty_fallback_forms_pass(self):
        for text in ("", "{}", "model: {provider: openai-codex}", "fallback_providers:",
                     "fallback_providers: []", "fallback_providers: null", "fallback_providers: ~",
                     "fallback_providers: [] # no fallback", "\ufefffallback_providers: []"):
            with self.subTest(text=text):
                self.config.write_text(text)
                proof.no_fallbacks()

    def test_indented_indentless_inline_and_nested_fallbacks_refuse(self):
        for text in (
            "fallback_providers:\n- provider: minimax\n  model: MiniMax-M3\nmodel: {}\n",
            "fallback_providers:\n  - provider: minimax\n    model: MiniMax-M3\n",
            "fallback_providers: [{provider: minimax, model: MiniMax-M3}]\n",
            "fallback_providers:\n- - provider: minimax\n    model: MiniMax-M3\n",
            "routes: &routes [{provider: minimax}]\nfallback_providers: *routes\n",
        ):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaisesRegex(RuntimeError, "fallback_providers"):
                    proof.no_fallbacks()

    def test_unrelated_nested_setting_is_not_a_root_fallback(self):
        self.config.write_text("providers:\n  other:\n    fallback_providers: [elsewhere]\nfallback_providers: []\n")
        proof.no_fallbacks()

    def test_observed_29_route_configuration_refuses(self):
        routes = (
            ("copilot", "claude-fable-5.1"), ("copilot", "gpt-5.6-sol"),
            ("copilot", "claude-opus-4.8"), ("openai-codex", "gpt-5.5"),
            ("copilot", "gpt-5.5"), ("copilot", "kimi-k3"), ("zai", "glm-5"),
            ("deepseek", "deepseek-v4-pro"), ("kimi", "cline-pass/kimi-k3"),
            ("minimax", "MiniMax-M3"), ("openrouter", "openrouter/free"),
            ("nous-portal", "poolside/laguna-s-2.1:free"), ("synthetic", "syn:large:text"),
            ("muse", "muse-spark-1.3"), ("nanogpt", "minimax/minimax-m2.7"),
            ("nvidia", "deepseek-ai/deepseek-v4-pro-0813"), ("mimo-lite", "mimo-v2.5-pro"),
            ("inception", "mercury-2"), ("poolside", "poolside/laguna-s-2.1"),
            ("clinepass", "cline-pass/qwen3.7-plus"), ("kilo", "kilo-auto/free"),
            ("opencode-free", "laguna-s-2.1-free"), ("mistral", "mistral-medium-3.5"),
            ("gemini", "gemini-2.5-flash"), ("cloudflare-workers-ai", "@cf/zai-org/glm-5.3"),
            ("xai-oauth", "grok-4"), ("kiro", "kiro-auto"), ("ollama-cloud", "kimi-k3"),
            ("opencode-go", "deepseek-v4-pro"),
        )
        self.assertEqual(len(routes), 29)
        self.config.write_text(yaml.safe_dump({
            "fallback_providers": [{"provider": provider, "model": model} for provider, model in routes],
            "model": {"provider": "copilot", "default": "claude-opus-5"},
        }))
        with self.assertRaisesRegex(RuntimeError, "fallback_providers"):
            proof.no_fallbacks()

    def test_malformed_fallback_types_refuse(self):
        for value in (False, True, 0, "", "[]", {}, {"provider": "minimax"}):
            with self.subTest(value=value):
                self.config.write_text(yaml.safe_dump({"fallback_providers": value}))
                with self.assertRaisesRegex(RuntimeError, "fallback_providers"):
                    proof.no_fallbacks()

    def test_absent_unreadable_invalid_and_non_mapping_config_refuse(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable or invalid"):
            proof.no_fallbacks()
        for text in ("fallback_providers: [", "- provider: minimax", "false", "configuration"):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(RuntimeError):
                    proof.no_fallbacks()
        self.config.write_bytes(b"\xff")
        with self.assertRaisesRegex(RuntimeError, "unavailable or invalid"):
            proof.no_fallbacks()
        with mock.patch.object(proof.Path, "read_text", side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, "unavailable or invalid"):
                proof.no_fallbacks()

    def test_refusal_precedes_provider_or_network_proof(self):
        self.config.write_text("fallback_providers:\n- provider: minimax\n")
        args = argparse.Namespace(provider="openai-codex", model="gpt-5.4", contract="codex-oauth")
        with mock.patch.object(proof, "oauth_logged_in") as oauth, mock.patch.object(proof, "json_get") as get:
            with self.assertRaises(RuntimeError):
                proof.prove(args)
        oauth.assert_not_called()
        get.assert_not_called()

    def test_missing_yaml_dependency_never_attests_ready(self):
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/hermes-route-proof.py"),
             "--provider", "openai-codex", "--model", "gpt-5.4", "--contract", "codex-oauth"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'yaml'", result.stderr)
        self.assertNotIn("IDOL ROUTE READY", result.stdout)


if __name__ == "__main__":
    unittest.main()
