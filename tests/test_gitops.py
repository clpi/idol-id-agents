from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from fleet_control.gitops import commit_claimed, create_draft_pull_request, publish_branch


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


if __name__ == "__main__":
    unittest.main()
