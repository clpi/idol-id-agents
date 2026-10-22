from __future__ import annotations

import os
import subprocess
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProviderCoverageTest(unittest.TestCase):
    def test_signed_up_providers_are_routed_and_disabled(self) -> None:
        completed = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "provider_coverage.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("none are enabled", completed.stdout)

    def test_apodex_refuses_without_a_session(self) -> None:
        env = os.environ.copy()
        env.pop("APODEX_API_KEY", None)
        env.pop("APODEX_SESSION_ID", None)
        completed = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "apodex_session.py")],
            check=False,
            capture_output=True,
            text=True,
            input="ping\n",
            env=env,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("chrisp1877@gmail.com", completed.stderr)
        self.assertNotIn("sk-", completed.stderr)


if __name__ == "__main__":
    unittest.main()
