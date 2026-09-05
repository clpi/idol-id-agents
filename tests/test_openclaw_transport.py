from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "openclaw_transport",
    ROOT / "scripts" / "openclaw_transport.py",
)
assert SPEC and SPEC.loader
transport = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transport
SPEC.loader.exec_module(transport)


class OpenClawTransportTests(unittest.TestCase):
    port = 18789

    def fixture(
        self,
        root: Path,
        *,
        arguments: tuple[str, ...] = ("openclaw-gateway",),
        ipv6: bool = True,
        ipv4_address: str = "0100007F",
        ipv6_address: str = "00000000000000000000000001000000",
        second_owner: bool = False,
    ) -> Path:
        proc = root / "proc"
        (proc / "net").mkdir(parents=True)
        uid = os.getuid()
        ipv4_inode = 41001
        ipv6_inode = 41002
        self.process(proc, 42, "100", arguments, (ipv4_inode,))
        if ipv6:
            if second_owner:
                self.process(proc, 43, "200", ("openclaw-gateway",), (ipv6_inode,))
            else:
                (proc / "42" / "fd" / "9").symlink_to(f"socket:[{ipv6_inode}]")
        tcp = self.listener_line(ipv4_address, ipv4_inode, uid)
        (proc / "net" / "tcp").write_text("header\n" + tcp)
        tcp6 = self.listener_line(ipv6_address, ipv6_inode, uid) if ipv6 else ""
        (proc / "net" / "tcp6").write_text("header\n" + tcp6)
        return proc

    def process(
        self,
        proc: Path,
        pid: int,
        start_time: str,
        arguments: tuple[str, ...],
        sockets: tuple[int, ...],
    ) -> None:
        directory = proc / str(pid)
        (directory / "fd").mkdir(parents=True)
        executable = proc.parent / f"node-{pid}"
        executable.write_bytes(b"node")
        executable.chmod(0o755)
        (directory / "exe").symlink_to(executable)
        for index, inode in enumerate(sockets, 7):
            (directory / "fd" / str(index)).symlink_to(f"socket:[{inode}]")
        stat_row = "1 (openclaw-gateway) S " + " ".join(["0"] * 18 + [start_time])
        (directory / "stat").write_text(stat_row)
        (directory / "cmdline").write_bytes(b"\0".join(part.encode() for part in arguments) + b"\0")

    def listener_line(self, address: str, inode: int, uid: int) -> str:
        return (
            f"0: {address}:{self.port:04X} 00000000:0000 0A "
            f"00000000:00000000 00:00000000 00000000 {uid} 0 {inode} 1\n"
        )

    def test_dual_loopback_listener_has_one_immutable_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary))
            observed = transport.observe_local_gateway(proc_root=proc)

        self.assertEqual(observed.pid, 42)
        self.assertEqual(observed.start_time, "100")
        self.assertEqual(observed.arguments, ("openclaw-gateway",))
        self.assertEqual(
            [(endpoint.family, endpoint.address, endpoint.kernel_inode) for endpoint in observed.endpoints],
            [("ipv4", "127.0.0.1", 41001), ("ipv6", "::1", 41002)],
        )
        self.assertGreater(observed.executable_inode, 0)

    def test_ipv4_loopback_is_required_and_ipv6_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            observed = transport.observe_local_gateway(proc_root=proc)
        self.assertEqual(tuple(endpoint.family for endpoint in observed.endpoints), ("ipv4",))
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary))
            (proc / "net" / "tcp").write_text("header\n")
            with self.assertRaisesRegex(transport.TransportRefusal, "unique"):
                transport.observe_local_gateway(proc_root=proc)

    def test_non_loopback_or_duplicate_family_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv4_address="00000000")
            with self.assertRaisesRegex(transport.TransportRefusal, "loopback"):
                transport.observe_local_gateway(proc_root=proc)
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            line = self.listener_line("0100007F", 41003, os.getuid())
            with (proc / "net" / "tcp").open("a") as output:
                output.write(line)
            with self.assertRaisesRegex(transport.TransportRefusal, "unique"):
                transport.observe_local_gateway(proc_root=proc)

    def test_dual_stack_listeners_must_have_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), second_owner=True)
            with self.assertRaisesRegex(transport.TransportRefusal, "unique owner"):
                transport.observe_local_gateway(proc_root=proc)

    def test_listener_uid_must_match_controller_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            line = self.listener_line("0100007F", 41001, os.getuid() + 1)
            (proc / "net" / "tcp").write_text("header\n" + line)
            with self.assertRaisesRegex(transport.TransportRefusal, "ownership"):
                transport.observe_local_gateway(proc_root=proc)

    def test_gateway_title_is_exact_and_error_does_not_echo_arguments(self) -> None:
        secret = "private-token"
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(
                Path(temporary),
                arguments=("openclaw-gateway", "--token", secret),
                ipv6=False,
            )
            with self.assertRaisesRegex(transport.TransportRefusal, "unique owner") as caught:
                transport.observe_local_gateway(proc_root=proc)
        self.assertNotIn(secret, str(caught.exception))

    def test_pid_reuse_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            original = transport.scan_processes(proc)
            replacement = transport.ProcessRecord(
                pid=42,
                uid=os.getuid(),
                start_time="101",
                arguments=("openclaw-gateway",),
                directory=proc / "42",
            )
            with mock.patch.object(
                transport,
                "scan_processes",
                side_effect=[original, (replacement,)],
            ):
                with self.assertRaisesRegex(transport.TransportRefusal, "changed"):
                    transport.observe_local_gateway(proc_root=proc)

    def test_listener_churn_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            original = transport._listener_rows
            calls = 0

            def changed(root: Path, port: int):
                nonlocal calls
                calls += 1
                rows = original(root, port)
                if calls == 2:
                    endpoint = rows[0]
                    return (
                        transport.LoopbackEndpoint(
                            endpoint.family,
                            endpoint.address,
                            endpoint.port,
                            endpoint.uid,
                            endpoint.kernel_inode + 1,
                        ),
                    )
                return rows

            with mock.patch.object(transport, "_listener_rows", side_effect=changed):
                with self.assertRaises(transport.TransportRefusal):
                    transport.observe_local_gateway(proc_root=proc)

    def test_missing_ipv6_table_is_an_optional_empty_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            (proc / "net" / "tcp6").unlink()
            observed = transport.observe_local_gateway(proc_root=proc)
        self.assertEqual(tuple(endpoint.family for endpoint in observed.endpoints), ("ipv4",))

    def test_missing_ipv4_listener_table_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = self.fixture(Path(temporary), ipv6=False)
            (proc / "net" / "tcp").unlink()
            with self.assertRaisesRegex(transport.TransportRefusal, "unavailable"):
                transport.observe_local_gateway(proc_root=proc)

    def test_empty_listener_table_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary) / "proc"
            (proc / "net").mkdir(parents=True)
            (proc / "net" / "tcp").write_text("header\n")
            with self.assertRaises(transport.TransportRefusal):
                transport.observe_local_gateway(proc_root=proc)


if __name__ == "__main__":
    unittest.main()
