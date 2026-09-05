from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest

from fleet_control.cli import main
from tests import test_controller


class ControlCliTests(unittest.TestCase):
    def test_explicit_control_operations_leave_configuration_and_work_unchanged(self):
        temporary, root, repo, state, config, agent, log = test_controller.ControllerTests().fixture(mode="observe-plan")
        with temporary:
            config_bytes = config.read_bytes()
            order_bytes = (state / "work-orders/t_controller_1.json").read_bytes()

            def call(*args):
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    code = main(["--config", str(config), "control", *args])
                return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

            code, status, _ = call("status")
            self.assertEqual(code, 0)
            self.assertEqual(status["reason"], "missing")
            self.assertFalse((state / "control").exists())
            self.assertEqual(call("enable", "--ttl-seconds", "0")[0], 2)
            self.assertFalse((state / "control").exists())
            code, status, _ = call("enable", "--ttl-seconds", "60")
            self.assertEqual(code, 0)
            self.assertTrue(status["permitted"])
            self.assertEqual(call("refresh", "--ttl-seconds", "60")[0], 0)
            code, status, _ = call("drain")
            self.assertEqual(code, 0)
            self.assertEqual(status["mode"], "draining")
            self.assertFalse(status["permitted"])
            self.assertEqual(call("enable", "--ttl-seconds", "60")[0], 2)
            code, status, _ = call("disable")
            self.assertEqual(code, 0)
            self.assertEqual(status["mode"], "disabled")
            self.assertEqual(config.read_bytes(), config_bytes)
            self.assertEqual((state / "work-orders/t_controller_1.json").read_bytes(), order_bytes)
            self.assertEqual(log.read_text(), "")
            self.assertFalse((state / "worktrees").exists())
            self.assertFalse((state / "fleet-history.jsonl").exists())
            self.assertNotIn("mac", status)


if __name__ == "__main__":
    unittest.main()
