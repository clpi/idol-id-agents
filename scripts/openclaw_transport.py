#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

try:
    from scripts.codex_inventory import InventoryError, ProcessRecord, scan_processes
except ModuleNotFoundError:  # Direct execution from scripts/.
    from codex_inventory import InventoryError, ProcessRecord, scan_processes


DEFAULT_GATEWAY_PORT = 18789
_IPV4_LOOPBACK = "0100007F"
_IPV6_LOOPBACK = "00000000000000000000000001000000"
_SOCKET = re.compile(r"socket:\[(\d+)\]")


class TransportRefusal(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoopbackEndpoint:
    family: str
    address: str
    port: int
    uid: int
    kernel_inode: int


@dataclass(frozen=True, slots=True)
class OpenClawTransportObservation:
    pid: int
    start_time: str
    uid: int
    executable_device: int
    executable_inode: int
    arguments: tuple[str, ...]
    endpoints: tuple[LoopbackEndpoint, ...]


def _listener_rows(proc_root: Path, port: int) -> tuple[LoopbackEndpoint, ...]:
    if port < 1 or port > 65535:
        raise TransportRefusal("OpenClaw gateway port is invalid")
    expected_port = f"{port:04X}"
    endpoints: list[LoopbackEndpoint] = []
    tables = (
        ("tcp", "ipv4", _IPV4_LOOPBACK, "127.0.0.1"),
        ("tcp6", "ipv6", _IPV6_LOOPBACK, "::1"),
    )
    for table, family, expected_address, address in tables:
        try:
            lines = (proc_root / "net" / table).read_text(encoding="utf-8").splitlines()[1:]
        except OSError as exc:
            raise TransportRefusal("OpenClaw listener table is unavailable") from exc
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                raise TransportRefusal("OpenClaw listener table is malformed")
            local_address, separator, local_port = fields[1].rpartition(":")
            if not separator or local_port.upper() != expected_port or fields[3] != "0A":
                continue
            if local_address.upper() != expected_address:
                raise TransportRefusal("OpenClaw gateway is not loopback-bound")
            try:
                uid = int(fields[7])
                inode = int(fields[9])
            except ValueError as exc:
                raise TransportRefusal("OpenClaw listener identity is malformed") from exc
            if inode <= 0:
                raise TransportRefusal("OpenClaw listener identity is malformed")
            endpoints.append(LoopbackEndpoint(family, address, port, uid, inode))
    ipv4 = tuple(endpoint for endpoint in endpoints if endpoint.family == "ipv4")
    ipv6 = tuple(endpoint for endpoint in endpoints if endpoint.family == "ipv6")
    if len(ipv4) != 1 or len(ipv6) > 1:
        raise TransportRefusal("OpenClaw gateway has no unique loopback listener set")
    return tuple(sorted(endpoints, key=lambda endpoint: endpoint.family))


def _socket_inodes(process: ProcessRecord) -> frozenset[int]:
    try:
        descriptors = tuple((process.directory / "fd").iterdir())
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise TransportRefusal("OpenClaw process descriptors are unavailable") from exc
    result: set[int] = set()
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TransportRefusal("OpenClaw process descriptors are unavailable") from exc
        match = _SOCKET.fullmatch(target)
        if match:
            result.add(int(match.group(1)))
    return frozenset(result)


def _executable_identity(process: ProcessRecord) -> tuple[int, int]:
    try:
        value = (process.directory / "exe").stat()
    except OSError as exc:
        raise TransportRefusal("OpenClaw gateway executable is unavailable") from exc
    if not stat.S_ISREG(value.st_mode):
        raise TransportRefusal("OpenClaw gateway executable is not regular")
    return value.st_dev, value.st_ino


def _capture_once(
    proc_root: Path,
    port: int,
    processes: tuple[ProcessRecord, ...],
) -> OpenClawTransportObservation:
    endpoints = _listener_rows(proc_root, port)
    current_uid = os.getuid()
    if any(endpoint.uid != current_uid for endpoint in endpoints):
        raise TransportRefusal("OpenClaw gateway listener ownership is invalid")
    listener_inodes = {endpoint.kernel_inode for endpoint in endpoints}
    candidates = tuple(
        process
        for process in processes
        if process.uid == current_uid and process.arguments == ("openclaw-gateway",)
    )
    if len(candidates) != 1:
        raise TransportRefusal("OpenClaw gateway listener has no unique owner")
    owner = candidates[0]
    owned_inodes = _socket_inodes(owner)
    if not listener_inodes.issubset(owned_inodes):
        raise TransportRefusal("OpenClaw gateway listener set is split or incomplete")
    executable_device, executable_inode = _executable_identity(owner)
    return OpenClawTransportObservation(
        pid=owner.pid,
        start_time=owner.start_time,
        uid=owner.uid,
        executable_device=executable_device,
        executable_inode=executable_inode,
        arguments=owner.arguments,
        endpoints=endpoints,
    )


def observe_local_gateway(
    port: int = DEFAULT_GATEWAY_PORT,
    *,
    proc_root: Path = Path("/proc"),
) -> OpenClawTransportObservation:
    try:
        before_processes = scan_processes(proc_root)
        before = _capture_once(proc_root, port, before_processes)
        after_processes = scan_processes(proc_root)
        after = _capture_once(proc_root, port, after_processes)
    except InventoryError as exc:
        raise TransportRefusal("OpenClaw process inventory is unavailable") from exc
    if before != after:
        raise TransportRefusal("OpenClaw gateway transport changed during observation")
    return before
