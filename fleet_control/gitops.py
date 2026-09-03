from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Iterable, Sequence


class GitRefusal(RuntimeError):
    pass


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")


@dataclass(frozen=True, slots=True)
class PublishedHandoff:
    branch: str
    commit: str
    pull_request_url: str | None


def run(
    repository: Path,
    arguments: Sequence[str],
    *,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise GitRefusal(result.stdout.strip() or f"git {' '.join(arguments)} failed")
    return result


def current_sha(repository: Path) -> str:
    value = run(repository, ("rev-parse", "HEAD")).stdout.strip()
    if not _SHA_RE.fullmatch(value):
        raise GitRefusal("repository HEAD is not an exact SHA")
    return value


def _branch_ref(branch: str) -> str:
    if (
        not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch.endswith(("/", ".", ".lock"))
    ):
        raise GitRefusal(f"invalid remote branch: {branch!r}")
    return f"refs/heads/{branch}"


def _remote(remote: str) -> str:
    if not _REMOTE_RE.fullmatch(remote):
        raise GitRefusal(f"invalid remote name: {remote!r}")
    return remote


def remote_branch_sha(repository: Path, *, remote: str, branch: str) -> str:
    ref = _branch_ref(branch)
    result = run(
        repository,
        ("ls-remote", "--exit-code", _remote(remote), ref),
        timeout=30,
    )
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref or not _SHA_RE.fullmatch(rows[0][0]):
        raise GitRefusal(f"remote branch {remote}/{branch} did not resolve to one exact SHA")
    return rows[0][0]


def fetch_remote_branch(repository: Path, *, remote: str, branch: str) -> str:
    ref = _branch_ref(branch)
    remote_name = _remote(remote)
    tracking_ref = f"refs/remotes/{remote_name}/{branch}"
    run(
        repository,
        ("fetch", "--no-tags", "--quiet", remote_name, f"{ref}:{tracking_ref}"),
        timeout=120,
    )
    value = run(repository, ("rev-parse", tracking_ref)).stdout.strip()
    if not _SHA_RE.fullmatch(value):
        raise GitRefusal(f"fetched branch {remote}/{branch} is not an exact SHA")
    return value


def current_branch(repository: Path) -> str:
    value = run(repository, ("symbolic-ref", "--quiet", "--short", "HEAD")).stdout.strip()
    _branch_ref(value)
    return value


def is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    if not _SHA_RE.fullmatch(ancestor) or not _SHA_RE.fullmatch(descendant):
        raise GitRefusal("ancestor check requires exact SHAs")
    result = run(repository, ("merge-base", "--is-ancestor", ancestor, descendant), check=False)
    if result.returncode not in {0, 1}:
        raise GitRefusal(result.stdout.strip() or "git merge-base failed")
    return result.returncode == 0


def paths_changed(repository: Path, old: str, new: str, paths: Sequence[str]) -> bool:
    if not _SHA_RE.fullmatch(old) or not _SHA_RE.fullmatch(new):
        raise GitRefusal("path comparison requires exact SHAs")
    if not paths:
        raise GitRefusal("path comparison requires watched paths")
    for path in paths:
        _parts(path)
    result = run(repository, ("diff", "--quiet", old, new, "--", *paths), check=False)
    if result.returncode not in {0, 1}:
        raise GitRefusal(result.stdout.strip() or "git path comparison failed")
    return result.returncode == 1


def fast_forward(repository: Path, *, branch: str, new_sha: str) -> None:
    if current_branch(repository) != branch:
        raise GitRefusal(f"authority worktree is not on {branch}")
    old_sha = current_sha(repository)
    if old_sha == new_sha:
        return
    if is_dirty(repository):
        raise GitRefusal("authority worktree is dirty")
    if not is_ancestor(repository, old_sha, new_sha):
        raise GitRefusal("remote branch is not a fast-forward of the authority worktree")
    run(repository, ("merge", "--ff-only", new_sha), timeout=180)
    if current_sha(repository) != new_sha or is_dirty(repository):
        raise GitRefusal("authority worktree did not reach the clean fetched SHA")


def require_exact_subject(repository: Path, expected_sha: str) -> None:
    observed = current_sha(repository)
    if observed != expected_sha:
        raise GitRefusal(f"stale work order: expected {expected_sha}, repository is {observed}")


def is_dirty(repository: Path) -> bool:
    return bool(run(repository, ("status", "--porcelain=v1", "--untracked-files=all")).stdout)


def create_worktree(*, repository: Path, path: Path, branch: str, base_sha: str) -> None:
    require_exact_subject(repository, base_sha)
    if path.exists():
        raise GitRefusal(f"worktree path already exists: {path}")
    branch_check = run(repository, ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"), check=False)
    if branch_check.returncode == 0:
        raise GitRefusal(f"worktree branch already exists: {branch}")
    path.parent.mkdir(parents=True, exist_ok=True)
    run(repository, ("worktree", "add", "--no-checkout", "-b", branch, str(path), base_sha), timeout=180)
    run(path, ("checkout", "--detach", base_sha))
    run(path, ("switch", "-C", branch, base_sha))
    if current_sha(path) != base_sha:
        raise GitRefusal("created worktree does not have the requested base SHA")


def _names(repository: Path, arguments: Sequence[str]) -> set[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise GitRefusal(result.stderr.decode("utf-8", "replace").strip() or "git path census failed")
    return {
        item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def changed_paths(repository: Path) -> tuple[str, ...]:
    paths = set()
    paths.update(_names(repository, ("diff", "--name-only", "-z", "--diff-filter=ACDMRTUXB")))
    paths.update(_names(repository, ("diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRTUXB")))
    paths.update(_names(repository, ("ls-files", "--others", "--exclude-standard", "-z")))
    return tuple(sorted(paths))


def _parts(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if value.startswith("/") or any(part in {"", ".", ".."} for part in path.parts):
        raise GitRefusal(f"changed path is not repository-relative: {value!r}")
    return path.parts


def path_is_claimed(path: str, claims: Iterable[str]) -> bool:
    observed = _parts(path)
    for raw in claims:
        claim = _parts(raw.rstrip("/"))
        if observed == claim or observed[: len(claim)] == claim:
            return True
    return False


def require_claimed_changes(repository: Path, claims: Sequence[str]) -> tuple[str, ...]:
    return require_claimed_paths(changed_paths(repository), claims)


def require_claimed_paths(paths: Sequence[str], claims: Sequence[str]) -> tuple[str, ...]:
    paths = tuple(paths)
    if not paths:
        raise GitRefusal("attempt produced no changed files")
    outside = tuple(path for path in paths if not path_is_claimed(path, claims))
    if outside:
        raise GitRefusal("attempt edited outside its path claims: " + ", ".join(outside))
    return paths


def commit_claimed(
    *,
    repository: Path,
    paths: Sequence[str],
    message: str,
    author_name: str,
    author_email: str,
) -> str:
    if not paths:
        raise GitRefusal("cannot commit an empty attempt")
    run(repository, ("config", "user.name", author_name))
    run(repository, ("config", "user.email", author_email))
    run(repository, ("add", "--", *paths))
    if run(repository, ("diff", "--cached", "--quiet"), check=False).returncode == 0:
        raise GitRefusal("claimed paths contain no staged change")
    run(repository, ("commit", "-m", message), timeout=180)
    return current_sha(repository)


def publish_branch(repository: Path, branch: str) -> None:
    run(repository, ("push", "--porcelain", "--set-upstream", "origin", branch), timeout=300)


def create_draft_pull_request(
    *,
    repository: Path,
    branch: str,
    base: str,
    title: str,
    body_path: Path,
) -> str:
    existing = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if existing.returncode != 0:
        raise GitRefusal(existing.stderr.strip() or "gh pr list failed")
    try:
        rows = json.loads(existing.stdout)
    except json.JSONDecodeError as exc:
        raise GitRefusal("gh pr list returned invalid JSON") from exc
    if isinstance(rows, list) and rows:
        url = rows[0].get("url") if isinstance(rows[0], dict) else None
        if isinstance(url, str) and url:
            return url
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body-file",
            str(body_path),
        ],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise GitRefusal(result.stderr.strip() or "gh pr create failed")
    url = result.stdout.strip().splitlines()[-1]
    if not url.startswith("https://"):
        raise GitRefusal("gh pr create returned no pull-request URL")
    return url
