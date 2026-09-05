import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdInstallerTests(unittest.TestCase):
    def run_policy(self, version_output: str, *, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            systemctl = Path(directory) / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" != --version ]; then exit 99; fi\n"
                "cat <<'EOF'\n"
                f"{version_output}\n"
                "EOF\n"
                f"exit {exit_code}\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{directory}:{environment['PATH']}"
            return subprocess.run(
                [str(ROOT / "scripts" / "fleet-systemd-recovery.sh")],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

    def test_modern_systemd_uses_bounded_stepped_backoff(self) -> None:
        result = self.run_policy("systemd 257 (257.9-1)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "[Unit]\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            "Restart=always\n"
            "RestartSec=30s\n"
            "RestartSteps=6\n"
            "RestartMaxDelaySec=15min\n",
        )

    def test_legacy_systemd_uses_fixed_portable_backoff(self) -> None:
        result = self.run_policy("systemd 253 (253.17-1)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "[Unit]\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            "Restart=always\n"
            "RestartSec=5min\n",
        )
        self.assertNotIn("RestartSteps", result.stdout)
        self.assertNotIn("RestartMaxDelaySec", result.stdout)

    def test_unknown_systemd_version_refuses(self) -> None:
        for output, exit_code in (("not systemd", 0), ("systemd rolling", 0), ("", 1)):
            with self.subTest(output=output, exit_code=exit_code):
                result = self.run_policy(output, exit_code=exit_code)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_installers_write_policy_before_service_mutation(self) -> None:
        for name in ("install-fleet-systemd.sh", "install-fleet-observer-systemd.sh"):
            with self.subTest(installer=name):
                script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                resolve = 'RECOVERY_POLICY=$("$ROOT/scripts/fleet-systemd-recovery.sh")'
                write = "40-restart-backoff.conf"
                self.assertIn(resolve, script)
                self.assertIn(write, script)
                self.assertIn("printf '%s\\n' \"$RECOVERY_POLICY\" > \"$RECOVERY_UNIT\"", script)
                self.assertLess(script.index(resolve), script.index("mkdir -p"))
                self.assertLess(script.index(resolve), script.index("systemctl --user"))

    def test_installers_refuse_unknown_version_without_mutating_home(self) -> None:
        for name in ("install-fleet-systemd.sh", "install-fleet-observer-systemd.sh"):
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                binaries = temporary / "bin"
                binaries.mkdir()
                uname = binaries / "uname"
                uname.write_text("#!/bin/sh\nprintf '%s\\n' Linux\n", encoding="utf-8")
                uname.chmod(0o755)
                systemctl = binaries / "systemctl"
                systemctl.write_text(
                    "#!/bin/sh\n"
                    "if [ \"${1:-}\" = --version ]; then\n"
                    "  printf '%s\\n' 'systemd rolling'\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 99\n",
                    encoding="utf-8",
                )
                systemctl.chmod(0o755)
                config = temporary / "fleet.json"
                config.write_text("{}\n", encoding="utf-8")
                environment = os.environ.copy()
                environment["HOME"] = str(temporary / "home")
                environment["PATH"] = f"{binaries}:{environment['PATH']}"
                result = subprocess.run(
                    ["/bin/sh", str(ROOT / "scripts" / name), str(config)],
                    capture_output=True,
                    check=False,
                    env=environment,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse((temporary / "home" / ".config").exists())

    def test_apply_installer_restarts_an_existing_controller(self) -> None:
        script = (ROOT / "scripts" / "install-fleet-systemd.sh").read_text(encoding="utf-8")
        enable = 'systemctl --user enable "$SERVICE"'
        restart = 'systemctl --user restart "$SERVICE"'
        self.assertIn(enable, script)
        self.assertIn(restart, script)
        self.assertLess(script.index(enable), script.index(restart))
        self.assertNotIn('systemctl --user enable --now "$SERVICE"', script)

    def test_policy_emitter_has_no_unit_or_service_mutations(self) -> None:
        script = (ROOT / "scripts" / "fleet-systemd-recovery.sh").read_text(encoding="utf-8")
        self.assertNotIn("mkdir", script)
        self.assertNotIn("systemctl --user", script)
        self.assertNotIn("daemon-reload", script)


if __name__ == "__main__":
    unittest.main()
