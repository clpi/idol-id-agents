from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fleet_control.processes import kill_group_and_reap


class ProcessCleanupTests(unittest.TestCase):
    def test_exited_leader_still_kills_live_descendant_without_pipe_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            child_path = Path(temporary) / "child.pid"
            process = subprocess.Popen(
                [
                    "/bin/sh",
                    "-c",
                    f"sleep 30 & printf '%s\\n' $! > {shlex.quote(str(child_path))}; exit 0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            child_pid = None
            try:
                process.wait(timeout=2)
                deadline = time.monotonic() + 2
                while not child_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(child_path.exists(), "descendant pid was not recorded")
                child_pid = int(child_path.read_text(encoding="utf-8"))
                started = time.monotonic()
                kill_group_and_reap(process, timeout=0.5)
                self.assertLess(time.monotonic() - started, 0.5)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("descendant survived owned process-group cleanup")
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)

    def test_missing_group_falls_back_to_direct_child_kill(self) -> None:
        process = mock.Mock(pid=4100)
        process.communicate.return_value = ("stdout", "stderr")
        with mock.patch("fleet_control.processes.os.killpg", side_effect=ProcessLookupError):
            result = kill_group_and_reap(process)
        process.kill.assert_called_once_with()
        process.communicate.assert_called_once()
        self.assertEqual(result, ("stdout", "stderr"))

    def test_pipe_timeout_closes_streams_before_bounded_wait(self) -> None:
        process = mock.Mock(pid=4200)
        process.stdin = mock.Mock(closed=False)
        process.stdout = mock.Mock(closed=False)
        process.stderr = mock.Mock(closed=False)
        process.communicate.side_effect = subprocess.TimeoutExpired(
            ["helper"],
            0.1,
            output="partial-out",
            stderr="partial-err",
        )
        with mock.patch("fleet_control.processes.os.killpg"):
            result = kill_group_and_reap(process, timeout=0.1)
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once()
        self.assertEqual(result, ("partial-out", "partial-err"))


if __name__ == "__main__":
    unittest.main()
