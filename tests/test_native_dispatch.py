from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from fleet_control.native_dispatch import _bounded


class NativeSubprocessTransportTests(unittest.TestCase):
    def test_completed_native_leader_preserves_result_and_kills_ordinary_descendant(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                child = (
                    "from pathlib import Path; import time; "
                    "Path('child-ready').write_text('ready'); time.sleep(0.3); "
                    "Path('late-mutation').write_text('survived')"
                )
                leader = (
                    "from pathlib import Path\nimport subprocess,sys,time\n"
                    f"subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                    "deadline=time.monotonic()+2\n"
                    "while not Path('child-ready').exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                    "assert Path('child-ready').exists()\n"
                    "print('retained stdout')\nprint('retained stderr', file=sys.stderr)\n"
                    f"raise SystemExit({exit_code})\n"
                )
                result = _bounded([sys.executable, "-c", leader], cwd=directory,
                                  env={"PATH": os.environ.get("PATH", "")}, timeout=5)
                self.assertEqual(result.returncode, exit_code)
                self.assertEqual(result.stdout, b"retained stdout\n")
                self.assertEqual(result.stderr, b"retained stderr\n")
                time.sleep(0.4)
                self.assertFalse((directory / "late-mutation").exists())


if __name__ == "__main__":
    unittest.main()
