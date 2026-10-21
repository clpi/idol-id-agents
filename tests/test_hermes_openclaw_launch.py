from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "scripts" / "hermes-openclaw-launch.mjs"


def run_launch(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    env.update(extra_env)
    return subprocess.run(
        ["node", str(LAUNCH)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class HermesOpenClawLaunchTest(unittest.TestCase):
    def test_launcher_has_no_embedded_credential_material(self) -> None:
        source = LAUNCH.read_text(encoding="utf-8")
        self.assertNotIn("createDecipheriv", source)
        self.assertNotIn("Buffer.from([", source)
        self.assertNotIn("OPENCLAW_GATEWAY_TOKEN", source)

    def test_missing_password_exits_nonzero_with_clear_error(self) -> None:
        result = run_launch({"OPENCLAW_GATEWAY_TOKEN": "unit-test-gateway-token"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDOL_HERMES_BRIDGE_ERROR=missing-hermes-webui-password", result.stderr)

    def test_blank_password_is_treated_as_missing(self) -> None:
        result = run_launch({"HERMES_WEBUI_PASSWORD": "   "})
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDOL_HERMES_BRIDGE_ERROR=missing-hermes-webui-password", result.stderr)

    def test_password_from_env_reaches_bridge_without_gateway_token(self) -> None:
        result = run_launch({"HERMES_WEBUI_PASSWORD": "unit-test-password"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDOL_HERMES_BRIDGE_ERROR=missing-required-environment", result.stderr)
        self.assertNotIn("missing-hermes-webui-password", result.stderr)
        self.assertNotIn("unit-test-password", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
