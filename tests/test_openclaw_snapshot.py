from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = ROOT / "plugins" / "openclaw-fleet-snapshot" / "snapshot.test.js"


class OpenClawFleetSnapshotPluginTests(unittest.TestCase):
    def test_node_contract_suite(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")

        completed = subprocess.run(
            [node, "--test", str(NODE_TEST)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertNotIn("fixture-private-content", output)


if __name__ == "__main__":
    unittest.main()
