from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "openclaw-probe.mjs"
SEED = "11" * 32
FAKE_GATEWAY = """
import { createRequire } from "node:module";
const require = createRequire(process.env.REPO_ROOT + "/package.json");
const { WebSocketServer } = require("ws");
const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
server.on("listening", () => console.log("PORT=" + server.address().port));
server.on("connection", (socket) => {
  socket.send(JSON.stringify({ type: "event", event: "connect.challenge", payload: { nonce: "fixture-nonce", ts: Date.now() } }));
  socket.on("message", (raw) => {
    const frame = JSON.parse(raw.toString("utf8"));
    if (frame.method === "connect") {
      console.log("CONNECT=" + JSON.stringify({ auth: frame.params.auth, deviceId: frame.params.device.id, scopes: frame.params.scopes }));
      socket.send(JSON.stringify({ type: "res", id: frame.id, ok: true, payload: { features: { methods: [] } } }));
      return;
    }
    socket.send(JSON.stringify({ type: "res", id: frame.id, ok: true, payload: {} }));
  });
});
setTimeout(() => process.exit(0), 30000).unref();
"""


class FakeGateway:
    def __enter__(self) -> "FakeGateway":
        self.dir = tempfile.TemporaryDirectory()
        script = Path(self.dir.name) / "fake-gateway.mjs"
        script.write_text(FAKE_GATEWAY, encoding="utf-8")
        self.proc = subprocess.Popen(
            ["node", str(script)],
            env={"PATH": os.environ.get("PATH", ""), "REPO_ROOT": str(ROOT)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.proc.stdout.readline().strip()
        if not line.startswith("PORT="):
            raise RuntimeError("fake gateway did not start: " + line + self.proc.stderr.read())
        self.url = f"ws://127.0.0.1:{line[5:]}"
        return self

    def connect_params(self) -> dict:
        line = self.proc.stdout.readline().strip()
        if not line.startswith("CONNECT="):
            raise AssertionError("no connect frame seen: " + line)
        return json.loads(line[8:])

    def __exit__(self, *exc: object) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)
        self.dir.cleanup()


def run_probe(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "OPENCLAW_PROBE_TIMEOUT_MS": "5000"}
    env.update(extra_env)
    return subprocess.run(["node", str(PROBE)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)


def probe_report(result: subprocess.CompletedProcess[str]) -> dict:
    line = next(row for row in result.stdout.splitlines() if row.startswith("IDOL_OPENCLAW_PROBE_V3="))
    return json.loads(line[len("IDOL_OPENCLAW_PROBE_V3="):])


class OpenClawProbeAuthTest(unittest.TestCase):
    def test_device_token_alone_connects_with_device_token_auth(self) -> None:
        with FakeGateway() as gateway:
            result = run_probe({
                "OPENCLAW_GATEWAY_URL": gateway.url,
                "OPENCLAW_DEVICE_SEED": SEED,
                "OPENCLAW_DEVICE_TOKEN": "fixture-device-token",
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            params = gateway.connect_params()
        self.assertEqual(params["auth"], {"token": "fixture-device-token", "deviceToken": "fixture-device-token"})
        self.assertEqual(params["scopes"], ["operator.read"])
        report = probe_report(result)
        self.assertEqual(report["authMode"], "device-token")
        self.assertEqual(report["deviceId"], params["deviceId"])
        self.assertNotIn("fixture-device-token", result.stdout + result.stderr)

    def test_gateway_token_alone_still_connects(self) -> None:
        with FakeGateway() as gateway:
            result = run_probe({
                "OPENCLAW_GATEWAY_URL": gateway.url,
                "OPENCLAW_DEVICE_SEED": SEED,
                "OPENCLAW_GATEWAY_TOKEN": "fixture-gateway-token",
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            params = gateway.connect_params()
        self.assertEqual(params["auth"], {"token": "fixture-gateway-token"})
        self.assertEqual(probe_report(result)["authMode"], "signed-shared-token")

    def test_no_token_exits_with_missing_required_environment(self) -> None:
        result = run_probe({"OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:9", "OPENCLAW_DEVICE_SEED": SEED})
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDOL_OPENCLAW_PROBE_ERROR=missing-required-environment", result.stderr)

    def test_device_token_without_seed_exits_with_missing_required_environment(self) -> None:
        result = run_probe({"OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:9", "OPENCLAW_DEVICE_TOKEN": "fixture-device-token"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDOL_OPENCLAW_PROBE_ERROR=missing-required-environment", result.stderr)

    def test_device_token_without_url_exits_with_missing_required_environment(self) -> None:
        result = run_probe({"OPENCLAW_DEVICE_SEED": SEED, "OPENCLAW_DEVICE_TOKEN": "fixture-device-token"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDOL_OPENCLAW_PROBE_ERROR=missing-required-environment", result.stderr)


if __name__ == "__main__":
    unittest.main()
