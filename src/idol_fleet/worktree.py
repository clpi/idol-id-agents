from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    repository: Path
    path: Path
    branch: str
    base_sha: str
    attempt_id: str


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=repository, text=True, capture_output=True
    )
    if check and result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result


class WorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def create(self, *, repository: Path, attempt_id: str, base_sha: str, branch: str) -> WorktreeInfo:
        repository = Path(repository).resolve()
        if not (repository / ".git").exists():
            raise WorktreeError("repository is not a normal git checkout")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", attempt_id):
            raise WorktreeError("invalid attempt id")
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise WorktreeError("invalid base sha")
        if not branch or branch.startswith("/") or ".." in branch:
            raise WorktreeError("invalid branch")
        exact = _git(repository, "rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
        if exact != base_sha:
            raise WorktreeError("base sha did not resolve exactly")
        if _git(repository, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
            raise WorktreeError("branch already exists")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / attempt_id
        if path.exists():
            raise WorktreeError("worktree path already exists")
        _git(repository, "worktree", "add", "--detach", str(path), base_sha)
        try:
            _git(path, "switch", "-c", branch)
        except Exception:
            _git(repository, "worktree", "remove", str(path), check=False)
            raise
        return WorktreeInfo(repository, path, branch, base_sha, attempt_id)

    def inspect(self, info: WorktreeInfo) -> dict[str, object]:
        head = _git(info.path, "rev-parse", "HEAD").stdout.strip()
        dirty = _git(info.path, "status", "--porcelain=v1").stdout.splitlines()
        return {"head": head, "dirty_count": len(dirty), "branch": _git(info.path, "branch", "--show-current").stdout.strip()}

    def retire(self, info: WorktreeInfo, *, require_pushed: bool = True) -> None:
        status = _git(info.path, "status", "--porcelain=v1").stdout.strip()
        if status:
            raise WorktreeError("refusing to retire a dirty worktree")
        if require_pushed:
            upstream = _git(info.path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
            if upstream.returncode != 0:
                raise WorktreeError("refusing to retire branch without upstream")
            ahead = _git(info.path, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
            if ahead != "0":
                raise WorktreeError("refusing to retire unpushed commits")
        _git(info.repository, "worktree", "remove", str(info.path))
        _git(info.repository, "worktree", "prune")
