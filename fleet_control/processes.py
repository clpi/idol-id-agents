from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any


def kill_group_and_reap(
    process: subprocess.Popen[Any],
    *,
    timeout: float = 2.0,
) -> tuple[Any, Any]:
    """Kill an owned process session and reap its leader within a bounded wait."""
    if timeout <= 0:
        raise ValueError("process reap timeout must be positive")

    deadline = time.monotonic() + timeout
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    try:
        return process.communicate(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
        return exc.stdout, exc.stderr
