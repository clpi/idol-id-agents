from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from fleet_control.control import LocalControl
from fleet_control.gitops import (
    _run_guarded_process,
    commit_claimed,
    create_draft_pull_request,
    create_worktree,
    fast_forward,
    fetch_remote_branch,
    publish_branch,
)


def process_stopped(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            process_stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            pass
        else:
            if process_stat.rsplit(")", 1)[1].split()[0] == "Z":
                return True
        time.sleep(0.01)
    return False


class LocalControlDisabled(RuntimeError):
    pass


class ImmediateProcess:
    next_pid = 4000

    def __init__(self, command, **kwargs) -> None:
        self.args = command
        self.kwargs = kwargs
        self.pid = self.next_pid
        type(self).next_pid += 1
        self.returncode = 0

    def communicate(self, timeout=None):
        return "", None

    def poll(self):
        return self.returncode


class GitMutationGuardTests(unittest.TestCase):
    @staticmethod
    def git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()

    def repository(self, root: Path) -> tuple[Path, str]:
        repository = root / "repository"
        repository.mkdir()
        self.git(repository, "init", "-b", "main")
        self.git(repository, "config", "user.name", "Fleet Test")
        self.git(repository, "config", "user.email", "fleet@example.test")
        (repository / "subject.txt").write_text("base\n", encoding="utf-8")
        self.git(repository, "add", "subject.txt")
        self.git(repository, "commit", "-m", "base")
        return repository, self.git(repository, "rev-parse", "HEAD")

    @staticmethod
    def install_delayed_hook(repository: Path, name: str, child: Path, marker: Path) -> None:
        hook = repository / ".git/hooks" / name
        hook.write_text(
            "#!/bin/sh\n"
            "(\n"
            "  sleep 0.4\n"
            f"  : > {shlex.quote(str(marker))}\n"
            ") &\n"
            f"printf '%s\\n' \"$!\" > {shlex.quote(str(child))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

    def test_maintenance_mutations_use_owned_session_runner(self) -> None:
        repository = Path("/authority")
        old_sha = "1" * 40
        new_sha = "2" * 40
        with (
            mock.patch("fleet_control.gitops.current_branch", return_value="main"),
            mock.patch("fleet_control.gitops.current_sha", side_effect=(old_sha, new_sha)),
            mock.patch("fleet_control.gitops.is_dirty", side_effect=(False, False)),
            mock.patch("fleet_control.gitops.is_ancestor", return_value=True),
            mock.patch("fleet_control.gitops._run_mutating_git") as mutation,
        ):
            fast_forward(repository, branch="main", new_sha=new_sha)
        mutation.assert_called_once_with(
            repository,
            ("merge", "--ff-only", new_sha),
            timeout=180,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "authority"
            path = root / "worktrees/bounded"
            repository.mkdir()
            branch_absent = subprocess.CompletedProcess([], 1, "")
            with (
                mock.patch("fleet_control.gitops.require_exact_subject"),
                mock.patch("fleet_control.gitops.run", return_value=branch_absent),
                mock.patch("fleet_control.gitops.current_sha", return_value=new_sha),
                mock.patch("fleet_control.gitops._run_mutating_git") as mutation,
            ):
                create_worktree(
                    repository=repository,
                    path=path,
                    branch="bounded",
                    base_sha=new_sha,
                )
            self.assertEqual(
                mutation.call_args_list,
                [
                    mock.call(
                        repository,
                        (
                            "worktree",
                            "add",
                            "--no-checkout",
                            "-b",
                            "bounded",
                            str(path),
                            new_sha,
                        ),
                        timeout=180,
                    ),
                    mock.call(path, ("checkout", "--detach", new_sha)),
                    mock.call(path, ("switch", "-C", "bounded", new_sha)),
                ],
            )

        resolved = subprocess.CompletedProcess([], 0, f"{new_sha}\n")
        with (
            mock.patch("fleet_control.gitops._run_mutating_git") as mutation,
            mock.patch("fleet_control.gitops.run", return_value=resolved),
        ):
            self.assertEqual(
                fetch_remote_branch(repository, remote="origin", branch="main"),
                new_sha,
            )
        mutation.assert_called_once_with(
            repository,
            (
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                "refs/heads/main:refs/remotes/origin/main",
            ),
            timeout=120,
        )

    def test_fast_forward_timeout_stops_hook_descendant_before_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, old_sha = self.repository(root)
            self.git(repository, "switch", "-c", "remote")
            (repository / "subject.txt").write_text("next\n", encoding="utf-8")
            self.git(repository, "commit", "-am", "next")
            new_sha = self.git(repository, "rev-parse", "HEAD")
            self.git(repository, "switch", "main")
            child_path = root / "child.pid"
            late_write = root / "late-write"
            self.install_delayed_hook(repository, "post-merge", child_path, late_write)
            real_runner = _run_guarded_process
            control = LocalControl(root / "control")
            control.enable(60)
            mutation_started = None

            @contextmanager
            def wait_for_hook_launch():
                yield
                deadline = time.monotonic() + 2
                while not child_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not child_path.exists():
                    raise AssertionError("post-merge hook did not launch")

            def bounded_runner(command, **kwargs):
                nonlocal mutation_started
                mutation_started = time.monotonic()
                kwargs["timeout"] = 0.1
                kwargs["launch_guard"] = wait_for_hook_launch
                return real_runner(command, **kwargs)

            child_pid = None
            try:
                with (
                    control.transition_permit("maintenance"),
                    mock.patch("fleet_control.gitops.current_branch", return_value="main"),
                    mock.patch("fleet_control.gitops.current_sha", return_value=old_sha),
                    mock.patch("fleet_control.gitops.is_dirty", return_value=False),
                    mock.patch("fleet_control.gitops.is_ancestor", return_value=True),
                    mock.patch("fleet_control.gitops._run_guarded_process", side_effect=bounded_runner),
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    fast_forward(repository, branch="main", new_sha=new_sha)
                self.assertIsNotNone(mutation_started)
                elapsed = time.monotonic() - mutation_started
                self.assertEqual(control.disable().mode, "disabled")
                self.assertLess(elapsed, 3.0)
                child_pid = int(child_path.read_text(encoding="utf-8"))
                self.assertTrue(process_stopped(child_pid))
                time.sleep(0.5)
                self.assertFalse(late_write.exists())
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_create_worktree_timeout_stops_hook_descendant_before_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, base_sha = self.repository(root)
            child_path = root / "child.pid"
            late_write = root / "late-write"
            self.install_delayed_hook(repository, "post-checkout", child_path, late_write)
            worktree = root / "worktrees/bounded"
            real_runner = _run_guarded_process
            control = LocalControl(root / "control")
            control.enable(60)
            with control.transition_permit("claim") as claim_guard:
                attempt_permit = claim_guard.issue_attempt_permit("worktree-test")
            mutation_started = None

            @contextmanager
            def wait_for_hook_launch():
                yield
                deadline = time.monotonic() + 2
                while not child_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not child_path.exists():
                    raise AssertionError("post-checkout hook did not launch")

            def bounded_runner(command, **kwargs):
                nonlocal mutation_started
                if command[1] == "checkout":
                    mutation_started = time.monotonic()
                    kwargs["timeout"] = 0.1
                    kwargs["launch_guard"] = wait_for_hook_launch
                return real_runner(command, **kwargs)

            child_pid = None
            try:
                with (
                    control.transition_permit("worktree", attempt_permit),
                    mock.patch("fleet_control.gitops._run_guarded_process", side_effect=bounded_runner),
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    create_worktree(
                        repository=repository,
                        path=worktree,
                        branch="bounded",
                        base_sha=base_sha,
                    )
                self.assertIsNotNone(mutation_started)
                elapsed = time.monotonic() - mutation_started
                self.assertEqual(control.disable().mode, "disabled")
                self.assertLess(elapsed, 3.0)
                child_pid = int(child_path.read_text(encoding="utf-8"))
                self.assertTrue(process_stopped(child_pid))
                time.sleep(0.5)
                self.assertFalse(late_write.exists())
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_commit_guards_every_mutating_git_launch(self) -> None:
        expected = (
            ("git", "config", "user.name", "Fleet"),
            ("git", "config", "user.email", "fleet@example.test"),
            ("git", "add", "--", "src/a"),
            ("git", "commit", "-m", "bounded change"),
        )
        for denied_launch in range(1, len(expected) + 1):
            with self.subTest(denied_launch=denied_launch), tempfile.TemporaryDirectory() as temporary:
                launches = []
                guard_entries = 0

                def popen(command, **kwargs):
                    launches.append(tuple(command))
                    return ImmediateProcess(command, **kwargs)

                @contextmanager
                def launch_guard():
                    nonlocal guard_entries
                    guard_entries += 1
                    if guard_entries == denied_launch:
                        raise LocalControlDisabled("disabled before mutation")
                    yield

                read_only = subprocess.CompletedProcess([], 1, "")
                with (
                    mock.patch("fleet_control.gitops.subprocess.Popen", side_effect=popen),
                    mock.patch("fleet_control.gitops.subprocess.run", return_value=read_only),
                    self.assertRaisesRegex(LocalControlDisabled, "disabled before mutation"),
                ):
                    commit_claimed(
                        repository=Path(temporary),
                        paths=("src/a",),
                        message="bounded change",
                        author_name="Fleet",
                        author_email="fleet@example.test",
                        launch_guard=launch_guard,
                    )
                self.assertEqual(tuple(launches), expected[: denied_launch - 1])

    def test_push_refuses_before_mutating_launch(self) -> None:
        @contextmanager
        def launch_guard():
            raise LocalControlDisabled("disabled before push")
            yield

        with (
            mock.patch("fleet_control.gitops.subprocess.Popen") as popen,
            self.assertRaisesRegex(LocalControlDisabled, "disabled before push"),
        ):
            publish_branch(Path("/repository"), "bounded", launch_guard=launch_guard)
        popen.assert_not_called()

    def test_pr_list_is_read_only_and_create_regates(self) -> None:
        @contextmanager
        def launch_guard():
            raise LocalControlDisabled("disabled before PR create")
            yield

        listed = subprocess.CompletedProcess([], 0, "[]", "")
        with (
            mock.patch("fleet_control.gitops.subprocess.run", return_value=listed) as run,
            mock.patch("fleet_control.gitops.subprocess.Popen") as popen,
            self.assertRaisesRegex(LocalControlDisabled, "disabled before PR create"),
        ):
            create_draft_pull_request(
                repository=Path("/repository"),
                branch="bounded",
                base="main",
                title="Bounded change",
                body_path=Path("/body.md"),
                launch_guard=launch_guard,
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ["gh", "pr", "list"])
        popen.assert_not_called()

    def test_guard_is_released_while_push_waits(self) -> None:
        communicating = threading.Event()
        finish = threading.Event()
        released = threading.Event()

        class BlockingProcess(ImmediateProcess):
            def communicate(self, timeout=None):
                communicating.set()
                if not finish.wait(2):
                    raise AssertionError("test did not release the fake push")
                self.returncode = 0
                return "ok", None

        @contextmanager
        def launch_guard():
            try:
                yield
            finally:
                released.set()

        failure: list[BaseException] = []

        def publish() -> None:
            try:
                publish_branch(Path("/repository"), "bounded", launch_guard=launch_guard)
            except BaseException as exc:
                failure.append(exc)

        with mock.patch("fleet_control.gitops.subprocess.Popen", side_effect=BlockingProcess):
            worker = threading.Thread(target=publish)
            worker.start()
            try:
                self.assertTrue(communicating.wait(1))
                self.assertTrue(released.is_set(), "guard remained held during push wait")
                self.assertTrue(worker.is_alive())
            finally:
                finish.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failure, [])

    def test_guard_exit_failure_kills_group_after_leader_exit_and_reaps(self) -> None:
        process = ImmediateProcess(["git", "push"])
        process.returncode = 0
        reaped = False

        def communicate(timeout=None):
            nonlocal reaped
            reaped = True
            process.returncode = -signal.SIGKILL
            return "", None

        process.communicate = communicate

        @contextmanager
        def launch_guard():
            yield
            raise LocalControlDisabled("changed during launch")

        with (
            mock.patch("fleet_control.gitops.subprocess.Popen", return_value=process) as popen,
            mock.patch("fleet_control.processes.os.killpg") as killpg,
            self.assertRaisesRegex(LocalControlDisabled, "changed during launch"),
        ):
            publish_branch(Path("/repository"), "bounded", launch_guard=launch_guard)
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertTrue(reaped)

    def test_timeout_kills_group_and_reaps(self) -> None:
        process = ImmediateProcess(["git", "push"])
        process.returncode = None
        communications = 0

        def communicate(timeout=None):
            nonlocal communications
            communications += 1
            if communications == 1:
                raise subprocess.TimeoutExpired(process.args, timeout)
            process.returncode = -signal.SIGKILL
            return "partial", None

        process.communicate = communicate

        @contextmanager
        def launch_guard():
            yield

        with (
            mock.patch("fleet_control.gitops.subprocess.Popen", return_value=process),
            mock.patch("fleet_control.processes.os.killpg") as killpg,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            publish_branch(Path("/repository"), "bounded", launch_guard=launch_guard)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(communications, 2)

    def test_interrupt_kills_group_and_reaps(self) -> None:
        process = ImmediateProcess(["git", "push"])
        process.returncode = None
        communications = 0

        def communicate(timeout=None):
            nonlocal communications
            communications += 1
            if communications == 1:
                raise KeyboardInterrupt
            process.returncode = -signal.SIGKILL
            return "", None

        process.communicate = communicate

        @contextmanager
        def launch_guard():
            yield

        with (
            mock.patch("fleet_control.gitops.subprocess.Popen", return_value=process),
            mock.patch("fleet_control.processes.os.killpg") as killpg,
            self.assertRaises(KeyboardInterrupt),
        ):
            publish_branch(Path("/repository"), "bounded", launch_guard=launch_guard)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(communications, 2)


if __name__ == "__main__":
    unittest.main()
