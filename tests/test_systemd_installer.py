import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import Optional
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdInstallerTests(unittest.TestCase):
    @staticmethod
    def executable(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run_apply_installer(
        self,
        root: Path,
        *,
        caller_uid: int = 0,
        account_uid: int = 1001,
        other_manager_state: str = "inactive",
        user: str = "fleetuser",
        system_mode: bool = True,
        provide_service_path: bool = True,
        service_path_value: Optional[str] = None,
        config_update: Optional[dict[str, object]] = None,
        preserve_holds: bool = False,
        precreate_state: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        binaries = root / "bin"
        binaries.mkdir()
        state = root / "state"
        if precreate_state:
            state.mkdir(mode=0o700)
        repository = root / "repository"
        (repository / ".git").mkdir(parents=True)
        service_home = root / "service-home"
        service_home.mkdir(mode=0o700)
        unit_dir = root / "system-units"
        systemctl_log = root / "systemctl.log"
        python_log = root / "python.log"
        runuser_log = root / "runuser.log"
        config = root / "fleet.json"
        raw_config: dict[str, object] = {
            "mode": "apply",
            "auto_calibrate": True,
            "state_dir": str(state),
            "repository": str(repository),
            "routes": [],
            "inventory": {"enabled": False, "auth_env": []},
        }
        if config_update is not None:
            raw_config.update(config_update)
        config.write_text(json.dumps(raw_config) + "\n", encoding="utf-8")

        if preserve_holds:
            for service in ("idol-fleet-idol.service", "idol-fleet-observe.service"):
                drop_in = unit_dir / f"{service}.d"
                drop_in.mkdir(parents=True, exist_ok=True)
                (drop_in / "50-auto-improvement-hold.conf").write_text(
                    "[Unit]\nConditionPathExists=/preserved-hold\n",
                    encoding="utf-8",
                )

        self.executable(binaries / "uname", "#!/bin/sh\nprintf '%s\\n' Linux\n")
        self.executable(
            binaries / "id",
            "#!/bin/sh\n"
            f"if [ \"${{1:-}}\" = -u ]; then printf '%s\\n' {caller_uid}; exit 0; fi\n"
            "exit 99\n",
        )
        self.executable(
            binaries / "systemctl",
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(systemctl_log))}\n"
            "if [ \"${1:-}\" = --version ]; then printf '%s\\n' 'systemd 257'; exit 0; fi\n"
            "for argument in \"$@\"; do\n"
            f"  if [ \"$argument\" = show ]; then printf '%s\\n' {shlex.quote(other_manager_state)}; exit 0; fi\n"
            "done\n"
            "exit 0\n",
        )
        self.executable(
            binaries / "runuser",
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(runuser_log))}\n"
            "while [ \"$#\" -gt 0 ] && [ \"$1\" != -- ]; do shift; done\n"
            "[ \"$#\" -gt 0 ] || exit 99\n"
            "shift\n"
            "exec \"$@\"\n",
        )
        python = binaries / "python3"
        self.executable(
            python,
            "#!/bin/sh\n"
            f"printf '%s|%s|%s|%s|%s|%s\\n' \"${{HOME:-}}\" \"${{USER:-}}\" "
            f"\"${{LOGNAME:-}}\" \"${{PATH:-}}\" \"${{ROOT_ONLY_SECRET-unset}}\" \"$*\" "
            f">> {shlex.quote(str(python_log))}\n"
            "if [ \"${1:-}\" = -c ]; then exit 0; fi\n"
            f"if [ \"${{1:-}}\" = - ] && [ \"${{2:-}}\" = {shlex.quote(user)} ]; then\n"
            f"  printf '%s\\n' {shlex.quote(user)} fleetgroup {shlex.quote(str(service_home))} {account_uid}\n"
            "  exit 0\n"
            "fi\n"
            f"if [ \"${{1:-}}\" = - ]; then exec {shlex.quote(sys.executable)} \"$@\"; fi\n"
            "exit 0\n",
        )

        source = (ROOT / "scripts" / "install-fleet-systemd.sh").read_text(encoding="utf-8")
        root_assignment = 'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)'
        source = source.replace(root_assignment, f"ROOT={shlex.quote(str(ROOT))}")
        source = source.replace(
            "SYSTEM_UNIT_DIR=/etc/systemd/system",
            f"SYSTEM_UNIT_DIR={shlex.quote(str(unit_dir))}",
        )
        installer = root / "install-fleet-systemd.sh"
        self.executable(installer, source)
        environment = os.environ.copy()
        environment.update({
            "HOME": str(root / "caller-home"),
            "PATH": f"{binaries}:{environment['PATH']}",
            "PYTHON": str(python),
            "ROOT_ONLY_SECRET": "must-not-reach-service-user",
        })
        if provide_service_path:
            environment["FLEET_SERVICE_PATH"] = service_path_value or environment["PATH"]
        else:
            environment.pop("FLEET_SERVICE_PATH", None)
        arguments = ["/bin/sh", str(installer)]
        if system_mode:
            arguments.extend(("--system-user", user))
        arguments.extend((str(config), "idol"))
        result = subprocess.run(
            arguments,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        return result, unit_dir, python_log, systemctl_log

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

    def test_system_mode_emits_non_root_service_identity_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, unit_dir, python_log, systemctl_log = self.run_apply_installer(
                root,
                preserve_holds=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = (unit_dir / "idol-fleet-idol.service").read_text(encoding="utf-8")
            self.assertIn("User=fleetuser\n", unit)
            self.assertIn("Group=fleetgroup\n", unit)
            self.assertIn(f"Environment=HOME={root / 'service-home'}\n", unit)
            self.assertIn(f"Environment=PATH={root / 'bin'}:", unit)
            self.assertIn("WantedBy=multi-user.target\n", unit)
            for policy in (
                "NoNewPrivileges=true",
                "PrivateTmp=true",
                "ProtectSystem=strict",
                "ProtectKernelTunables=true",
                "ProtectKernelModules=true",
                "ProtectControlGroups=true",
                "RestrictSUIDSGID=true",
            ):
                self.assertIn(policy, unit)
            recovery = unit_dir / "idol-fleet-idol.service.d/40-restart-backoff.conf"
            self.assertIn("RestartSteps=6", recovery.read_text(encoding="utf-8"))
            proof_log = python_log.read_text(encoding="utf-8")
            service_context = f"{root / 'service-home'}|fleetuser|fleetuser|"
            service_proofs = [
                line for line in proof_log.splitlines() if line.startswith(service_context)
            ]
            self.assertTrue(any(line.endswith("|unset|-m compileall -q fleet_control tests") for line in service_proofs))
            self.assertTrue(any("|unset|-m unittest discover" in line for line in service_proofs))
            self.assertTrue(all("must-not-reach-service-user" not in line for line in service_proofs))
            control = systemctl_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("--user show idol-fleet-idol.service --property=ActiveState --value", control)
            observer_stop = "--user disable --now idol-fleet-observe.service"
            self.assertIn(observer_stop, control)
            self.assertIn("daemon-reload", control)
            self.assertIn("enable idol-fleet-idol.service", control)
            self.assertIn("restart idol-fleet-idol.service", control)
            self.assertLess(control.index(observer_stop), control.index("restart idol-fleet-idol.service"))
            self.assertNotIn("--user daemon-reload", control)
            for service in ("idol-fleet-idol.service", "idol-fleet-observe.service"):
                hold = unit_dir / f"{service}.d/50-auto-improvement-hold.conf"
                self.assertEqual(
                    hold.read_text(encoding="utf-8"),
                    "[Unit]\nConditionPathExists=/preserved-hold\n",
                )

    def test_system_mode_requires_explicit_safe_service_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, unit_dir, _, systemctl_log = self.run_apply_installer(
                Path(directory),
                provide_service_path=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires FLEET_SERVICE_PATH", result.stderr)
            self.assertFalse(unit_dir.exists())

            self.assertEqual(systemctl_log.read_text(encoding="utf-8").splitlines(), ["--version"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, unit_dir, _, _ = self.run_apply_installer(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = (unit_dir / "idol-fleet-idol.service").read_text(encoding="utf-8")
            self.assertIn(f"Environment=PATH={root / 'bin'}:", unit)

        with tempfile.TemporaryDirectory() as directory:
            result, unit_dir, _, _ = self.run_apply_installer(
                Path(directory),
                service_path_value="/usr/bin:%unsafe",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("service PATH contains unsupported unit characters", result.stderr)
            self.assertFalse(unit_dir.exists())

    def test_shared_inventory_path_and_default_disabled_control_are_provisioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result, unit_dir, _, _ = self.run_apply_installer(
                root, config_update={"inventory": {"enabled": True, "auth_env": []}},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            shared = root / "service-home/.local/state/idol-fleet-inventory"
            self.assertEqual(shared.stat().st_mode & 0o777, 0o700)
            self.assertEqual((shared / "snapshot.lock").stat().st_mode & 0o777, 0o600)
            unit = (unit_dir / "idol-fleet-idol.service").read_text()
            self.assertIn(f"ReadWritePaths={root / 'state'} {root / 'repository'} {shared}\n", unit)
            state = root / "state/control/control-state.json"
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            from fleet_control.control import LocalControl
            status = LocalControl(state.parent).status()
            self.assertEqual(status.mode, "disabled")
            self.assertFalse(status.permitted)

    def test_first_install_creates_private_disabled_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result, _, _, _ = self.run_apply_installer(root, precreate_state=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "state").stat().st_mode & 0o777, 0o700)
            from fleet_control.control import LocalControl
            status = LocalControl(root / "state/control").status()
            self.assertTrue(status.integrity_valid)
            self.assertEqual(status.mode, "disabled")
            self.assertFalse(status.permitted)

    def test_system_mode_refuses_uninstalled_credential_environment(self) -> None:
        configurations = (
            {"routes": [{"auth_env": ["PROVIDER_TOKEN"]}]},
            {"routes": [{"usage_auth_env": ["USAGE_TOKEN"]}]},
            {"inventory": {"enabled": False, "auth_env": ["INVENTORY_TOKEN"]}},
        )
        for update in configurations:
            with self.subTest(update=update), tempfile.TemporaryDirectory() as directory:
                result, unit_dir, _, systemctl_log = self.run_apply_installer(
                    Path(directory),
                    config_update=update,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("unsupported credential environment", result.stderr)
                self.assertFalse(unit_dir.exists())
                self.assertNotIn("daemon-reload", systemctl_log.read_text(encoding="utf-8"))

    def test_user_mode_refuses_enabled_live_inventory_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _, _, systemctl_log = self.run_apply_installer(
                root,
                system_mode=False,
                provide_service_path=False,
                config_update={"inventory": {"enabled": True, "auth_env": []}},
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires --system-user USER", result.stderr)
            self.assertFalse((root / "caller-home/.config").exists())
            self.assertNotIn("daemon-reload", systemctl_log.read_text(encoding="utf-8"))

    def test_existing_user_mode_contract_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _, _, systemctl_log = self.run_apply_installer(
                root,
                system_mode=False,
                provide_service_path=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = (root / "caller-home/.config/systemd/user/idol-fleet-idol.service").read_text(
                encoding="utf-8"
            )
            self.assertIn("WantedBy=default.target\n", unit)
            self.assertNotIn("User=", unit)
            control = systemctl_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("show idol-fleet-idol.service --property=ActiveState --value", control)
            self.assertIn("--user daemon-reload", control)
            self.assertIn("--user restart idol-fleet-idol.service", control)

    def test_system_mode_requires_root_before_unit_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, unit_dir, _, _ = self.run_apply_installer(
                Path(directory),
                caller_uid=501,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(unit_dir.exists())

    def test_system_mode_refuses_root_as_service_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, unit_dir, _, _ = self.run_apply_installer(
                Path(directory),
                account_uid=0,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(unit_dir.exists())

    def test_system_mode_refuses_active_user_manager_before_unit_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, unit_dir, _, systemctl_log = self.run_apply_installer(
                Path(directory),
                other_manager_state="active",
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(unit_dir.exists())
            self.assertNotIn("daemon-reload", systemctl_log.read_text(encoding="utf-8"))

    def test_policy_emitter_has_no_unit_or_service_mutations(self) -> None:
        script = (ROOT / "scripts" / "fleet-systemd-recovery.sh").read_text(encoding="utf-8")
        self.assertNotIn("mkdir", script)
        self.assertNotIn("systemctl --user", script)
        self.assertNotIn("daemon-reload", script)


if __name__ == "__main__":
    unittest.main()
