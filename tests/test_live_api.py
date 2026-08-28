from __future__ import annotations

import json
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from fleet_control.journal import AppendOnlyJournal, project_live
from fleet_control.live_api import LiveApiError, LiveHandler, LiveObservatory, serve
from fleet_control.util import atomic_write_json


class LiveApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        journal = AppendOnlyJournal(self.state / "history.ndjson")
        accepted = journal.append(
            kind="task.accepted",
            subject="task-1",
            actor="integrator",
            payload={"id": "task-1", "state": "accepted"},
            accepted=True,
        )
        live = project_live(journal.read())
        live["last_reconciled_at"] = "2026-08-28T12:00:00+00:00"
        atomic_write_json(self.state / "live.json", live)
        observatory = LiveObservatory(self.state)
        handler = type("BoundLiveHandler", (LiveHandler,), {"observatory": observatory})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.accepted_id = accepted["id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.status, dict(response.headers), json.loads(response.read())

    def test_health_and_live_projection(self):
        status, headers, health = self.get("/health")
        self.assertEqual(200, status)
        self.assertTrue(health["ok"])
        self.assertEqual("no-store", headers["Cache-Control"])
        status, _, live = self.get("/v1/live")
        self.assertEqual(200, status)
        self.assertEqual([self.accepted_id], live["accepted_frontier"])

    def test_bounded_history_and_entity_projection(self):
        status, _, history = self.get("/v1/history?limit=1")
        self.assertEqual(200, status)
        self.assertEqual(1, history["count"])
        status, _, tasks = self.get("/v1/tasks")
        self.assertEqual(200, status)
        self.assertEqual("accepted", tasks["tasks"]["task-1"]["state"])

    def test_mutation_methods_are_refused(self):
        request = urllib.request.Request(self.base + "/v1/live", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(405, caught.exception.code)
        self.assertEqual("read-only", json.loads(caught.exception.read())["error"])

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaises(LiveApiError):
            serve(self.state, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
