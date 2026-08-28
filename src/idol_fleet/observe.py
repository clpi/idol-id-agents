from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repository, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git observation failed")
    return result.stdout.strip()


def observe_git_repository(repository: Path, identity: str) -> dict[str, Any]:
    repository = Path(repository).resolve()
    head = _git(repository, "rev-parse", "HEAD")
    branch = _git(repository, "branch", "--show-current")
    status = _git(repository, "status", "--porcelain=v1").splitlines()
    remote = _git(repository, "remote", "get-url", "origin") if _git(repository, "remote") else ""
    if "@" in remote and "://" in remote:
        scheme, rest = remote.split("://", 1)
        remote = scheme + "://[credential-redacted]@" + rest.split("@", 1)[1]
    return {
        "kind": "repository",
        "identity": identity,
        "head": head,
        "branch": branch or None,
        "dirty_count": len(status),
        "remote": remote or None,
        "observed_path_hash": hashlib.sha256(str(repository).encode()).hexdigest()[:16],
    }
