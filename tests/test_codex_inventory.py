from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_inventory",
    ROOT / "scripts" / "codex_inventory.py",
)
assert SPEC and SPEC.loader
codex = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex
SPEC.loader.exec_module(codex)


class FakeRpc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CodexInventoryTests(unittest.TestCase):
    def process(self, pid: int, arguments: tuple[str, ...]):
        return codex.ProcessRecord(
            pid=pid,
            uid=os.getuid(),
            start_time=str(pid * 10),
            arguments=arguments,
            directory=Path(f"/proc/{pid}"),
        )

    def test_process_parser_distinguishes_daemon_proxy_and_standalone_work(self) -> None:
        listener = self.process(
            10,
            (
                "codex",
                "-c",
                "features.code_mode_host=true",
                "app-server",
                "--listen",
                "unix://",
            ),
        )
        proxy = self.process(11, ("codex", "app-server", "proxy"))
        tui = self.process(12, ("codex", "--model", "gpt-6"))
        execute = self.process(13, ("codex", "exec", "audit app-server --listen unix://"))
        configured = self.process(14, ("codex", "-c", "note=app-server", "exec", "work"))

        self.assertEqual(codex.control_process_kind(listener), "listener")
        self.assertEqual(codex.control_process_kind(proxy), "proxy")
        self.assertEqual(codex.control_process_kind(tui), "worker")
        self.assertEqual(codex.control_process_kind(execute), "worker")
        self.assertEqual(codex.control_process_kind(configured), "worker")
        self.assertEqual(
            codex.control_process_kind(
                self.process(15, ("codex", "app-server", "--listen", "stdio://"))
            ),
            "unsupported",
        )

    def test_process_scan_returns_every_stable_visible_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            stat_row = "1 (command name) S " + " ".join(["0"] * 18 + ["500"])
            for pid, command in (("10", b"codex\0exec\0task\0"), ("20", b"python3\0worker.py\0")):
                directory = proc / pid
                directory.mkdir()
                (directory / "stat").write_text(stat_row)
                (directory / "cmdline").write_bytes(command)
            (proc / "not-a-pid").mkdir()

            records = codex.scan_processes(proc)

            self.assertEqual([record.pid for record in records], [10, 20])
            self.assertEqual(records[0].start_time, "500")
            self.assertEqual(records[0].arguments, ("codex", "exec", "task"))

    def test_loaded_thread_pages_are_exhausted_and_idle_rows_are_omitted(self) -> None:
        rpc = FakeRpc(
            [
                {"data": ["thread-a"], "nextCursor": "page-2"},
                {"data": ["thread-b"], "nextCursor": None},
                {"thread": self.thread("thread-a", "idle", preview="private-a")},
                {"thread": self.thread("thread-b", "idle", preview="private-b")},
            ]
        )

        rows = codex.loaded_thread_rows(rpc, observed_at=100.0, hostname="r16")

        self.assertEqual(rows, ())
        self.assertEqual(
            rpc.requests[:2],
            [
                ("thread/loaded/list", {"cursor": None, "limit": 1000}),
                ("thread/loaded/list", {"cursor": "page-2", "limit": 1000}),
            ],
        )
        self.assertEqual(
            rpc.requests[2:],
            [
                ("thread/read", {"threadId": "thread-a", "includeTurns": False}),
                ("thread/read", {"threadId": "thread-b", "includeTurns": False}),
            ],
        )

    def test_active_thread_is_unidentified_and_contains_no_response_content(self) -> None:
        rpc = FakeRpc(
            [
                {"data": ["thread-a"], "nextCursor": None},
                {
                    "thread": self.thread(
                        "thread-a",
                        "active",
                        preview="private prompt",
                        activeFlags=["waitingOnApproval"],
                    )
                },
            ]
        )

        rows = codex.loaded_thread_rows(rpc, observed_at=100.0, hostname="r16")

        self.assertEqual(
            rows,
            (
                {
                    "id": "thread-a",
                    "status": "running",
                    "last_activity": 100.0,
                    "host": "r16",
                    "actor": "codex-cli",
                },
            ),
        )
        self.assertNotIn("private prompt", json.dumps(rows))
        self.assertNotIn("task_id", rows[0])
        self.assertNotIn("order_id", rows[0])

    def test_loaded_list_rejects_repeated_cursor(self) -> None:
        rpc = FakeRpc(
            [
                {"data": ["thread-a"], "nextCursor": "repeat"},
                {"data": ["thread-b"], "nextCursor": "repeat"},
            ]
        )
        with self.assertRaisesRegex(codex.InventoryError, "pagination"):
            codex.loaded_thread_rows(rpc, observed_at=100.0, hostname="r16")

    def test_unknown_or_inconsistent_loaded_status_is_refused(self) -> None:
        for status in ("notLoaded", "systemError", "new-status"):
            with self.subTest(status=status):
                rpc = FakeRpc(
                    [
                        {"data": ["thread-a"], "nextCursor": None},
                        {"thread": self.thread("thread-a", status)},
                    ]
                )
                with self.assertRaises(codex.InventoryError):
                    codex.loaded_thread_rows(rpc, observed_at=100.0, hostname="r16")

    def test_thread_read_error_is_refused_without_error_body(self) -> None:
        rpc = FakeRpc(
            [
                {"data": ["thread-a"], "nextCursor": None},
                codex.InventoryError("private remote body"),
            ]
        )
        with self.assertRaisesRegex(codex.InventoryError, "thread read refused") as caught:
            codex.loaded_thread_rows(rpc, observed_at=100.0, hostname="r16")
        self.assertNotIn("private remote body", str(caught.exception))

    def test_daemon_version_requires_exact_supported_matching_versions(self) -> None:
        value = codex.daemon_version(
            {
                "status": "running",
                "managedCodexPath": "/opt/codex",
                "managedCodexVersion": "0.152.0",
                "socketPath": "/tmp/codex.sock",
                "cliVersion": "0.152.0",
                "appServerVersion": "0.152.0",
            }
        )
        self.assertEqual(value.managed_path, Path("/opt/codex"))
        for key, replacement in (
            ("status", "notRunning"),
            ("appServerVersion", "0.151.0"),
            ("managedCodexVersion", "0.153.0"),
        ):
            raw = {
                "status": "running",
                "managedCodexPath": "/opt/codex",
                "managedCodexVersion": "0.152.0",
                "socketPath": "/tmp/codex.sock",
                "cliVersion": "0.152.0",
                "appServerVersion": "0.152.0",
            }
            raw[key] = replacement
            with self.subTest(key=key), self.assertRaises(codex.InventoryError):
                codex.daemon_version(raw)

    def test_socket_listener_is_bound_to_daemon_executable_and_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "codex"
            managed.write_bytes(b"binary")
            managed.chmod(0o755)
            socket_path = root / "codex.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen()
            socket_path.chmod(0o600)
            proc = root / "proc"
            process_dir = proc / "42"
            (process_dir / "fd").mkdir(parents=True)
            (proc / "net").mkdir()
            (process_dir / "exe").symlink_to(managed)
            (process_dir / "fd" / "7").symlink_to("socket:[1234]")
            (proc / "net" / "unix").write_text(
                "Num RefCount Protocol Flags Type St Inode Path\n"
                f"000: 00000002 00000000 00010000 0001 01 1234 {socket_path}\n"
            )
            process = codex.ProcessRecord(
                pid=42,
                uid=os.getuid(),
                start_time="start",
                arguments=("codex", "app-server", "--listen", "unix://"),
                directory=process_dir,
            )
            version = codex.DaemonVersion(
                managed_path=managed,
                socket_path=socket_path,
                version="0.152.0",
            )
            try:
                identity = codex.capture_daemon_identity(version, process, proc_root=proc)
            finally:
                listener.close()
            self.assertEqual(identity.pid, 42)
            self.assertEqual(identity.socket_kernel_inode, 1234)
            self.assertEqual(identity.socket_mode, 0o600)

    def test_partial_proxy_without_listener_is_refused(self) -> None:
        proxy = self.process(11, ("codex", "app-server", "proxy"))
        with self.assertRaisesRegex(codex.InventoryError, "partial"):
            codex.observe_codex(
                (proxy,),
                observed_at=100.0,
                proc_root=Path("/proc"),
            )

    def test_multiple_listeners_are_refused(self) -> None:
        listeners = tuple(
            self.process(pid, ("codex", "app-server", "--listen", "unix://"))
            for pid in (10, 11)
        )
        with self.assertRaisesRegex(codex.InventoryError, "not unique"):
            codex.observe_codex(listeners, observed_at=100.0, proc_root=Path("/proc"))

    def test_daemon_identity_drift_is_refused(self) -> None:
        listener = self.process(10, ("codex", "app-server", "--listen", "unix://"))
        version = codex.DaemonVersion(Path("/opt/codex"), Path("/tmp/codex.sock"), "0.152.0")
        first = codex.DaemonIdentity(10, "100", 1, 2, 3, 4, 0o600, os.getuid(), 123)
        second = codex.DaemonIdentity(10, "changed", 1, 2, 3, 4, 0o600, os.getuid(), 123)
        with mock.patch.object(codex, "read_daemon_version", return_value=version), mock.patch.object(
            codex,
            "capture_daemon_identity",
            side_effect=[first, second],
        ), mock.patch.object(
            codex,
            "query_loaded_threads",
            return_value=(),
        ), mock.patch.object(
            codex,
            "scan_processes",
            return_value=(listener,),
        ):
            with self.assertRaisesRegex(codex.InventoryError, "identity changed"):
                codex.observe_codex(
                    (listener,),
                    observed_at=100.0,
                    proc_root=Path("/proc"),
                )

    def test_verified_listener_and_proxy_are_covered_by_pid_and_start_time(self) -> None:
        listener = self.process(10, ("codex", "app-server", "--listen", "unix://"))
        proxy = self.process(11, ("codex", "app-server", "proxy"))
        version = codex.DaemonVersion(Path("/opt/codex"), Path("/tmp/codex.sock"), "0.152.0")
        identity = codex.DaemonIdentity(10, "100", 1, 2, 3, 4, 0o600, os.getuid(), 123)
        with mock.patch.object(codex, "read_daemon_version", return_value=version), mock.patch.object(
            codex,
            "capture_daemon_identity",
            return_value=identity,
        ), mock.patch.object(
            codex,
            "query_loaded_threads",
            return_value=({"id": "thread-a", "status": "running"},),
        ), mock.patch.object(
            codex,
            "scan_processes",
            return_value=(listener, proxy),
        ), mock.patch.object(
            codex,
            "_proxy_covered",
            return_value=True,
        ):
            observed = codex.observe_codex(
                (listener, proxy),
                observed_at=100.0,
                proc_root=Path("/proc"),
            )
        self.assertEqual(observed.processes, (listener, proxy))
        self.assertEqual(observed.covered_processes, {(10, "100"), (11, "110")})
        self.assertEqual(observed.sessions[0]["id"], "thread-a")

    def test_stdlib_websocket_uses_observed_metadata_only_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            socket_path = Path(temporary) / "codex.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen()
            received: list[dict] = []
            errors: list[BaseException] = []

            def read_exact(connection, size):
                result = b""
                while len(result) < size:
                    block = connection.recv(size - len(result))
                    if not block:
                        raise RuntimeError("client closed")
                    result += block
                return result

            def read_frame(connection):
                header = read_exact(connection, 2)
                length = header[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(read_exact(connection, 2), "big")
                elif length == 127:
                    length = int.from_bytes(read_exact(connection, 8), "big")
                mask = read_exact(connection, 4)
                payload = read_exact(connection, length)
                return bytes(value ^ mask[index % 4] for index, value in enumerate(payload))

            def send_frame(connection, value):
                payload = json.dumps(value, separators=(",", ":")).encode()
                if len(payload) < 126:
                    header = bytes((0x81, len(payload)))
                else:
                    header = bytes((0x81, 126)) + len(payload).to_bytes(2, "big")
                connection.sendall(header + payload)

            def serve():
                try:
                    connection, _ = server.accept()
                    with connection:
                        request = b""
                        while b"\r\n\r\n" not in request:
                            request += connection.recv(4096)
                        headers = {}
                        for line in request.decode().split("\r\n")[1:]:
                            name, separator, value = line.partition(":")
                            if separator:
                                headers[name.lower()] = value.strip()
                        accept = __import__("base64").b64encode(
                            __import__("hashlib").sha1(
                                (
                                    headers["sec-websocket-key"]
                                    + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                                ).encode()
                            ).digest()
                        ).decode()
                        connection.sendall(
                            (
                                "HTTP/1.1 101 Switching Protocols\r\n"
                                "Upgrade: websocket\r\n"
                                "Connection: Upgrade\r\n"
                                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                            ).encode()
                        )
                        received.append(json.loads(read_frame(connection)))
                        send_frame(
                            connection,
                            {
                                "id": 1,
                                "result": {
                                    "codexHome": "/home/clp/.codex",
                                    "platformFamily": "unix",
                                    "platformOs": "linux",
                                    "userAgent": "Codex Desktop/0.152.0 (inventory; 1)",
                                },
                            },
                        )
                        received.append(json.loads(read_frame(connection)))
                        received.append(json.loads(read_frame(connection)))
                        send_frame(
                            connection,
                            {
                                "method": "account/updated",
                                "params": {"preview": "private notification body"},
                                "emittedAtMs": 100,
                            },
                        )
                        send_frame(
                            connection,
                            {"id": 2, "result": {"data": [], "nextCursor": None}},
                        )
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                rows = codex.query_loaded_threads(
                    codex.DaemonVersion(Path("/opt/codex"), socket_path, "0.152.0"),
                    observed_at=100.0,
                    hostname="r16",
                )
            finally:
                thread.join(timeout=3)
                server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(rows, ())
            self.assertEqual(received[0]["method"], "initialize")
            self.assertEqual(received[1], {"method": "initialized", "params": {}})
            self.assertEqual(
                received[2],
                {
                    "id": 2,
                    "method": "thread/loaded/list",
                    "params": {"cursor": None, "limit": 1000},
                },
            )

    def test_notification_with_nonobject_params_is_refused_without_content(self) -> None:
        rpc = object.__new__(codex.WebSocketRpc)
        rpc.next_id = 2
        rpc._send_frame = mock.Mock()
        rpc._message = mock.Mock(
            return_value={
                "method": "account/updated",
                "params": "private notification body",
                "emittedAtMs": 100,
            }
        )
        with self.assertRaisesRegex(codex.InventoryError, "notification is malformed") as caught:
            rpc.request("thread/loaded/list", {"cursor": None, "limit": 1000})
        self.assertNotIn("private notification body", str(caught.exception))

    @staticmethod
    def thread(thread_id: str, status: str, *, preview: str = "", activeFlags=None):
        status_value = {"type": status}
        if status == "active":
            status_value["activeFlags"] = [] if activeFlags is None else activeFlags
        return {
            "cliVersion": "0.152.0",
            "createdAt": 1,
            "cwd": "/work",
            "ephemeral": False,
            "id": thread_id,
            "modelProvider": "openai",
            "preview": preview,
            "projectId": None,
            "sessionId": thread_id,
            "source": "cli",
            "status": status_value,
            "turns": [],
            "updatedAt": 2,
        }


if __name__ == "__main__":
    unittest.main()
