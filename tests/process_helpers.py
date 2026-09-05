import subprocess
import time


def process_stopped(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=1, check=False,
        )
        if result.returncode == 1 or result.stdout.strip().startswith("Z"):
            return True
        time.sleep(0.01)
    return False
