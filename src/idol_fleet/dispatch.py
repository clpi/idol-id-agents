from __future__ import annotations

import time
import uuid
from typing import Any, Mapping

from .journal import Journal
from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import Protocol

from .model import BillingClass, RepositoryPath, Route, WorkOrder
from .process import run_command
from .runtime import RunResult
from .work_order import materialize_prompt
from .worktree import WorktreeInfo, WorktreeManager


class DispatchError(RuntimeError):
    pass


class RuntimeAdapter(Protocol):
    def execute(self, order: WorkOrder, route: Route, prompt_path: Path, cwd: Path, **kwargs: object) -> RunResult: ...


class RepositoryClaimsAdapter(Protocol):
    def acquire(self, owner: str, paths: tuple[RepositoryPath, ...], work: str) -> None: ...
    def release(self, owner: str, paths: tuple[RepositoryPath, ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    attempt_id: str
    state: str
    worktree: Path
    commit_sha: str | None
    run: RunResult
    changed_paths: tuple[str, ...]


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=repository, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise DispatchError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result


def _path_allowed(path: str, claims: tuple[RepositoryPath, ...]) -> bool:
    return any(path == str(claim) or path.startswith(str(claim) + "/") for claim in claims)


def _changed_paths(worktree: Path) -> tuple[str, ...]:
    result = _git(worktree, "status", "--porcelain=v1", "-z")
    raw = result.stdout
    paths: list[str] = []
    entries = raw.split("\x00")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise DispatchError("unparseable git status entry")
        status = entry[:2]
        path = entry[3:]
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(entries):
                raise DispatchError("unparseable rename status")
            path = entries[index]
            index += 1
        paths.append(path)
    return tuple(sorted(set(paths)))


class Dispatcher:
    def __init__(
        self,
        *,
        journal: Journal,
        semantic_claims: object,
        repository_claims: RepositoryClaimsAdapter,
        worktrees: WorktreeManager,
        apply_enabled: bool,
    ) -> None:
        self.journal = journal
        self.semantic_claims = semantic_claims
        self.repository_claims = repository_claims
        self.worktrees = worktrees
        self.apply_enabled = apply_enabled

    def _event(self, attempt: str, kind: str, fact: Mapping[str, Any]) -> None:
        self.journal.append({
            "id": str(uuid.uuid4()),
            "kind": kind,
            "at": time.time(),
            "fact": {"attempt": attempt, **dict(fact)},
        })

    def dispatch(
        self,
        order: WorkOrder,
        route: Route,
        repository: Path,
        runtime: RuntimeAdapter,
        *,
        authority: tuple[tuple[str, str], ...],
    ) -> DispatchOutcome:
        if not self.apply_enabled:
            raise PermissionError("apply mode is not enabled by calibration")
        if route.id != order.route_id:
            raise DispatchError("work order route differs from selected route")
        if route.billing not in {BillingClass.LOCAL, BillingClass.INCLUDED}:
            raise DispatchError("selected route is not local or included")
        if not route.billing_proven:
            raise DispatchError("selected route billing proof is untrusted")
        repository = Path(repository).resolve()
        observed = _git(repository, "rev-parse", "HEAD").stdout.strip()
        if observed != order.base_sha:
            raise DispatchError("stale-base-sha")

        worktree_info: WorktreeInfo | None = None
        semantic_acquired = False
        repository_acquired = False
        run_result: RunResult | None = None
        self._event(order.id, "attempt-validated", {"task": order.task_id, "base_sha": order.base_sha})
        try:
            self.semantic_claims.acquire(owner=order.id, task=order.task_id, targets=order.semantic_claims)
            semantic_acquired = True
            self.repository_claims.acquire(order.id, order.path_claims, order.task_id)
            repository_acquired = True
            self._event(order.id, "attempt-claimed", {"semantic_count": len(order.semantic_claims), "path_count": len(order.path_claims)})
            worktree_info = self.worktrees.create(
                repository=repository,
                attempt_id=order.id,
                base_sha=order.base_sha,
                branch=order.branch,
            )
            with tempfile.TemporaryDirectory(prefix=f"idol-fleet-{order.id}-") as td:
                prompt_path = Path(td) / "work-order.md"
                prompt_path.write_text(materialize_prompt(order, authority=authority), encoding="utf-8")
                self._event(order.id, "attempt-running", {"route": route.id, "provider": route.provider, "model": route.model, "runtime": route.runtime})
                run_result = runtime.execute(order, route, prompt_path, worktree_info.path)
            if not run_result.ok or run_result.status != "ok" or run_result.timed_out or run_result.returncode != 0:
                raise DispatchError("runtime-outcome-not-ok")
            changed = _changed_paths(worktree_info.path)
            outside = tuple(path for path in changed if not _path_allowed(path, order.path_claims))
            if outside:
                self._event(order.id, "attempt-held", {"reason": "outside-claim-change", "outside_count": len(outside)})
                raise DispatchError("outside-claim-change: " + ", ".join(outside))
            if order.role in {"mechanic", "implementer"} and not changed:
                raise DispatchError("editing attempt produced no changed paths")
            for witness in order.witnesses:
                argv = shlex.split(witness)
                if not argv:
                    raise DispatchError("empty witness command")
                result = run_command(argv, cwd=worktree_info.path, timeout=max(30, order.estimated_seconds))
                if result.returncode != 0 or result.timed_out:
                    self._event(order.id, "attempt-held", {"reason": "witness-failed", "returncode": result.returncode, "timed_out": result.timed_out})
                    raise DispatchError("witness-failed")
            commit_sha: str | None = None
            if changed:
                _git(worktree_info.path, "add", "--", *changed)
                message = f"fleet({order.task_id}): {order.required_outcome.strip()[:72]}"
                _git(worktree_info.path, "commit", "-m", message)
                commit_sha = _git(worktree_info.path, "rev-parse", "HEAD").stdout.strip()
            state = "review" if order.risk in {"high", "critical"} or order.reviewer_family else "ready"
            self._event(order.id, f"attempt-{state}", {
                "commit_sha": commit_sha,
                "changed_count": len(changed),
                "usage_total": run_result.usage_total,
                "cost_usd": run_result.cost_usd,
            })
            return DispatchOutcome(order.id, state, worktree_info.path, commit_sha, run_result, changed)
        except Exception as exc:
            if worktree_info is not None:
                self._event(order.id, "attempt-preserved", {"reason": type(exc).__name__, "worktree_hash": __import__("hashlib").sha256(str(worktree_info.path).encode()).hexdigest()[:16]})
            raise
        finally:
            release_errors: list[str] = []
            if repository_acquired:
                try:
                    self.repository_claims.release(order.id, order.path_claims)
                except Exception:
                    release_errors.append("repository")
            if semantic_acquired:
                try:
                    self.semantic_claims.release(owner=order.id, targets=order.semantic_claims)
                except Exception:
                    release_errors.append("semantic")
            if release_errors:
                self._event(order.id, "claim-release-failed", {"claim_kinds": release_errors})
