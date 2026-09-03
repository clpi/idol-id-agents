from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdInstallerTests(unittest.TestCase):
    def test_apply_installer_restarts_an_existing_controller(self) -> None:
        script = (ROOT / "scripts" / "install-fleet-systemd.sh").read_text(encoding="utf-8")
        enable = 'systemctl --user enable "$SERVICE"'
        restart = 'systemctl --user restart "$SERVICE"'
        self.assertIn(enable, script)
        self.assertIn(restart, script)
        self.assertLess(script.index(enable), script.index(restart))
        self.assertNotIn('systemctl --user enable --now "$SERVICE"', script)


if __name__ == "__main__":
    unittest.main()
