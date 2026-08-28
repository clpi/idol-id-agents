from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import atomic_write_json, utc_now


class WorkspaceRefused(RuntimeError):
    pass


_READ_ONLY_ROLES = frozenset({"architect", "counterexample", "reviewer", "evidence"})
_WRITE_ROLES = frozenset({"implementer", "integrator"})
_CANDIDATE_ROLES = frozenset({"reviewer", "evidence", "integrator"})
_SUCCESS_VERDICTS = frozenset({"accepted", "no-counterexample", "ready-for-review", "pass", "ready-for-admission"})


@dataclass(frozen=True)
class Workspace:
    path: Path
    subject_sha: str
    branch: str | None
    read_only: bool


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    proc = _run(["git", *args], cwd=repo, timeout=timeout)
    if proc.returncode != 0:
        raise WorkspaceRefused(f"git {args[0] if args else ''} failed")
    return proc.stdout.strip()


def _safe(value: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9.-]+", "-", value).strip("-.").lower()
    if not cleaned:
        raise WorkspaceRefused("worktree branch identity is empty after normalization")
    return cleaned[:limit]


def _subject(payload: dict[str, Any]) -> str:
    role = str(payload.get("role") or "")
    if role in _CANDIDATE_ROLES:
        candidate = str(payload.get("candidate_sha") or "").strip()
        if not candidate:
            raise WorkspaceRefused(f"{role} requires an exact candidate_sha")
        return candidate
    subject = str(payload.get("base_sha") or "").strip()
    if not subject:
        raise WorkspaceRefused("work order has no exact base_sha")
    return subject


def _branch(payload: dict[str, Any]) -> str:
    task = _safe(str(payload.get("task_id") or "task"), 42)
    role = _safe(str(payload.get("role") or "role"), 20)
    suffix = _safe(str(payload.get("agent_id") or "agent")[-16:], 16)
    return f"fleet/{task}/{role}-{suffix}"


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD")


def _branch_name(path: Path) -> str | None:
    value = _git(path, "branch", "--show-current")
    return value or None


def _is_worktree(path: Path) -> bool:
    if not path.is_dir():
        return False
    proc = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, timeout=20)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def ensure_workspace(
    *,
    repo: Path,
    state_dir: Path,
    payload: dict[str, Any],
) -> Workspace:
    role = str(payload.get("role") or "")
    if role not in _READ_ONLY_ROLES | _WRITE_ROLES:
        raise WorkspaceRefused(f"unsupported fleet role: {role!r}")
    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id:
        raise WorkspaceRefused("agent_id is required")
    subject = _subject(payload)
    # The common object database must already contain the exact admitted subject.
    _git(repo, "cat-file", "-e", f"{subject}^{{commit}}")

    worktree_root = state_dir / "worktrees"
    path = worktree_root / agent_id
    record_path = state_dir / "workspace-records" / f"{agent_id}.json"
    read_only = role in _READ_ONLY_ROLES
    branch = None if read_only else _branch(payload)

    if path.exists():
        if not _is_worktree(path):
            raise WorkspaceRefused("existing workspace path is not a Git worktree")
    else:
        worktree_root.mkdir(parents=True, exist_ok=True)
        if read_only:
            proc = _run(
                ["git", "worktree", "add", "--detach", str(path), subject],
                cwd=repo,
                timeout=180,
            )
        else:
            assert branch is not None
            ref = _run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], cwd=repo)
            if ref.returncode == 0:
                if not record_path.is_file():
                    raise WorkspaceRefused("deterministic branch exists without a workspace receipt")
                proc = _run(["git", "worktree", "add", str(path), branch], cwd=repo, timeout=180)
            else:
                proc = _run(
                    ["git", "worktree", "add", "-b", branch, str(path), subject],
                    cwd=repo,
                    timeout=180,
                )
        if proc.returncode != 0:
            raise WorkspaceRefused("Git refused creation of the exact-subject worktree")

    actual_branch = _branch_name(path)
    if read_only:
        if actual_branch is not None:
            raise WorkspaceRefused("read-only role is not on a detached worktree")
        if _head(path) != subject:
            raise WorkspaceRefused("read-only worktree is not at the exact admitted subject")
    else:
        if actual_branch != branch:
            raise WorkspaceRefused("write worktree is on the wrong deterministic branch")
        # A resumed branch may have commits, but its subject must remain an ancestor.
        proc = _run(["git", "merge-base", "--is-ancestor", subject, "HEAD"], cwd=path)
        if proc.returncode != 0:
            raise WorkspaceRefused("write branch no longer descends from the admitted subject")

    workspace = Workspace(path=path, subject_sha=subject, branch=branch, read_only=read_only)
    atomic_write_json(
        record_path,
        {
            "schema": "idol.agent.workspace.v1",
            "agent_id": agent_id,
            "task_id": payload.get("task_id"),
            "role": role,
            "repo_id": payload.get("repo_id"),
            "base_sha": payload.get("base_sha"),
            "candidate_sha": payload.get("candidate_sha"),
            "subject_sha": subject,
            "path": str(path),
            "branch": branch,
            "read_only": read_only,
            "created_or_verified_at": utc_now().isoformat(),
        },
    )
    return workspace


