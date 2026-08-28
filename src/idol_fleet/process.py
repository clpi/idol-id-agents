from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

from .model import CommandResult


_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TMPDIR", "HOME", "USER", "LOGNAME", "SHELL")


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float,
    kill_grace: float = 2.0,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise TypeError("argv must be an argument-vector sequence")
    args = tuple(str(value) for value in argv)
    if not args or any("\x00" in value for value in args):
        raise ValueError("argv must contain valid arguments")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    child_env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
    if env:
        for key, value in env.items():
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("invalid environment entry")
            child_env[str(key)] = str(value)
    started = time.monotonic()
    proc = subprocess.Popen(
        args,
        cwd=None if cwd is None else str(cwd),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=max(0.0, kill_grace))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
    duration = time.monotonic() - started
    returncode = proc.returncode
    sig = -returncode if returncode is not None and returncode < 0 else None
    return CommandResult(
        argv=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=duration,
        signal=sig,
    )
