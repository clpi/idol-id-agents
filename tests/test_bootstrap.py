"""Exercise bootstrap with an isolated PATH: no real installs, network, or auth."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap.sh"
TEMPLATE = ROOT / "config/.shared-env.template"
BASH = shutil.which("bash")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.dispatch = self.root / "fake-command"
        self.dispatch.write_text(
            f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
bin_dir = Path(os.environ["TEST_BIN"])
with Path(os.environ["TEST_LOG"]).open("a") as output:
    output.write(json.dumps([name, *args]) + "\n")

def provide(command):
    destination = bin_dir / command
    if not destination.exists():
        destination.symlink_to(sys.executable if command == "python3" else os.environ["TEST_DISPATCH"])

if name == "uname":
    print(os.environ.get("TEST_OS", "Linux"))
elif name == "id":
    print(os.environ.get("TEST_UID", "1000"))
elif name == "node":
    print(os.environ.get("TEST_NODE_MAJOR", "22"))
elif name == "sudo":
    os.execvpe(args[0], args, os.environ)
elif name in {"brew", "apt-get"}:
    if "install" in args:
        for command in ("git", "curl", "python3", "node", "npm"):
            provide(command)
elif name == "curl":
    if os.environ.get("TEST_CURL_FAIL"):
        raise SystemExit(22)
    target = Path(args[args.index("--output") + 1])
    target.write_bytes(b"tampered\n" if os.environ.get("TEST_TAMPER") else Path(os.environ["TEST_TEMPLATE"]).read_bytes())
elif name == "npm":
    if os.environ.get("TEST_NPM_FAIL"):
        raise SystemExit(42)
    if not os.environ.get("TEST_NPM_NO_BINARY"):
        executable = {"@anthropic-ai/claude-code": "claude", "@openai/codex": "codex"}[args[-1]]
        target = Path(args[args.index("--prefix") + 1]) / "bin" / executable
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(os.environ["TEST_DISPATCH"])
''',
            encoding="utf-8",
        )
        self.dispatch.chmod(0o755)
        for name in ("dirname", "cp", "rm", "mktemp", "env"):
            (self.bin / name).symlink_to(shutil.which(name))
        for name in ("uname", "id", "git", "curl", "node", "npm"):
            self.provide(name)
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "TEST_BIN": str(self.bin),
            "TEST_LOG": str(self.log),
            "TEST_DISPATCH": str(self.dispatch),
            "TEST_TEMPLATE": str(TEMPLATE),
        }

    def provide(self, name):
        (self.bin / name).symlink_to(self.dispatch)

    def remove_dependencies(self):
        for name in ("git", "curl", "python3", "node", "npm"):
            (self.bin / name).unlink()

    def run_bootstrap(self, *, piped=False, args=()):
        return subprocess.run(
            [BASH, *( ["-s", "--"] if piped else [str(SCRIPT)] ), *args],
            input=SCRIPT.read_text(encoding="utf-8") if piped else None,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def commands(self, name=None):
        commands = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [command for command in commands if name is None or command[0] == name]

    def assert_no_agent_launches(self):
        self.assertFalse(set(command[0] for command in self.commands()) & {"claude", "codex", "hermes", "kilo"})

    def test_checkout_creates_private_template_and_reports_missing_agents(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / ".shared-env"
        self.assertEqual(target.read_bytes(), TEMPLATE.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.commands("curl"))
        packages = self.commands("npm")
        self.assertEqual([args[-1] for args in packages], ["@anthropic-ai/claude-code", "@openai/codex"])
        for args in packages:
            self.assertEqual(args[args.index("--prefix") + 1], str(self.home / ".local"))
        self.assertIn("hermes: not installed/on PATH", result.stdout)
        self.assertIn("kilo: not installed/on PATH", result.stdout)
        self.assertIn("claude: " + str(self.home / ".local/bin/claude"), result.stdout)
        self.assert_no_agent_launches()

    def test_macos_uses_homebrew_for_missing_dependencies(self):
        self.env["TEST_OS"] = "Darwin"
        self.remove_dependencies()
        self.provide("brew")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands("brew"), [["brew", "install", "git", "curl", "python", "node"]])
        self.assertFalse(self.commands("apt-get"))
        self.assertFalse(self.commands("sudo"))

    def test_debian_root_does_not_require_sudo(self):
        self.env["TEST_UID"] = "0"
        self.remove_dependencies()
        self.provide("apt-get")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands("apt-get"), [
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "ca-certificates", "git", "curl", "python3", "nodejs", "npm"],
        ])
        self.assertFalse(self.commands("sudo"))

    def test_debian_uses_sudo_only_for_system_packages(self):
        (self.bin / "git").unlink()
        self.provide("apt-get")
        self.provide("sudo")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        sudo = self.commands("sudo")
        self.assertEqual(len(sudo), 2)
        self.assertTrue(all(args[1:3] == ["env", "DEBIAN_FRONTEND=noninteractive"] for args in sudo))
        self.assertTrue(all("npm" not in args for args in sudo))

    def test_macos_without_homebrew_fails_before_writes(self):
        self.env["TEST_OS"] = "Darwin"
        (self.bin / "git").unlink()
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install Homebrew", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse(self.commands("npm"))

    def test_preprovisioned_linux_needs_no_package_manager(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.commands("apt-get"))
        self.assertFalse(self.commands("brew"))

    def test_pipe_install_fetches_pinned_template(self):
        result = self.run_bootstrap(piped=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / ".shared-env").read_bytes(), TEMPLATE.read_bytes())
        self.assertEqual(len(self.commands("curl")), 1)
        args = self.commands("curl")[0]
        self.assertIn("https://raw.githubusercontent.com/clpi/idol-id-agents/4cb500f1ac17eb358723bf495c3748ea03d4593c/config/.shared-env.template", args)
        self.assertEqual(list(self.home.glob(".shared-env.*")), [])

    def test_bad_download_is_not_published_or_followed_by_install(self):
        for flag in ("TEST_CURL_FAIL", "TEST_TAMPER"):
            with self.subTest(flag=flag):
                self.env[flag] = "1"
                result = self.run_bootstrap(piped=True)
                self.env.pop(flag)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / ".shared-env").exists())
                self.assertEqual(list(self.home.glob(".shared-env.*")), [])
                self.assertFalse(self.commands("npm"))

    def test_existing_secret_and_agents_are_preserved_without_execution(self):
        original = b'export TOKEN="do-not-print-this-fixture"\n'
        target = self.home / ".shared-env"
        target.write_bytes(original)
        for name in ("claude", "codex", "hermes"):
            self.provide(name)
        result = self.run_bootstrap(piped=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertNotIn("do-not-print", result.stdout + result.stderr)
        self.assertFalse(self.commands("curl"))
        self.assertFalse(self.commands("npm"))
        self.assert_no_agent_launches()

    def test_dangling_secret_symlink_is_preserved(self):
        target = self.home / ".shared-env"
        target.symlink_to(self.home / "not-yet-mounted-secret")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertFalse(self.commands("curl"))

    def test_failed_agent_install_does_not_announce_completion(self):
        for flag in ("TEST_NPM_FAIL", "TEST_NPM_NO_BINARY"):
            with self.subTest(flag=flag):
                self.env[flag] = "1"
                result = self.run_bootstrap()
                self.env.pop(flag)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Bootstrap complete", result.stdout)

    def test_unsupported_os_and_old_node_fail_before_writes(self):
        self.env["TEST_OS"] = "FreeBSD"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.home.iterdir()), [])
        self.env["TEST_OS"] = "Linux"
        self.env["TEST_NODE_MAJOR"] = "16"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node.js 18", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_help_is_read_only(self):
        result = self.run_bootstrap(args=("--help",))
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.commands())
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