def _changed_paths(path: Path, subject: str) -> set[str]:
    changed: set[str] = set()
    commands = (
        ["git", "diff", "--name-only", f"{subject}..HEAD"],
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    for command in commands:
        proc = _run(command, cwd=path)
        if proc.returncode != 0:
            raise WorkspaceRefused("could not enumerate the candidate's changed paths")
        changed.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return changed


def verify_handoff(
    *,
    workspace: Workspace,
    payload: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    role = str(payload.get("role") or "")
    actual_head = _head(workspace.path)
    actual_branch = _branch_name(workspace.path)
    verdict = str(handoff.get("verdict") or "")
    changed = _changed_paths(workspace.path, workspace.subject_sha)
    allowed = {str(value) for value in payload.get("paths", [])}
    outside = sorted(changed - allowed)
    if outside:
        raise WorkspaceRefused(f"candidate changed unclaimed paths: {outside[:8]}")

    if workspace.read_only:
        if changed:
            raise WorkspaceRefused("read-only role changed the exact candidate worktree")
        if actual_head != workspace.subject_sha or actual_branch is not None:
            raise WorkspaceRefused("read-only role moved the exact detached subject")
    else:
        if actual_branch != workspace.branch:
            raise WorkspaceRefused("write role changed or detached its deterministic branch")
        if verdict in _SUCCESS_VERDICTS:
            status = _git(workspace.path, "status", "--porcelain=v1")
            if status:
                raise WorkspaceRefused("successful write handoff retains uncommitted work")
            if role == "implementer" and actual_head == workspace.subject_sha:
                raise WorkspaceRefused("implementation handoff contains no committed change")

    if str(handoff.get("final_sha") or "") != actual_head:
        raise WorkspaceRefused("handoff final_sha does not match the worktree")
    expected_branch = workspace.branch or ""
    if str(handoff.get("branch") or "") != expected_branch:
        raise WorkspaceRefused("handoff branch does not match the worktree")
    expected_candidate = str(payload.get("candidate_sha") or "")
    if role in _CANDIDATE_ROLES and str(handoff.get("candidate_sha") or "") != expected_candidate:
        raise WorkspaceRefused("handoff candidate_sha does not match the reviewed candidate")

    verified = dict(handoff)
    verified["workspace_subject_sha"] = workspace.subject_sha
    verified["verified_final_sha"] = actual_head
    verified["verified_branch"] = actual_branch
    verified["verified_changed_paths"] = sorted(changed)
    verified["git_verified_at"] = utc_now().isoformat()
    return verified
