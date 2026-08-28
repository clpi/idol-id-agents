from __future__ import annotations

import contextlib
import importlib.util
import json
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "probe_claw_ui", ROOT / "scripts" / "probe_claw_ui.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class FixtureHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        status, content_type, body = self.routes.get(
            self.path,
            (404, "text/plain; charset=utf-8", b"missing"),
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextlib.contextmanager
def fixture(routes: dict[str, tuple[int, str, bytes]]):
    FixtureHandler.routes = routes
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def page(*, fallback: bool = False) -> bytes:
    marker = "<h1>Control UI did not start</h1>" if fallback else "<openclaw-app></openclaw-app>"
    return (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="/assets/app.css">'
        '<script type="module" src="/assets/app.js"></script>'
        f"</head><body>{marker}</body></html>"
    ).encode()


class ProbeTests(unittest.TestCase):
    def test_healthy_assets_and_protected_runtime_config_pass(self) -> None:
        routes = {
            "/": (200, "text/html; charset=utf-8", page()),
            "/assets/app.css": (200, "text/css", b"body{}"),
            "/assets/app.js": (
                200,
                "text/javascript; charset=utf-8",
                b"customElements.define('openclaw-app', class extends HTMLElement {})",
            ),
            "/control-ui-config.json": (401, "application/json", b'{"error":"auth"}'),
        }
        with fixture(routes) as url:
            report = probe.probe(url, timeout=2)
        self.assertTrue(report["ok"], json.dumps(report, indent=2))
        self.assertEqual(report["config"]["status"], 401)
        self.assertEqual(report["summary"]["failed_assets"], 0)
        self.assertEqual(report["summary"]["script_assets"], 1)

    def test_fallback_marker_is_http_observation_not_boot_verdict(self) -> None:
        routes = {
            "/": (200, "text/html", page(fallback=True)),
            "/assets/app.css": (200, "text/css", b"body{}"),
            "/assets/app.js": (200, "application/javascript", b"console.log('loaded')"),
            "/control-ui-config.json": (403, "application/json", b"{}"),
        }
        with fixture(routes) as url:
            report = probe.probe(url, timeout=2)
        self.assertTrue(report["ok"], json.dumps(report, indent=2))
        self.assertTrue(report["root"]["fallback_marker"])
        self.assertNotIn("fallback-shell-visible", report["errors"])

    def test_missing_script_fails_with_exact_asset_evidence(self) -> None:
        routes = {
            "/": (200, "text/html", page()),
            "/assets/app.css": (200, "text/css", b"body{}"),
            "/control-ui-config.json": (401, "application/json", b"{}"),
        }
        with fixture(routes) as url:
            report = probe.probe(url, timeout=2)
        self.assertFalse(report["ok"])
        failed = [item for item in report["assets"] if not item["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["path"], "/assets/app.js")
        self.assertEqual(failed[0]["status"], 404)

    def test_script_served_as_html_fails_instead_of_looking_reachable(self) -> None:
        routes = {
            "/": (200, "text/html", page()),
            "/assets/app.css": (200, "text/css", b"body{}"),
            "/assets/app.js": (200, "text/html", b"<!doctype html>fallback"),
            "/control-ui-config.json": (401, "application/json", b"{}"),
        }
        with fixture(routes) as url:
            report = probe.probe(url, timeout=2)
        self.assertFalse(report["ok"])
        script = next(item for item in report["assets"] if item["kind"] == "script")
        self.assertEqual(script["error"], "unexpected-content-type:text/html")

    def test_runtime_config_is_recursively_redacted(self) -> None:
        routes = {
            "/": (200, "text/html", page()),
            "/assets/app.css": (200, "text/css", b"body{}"),
            "/assets/app.js": (200, "text/javascript", b"console.log('ok')"),
            "/control-ui-config.json": (
                200,
                "application/json",
                b'{"gateway":"wss://example.invalid","token":"secret","nested":{"password":"hidden"}}',
            ),
        }
        with fixture(routes) as url:
            report = probe.probe(url, timeout=2)
        self.assertTrue(report["ok"])
        self.assertEqual(report["config"]["json"]["token"], "<redacted>")
        self.assertEqual(report["config"]["json"]["nested"]["password"], "<redacted>")
        self.assertEqual(report["config"]["json"]["gateway"], "wss://example.invalid")

    def test_no_external_script_is_a_boot_failure(self) -> None:
        routes = {
            "/": (200, "text/html", b"<!doctype html><html><body>empty</body></html>"),
            "/control-ui-config.json": (401, "application/json", b"{}"),
        }
        with fixture(routes) as url:
            report = probe.probe(url, timeout=2)
        self.assertFalse(report["ok"])
        self.assertIn("no-external-script", report["errors"])


if __name__ == "__main__":
    unittest.main()
