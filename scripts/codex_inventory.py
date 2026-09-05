#!/usr/bin/env python3
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
from typing import Any, Mapping, Protocol, Sequence


SUPPORTED_VERSION = "0.152.0"
MAX_FRAME_BYTES = 1_000_000
MAX_FRAMES = 4096
MAX_LOADED_PAGES = 50
MAX_LOADED_THREADS = 2000
LOADED_PAGE_SIZE = 1000
RPC_TIMEOUT_SECONDS = 5.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class InventoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    uid: int
    start_time: str
    arguments: tuple[str, ...]
    directory: Path

    @property
    def identity(self) -> tuple[int, str]:
        return self.pid, self.start_time


@dataclass(frozen=True, slots=True)
class DaemonVersion:
    managed_path: Path
    socket_path: Path
    version: str


@dataclass(frozen=True, slots=True)
class DaemonIdentity:
    pid: int
    start_time: str
    executable_device: int
    executable_inode: int
    socket_device: int
    socket_inode: int
    socket_mode: int
    socket_uid: int
    socket_kernel_inode: int


@dataclass(frozen=True, slots=True)
class CodexObservation:
    processes: tuple[ProcessRecord, ...]
    covered_processes: frozenset[tuple[int, str]]
    sessions: tuple[Mapping[str, Any], ...]


class Rpc(Protocol):
    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _start_time(value: str) -> str:
    end = value.rfind(")")
    if end < 0:
        raise InventoryError("process scan returned malformed stat data")
    fields = value[end + 2 :].split()
    if len(fields) <= 19 or not fields[19].isdigit():
        raise InventoryError("process scan returned malformed stat data")
    return fields[19]


def scan_processes(proc_root: Path = Path("/proc")) -> tuple[ProcessRecord, ...]:
    if not proc_root.is_dir():
        return ()
    current_uid = os.getuid()
    records: list[ProcessRecord] = []
    try:
        directories = tuple(proc_root.iterdir())
    except OSError as exc:
        raise InventoryError("process scan is unavailable") from exc
    for directory in sorted(directories, key=lambda item: item.name):
        if not directory.name.isdigit():
            continue
        uid: int | None = None
        try:
            uid = directory.stat().st_uid
            before = _start_time((directory / "stat").read_text(encoding="utf-8"))
            arguments = tuple(
                part.decode("utf-8", "replace")
                for part in (directory / "cmdline").read_bytes().split(b"\0")
                if part
            )
            after = _start_time((directory / "stat").read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except OSError as exc:
            if uid is None or uid == current_uid:
                raise InventoryError("same-user process scan is incomplete") from exc
            continue
        if before != after:
            raise InventoryError("process identity changed during scan")
        if arguments:
            records.append(
                ProcessRecord(
                    pid=int(directory.name),
                    uid=uid,
                    start_time=after,
                    arguments=arguments,
                    directory=directory,
                )
            )
    return tuple(records)


def _codex_subcommand(arguments: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    if not arguments or Path(arguments[0]).name != "codex":
        return None, ()
    index = 1
    while index < len(arguments):
        token = arguments[index]
        if token in {"-c", "--config"}:
            if index + 1 >= len(arguments):
                return "unsupported", ()
            index += 2
            continue
        if token.startswith("--config="):
            index += 1
            continue
        if token.startswith("-"):
            return "unsupported" if "app-server" in arguments[index + 1 :] else "worker", ()
        return token, tuple(arguments[index + 1 :])
    return "worker", ()


def control_process_kind(process: ProcessRecord) -> str:
    command, arguments = _codex_subcommand(process.arguments)
    if command != "app-server":
        return "worker" if command is not None else "other"
    if arguments in {
        ("--listen", "unix://"),
        ("--remote-control", "--listen", "unix://"),
    }:
        return "listener"
    if arguments == ("proxy",):
        return "proxy"
    if len(arguments) == 3 and arguments[:2] == ("proxy", "--sock"):
        return "proxy"
    if "--listen" in arguments or (arguments and arguments[0] == "proxy"):
        return "unsupported"
    return "worker"


def daemon_version(raw: Mapping[str, Any]) -> DaemonVersion:
    required = {
        "status",
        "managedCodexPath",
        "managedCodexVersion",
        "socketPath",
        "cliVersion",
        "appServerVersion",
    }
    if set(raw) != required or raw.get("status") != "running":
        raise InventoryError("Codex daemon metadata is unsupported")
    versions = tuple(raw.get(key) for key in ("managedCodexVersion", "cliVersion", "appServerVersion"))
    if versions != (SUPPORTED_VERSION,) * 3:
        raise InventoryError("Codex daemon version is unsupported")
    managed_path = Path(str(raw.get("managedCodexPath", "")))
    socket_path = Path(str(raw.get("socketPath", "")))
    if not managed_path.is_absolute() or not socket_path.is_absolute():
        raise InventoryError("Codex daemon paths are invalid")
    return DaemonVersion(managed_path, socket_path, SUPPORTED_VERSION)


def read_daemon_version() -> DaemonVersion:
    executable = shutil.which("codex")
    if not executable:
        raise InventoryError("Codex CLI is unavailable")
    try:
        result = subprocess.run(
            [executable, "app-server", "daemon", "version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryError("Codex daemon metadata query failed") from exc
    if result.returncode != 0 or len(result.stdout) > 64_000:
        raise InventoryError("Codex daemon metadata query failed")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InventoryError("Codex daemon metadata is malformed") from exc
    if not isinstance(raw, Mapping):
        raise InventoryError("Codex daemon metadata is malformed")
    return daemon_version(raw)


def _listening_socket_inode(proc_root: Path, socket_path: Path) -> int:
    try:
        lines = (proc_root / "net" / "unix").read_text(encoding="utf-8").splitlines()[1:]
    except OSError as exc:
        raise InventoryError("Unix listener table is unavailable") from exc
    matches: list[int] = []
    for line in lines:
        fields = line.split(maxsplit=7)
        if len(fields) != 8 or fields[7] != str(socket_path):
            continue
        if fields[3:6] == ["00010000", "0001", "01"] and fields[6].isdigit():
            matches.append(int(fields[6]))
    if len(matches) != 1:
        raise InventoryError("Codex control socket has no unique listener")
    return matches[0]


def _process_socket_inodes(directory: Path) -> frozenset[int]:
    result: set[int] = set()
    try:
        entries = tuple((directory / "fd").iterdir())
    except OSError as exc:
        raise InventoryError("Codex listener descriptors are unavailable") from exc
    for entry in entries:
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InventoryError("Codex listener descriptors are unavailable") from exc
        match = re.fullmatch(r"socket:\[(\d+)\]", target)
        if match:
            result.add(int(match.group(1)))
    return frozenset(result)


def _stat_regular(path: Path, label: str) -> os.stat_result:
    try:
        value = path.stat()
    except OSError as exc:
        raise InventoryError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(value.st_mode):
        raise InventoryError(f"{label} is not regular")
    return value
def capture_daemon_identity(
    version: DaemonVersion,
    process: ProcessRecord,
    *,
    proc_root: Path = Path("/proc"),
) -> DaemonIdentity:
    current_uid = os.getuid()
    if process.uid != current_uid or control_process_kind(process) != "listener":
        raise InventoryError("Codex listener ownership is invalid")
    managed = _stat_regular(version.managed_path, "managed Codex executable")
    executable = _stat_regular(process.directory / "exe", "running Codex executable")
    if managed.st_uid != current_uid or (managed.st_dev, managed.st_ino) != (
        executable.st_dev,
        executable.st_ino,
    ):
        raise InventoryError("running Codex executable is unbound")
    try:
        socket_value = version.socket_path.lstat()
    except OSError as exc:
        raise InventoryError("Codex control socket is unavailable") from exc
    socket_mode = stat.S_IMODE(socket_value.st_mode)
    if (
        not stat.S_ISSOCK(socket_value.st_mode)
        or socket_value.st_uid != current_uid
        or socket_mode != 0o600
    ):
        raise InventoryError("Codex control socket ownership is invalid")
    kernel_inode = _listening_socket_inode(proc_root, version.socket_path)
    if kernel_inode not in _process_socket_inodes(process.directory):
        raise InventoryError("Codex control socket listener is unbound")
    return DaemonIdentity(
        pid=process.pid,
        start_time=process.start_time,
        executable_device=executable.st_dev,
        executable_inode=executable.st_ino,
        socket_device=socket_value.st_dev,
        socket_inode=socket_value.st_ino,
        socket_mode=socket_mode,
        socket_uid=socket_value.st_uid,
        socket_kernel_inode=kernel_inode,
    )


def _proxy_covered(process: ProcessRecord, version: DaemonVersion, identity: DaemonIdentity) -> bool:
    if process.uid != os.getuid() or control_process_kind(process) != "proxy":
        return False
    command, arguments = _codex_subcommand(process.arguments)
    if command != "app-server":
        return False
    if arguments != ("proxy",) and arguments != ("proxy", "--sock", str(version.socket_path)):
        return False
    executable = _stat_regular(process.directory / "exe", "running Codex proxy executable")
    return (executable.st_dev, executable.st_ino) == (
        identity.executable_device,
        identity.executable_inode,
    )


class WebSocketRpc:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = RPC_TIMEOUT_SECONDS) -> None:
        self.deadline = time.monotonic() + timeout_seconds
        self.frames = 0
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._set_timeout()
            self.socket.connect(str(socket_path))
            self._handshake()
            result = self.request(
                "initialize",
                {
                    "capabilities": {"experimentalApi": False, "optOutNotificationMethods": []},
                    "clientInfo": {"name": "idol-fleet-inventory-audit", "version": "1"},
                },
                request_id=1,
            )
            self._validate_initialize(result)
            self.notify("initialized", {})
            self.next_id = 2
        except Exception:
            self.socket.close()
            raise

    def _set_timeout(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise InventoryError("Codex daemon RPC timed out")
        self.socket.settimeout(remaining)

    def _read_exact(self, size: int) -> bytes:
        blocks: list[bytes] = []
        remaining = size
        while remaining:
            self._set_timeout()
            try:
                block = self.socket.recv(remaining)
            except (OSError, TimeoutError) as exc:
                raise InventoryError("Codex daemon RPC read failed") from exc
            if not block:
                raise InventoryError("Codex daemon RPC closed early")
            blocks.append(block)
            remaining -= len(block)
        return b"".join(blocks)

    def _handshake(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.socket.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            if len(response) >= 16_384:
                raise InventoryError("Codex daemon WebSocket handshake is oversized")
            response.extend(self._read_exact(1))
        lines = response.decode("ascii", "strict").split("\r\n")
        if lines[0] != "HTTP/1.1 101 Switching Protocols":
            raise InventoryError("Codex daemon WebSocket handshake was refused")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise InventoryError("Codex daemon WebSocket handshake is malformed")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if (
            headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in headers.get("connection", "").lower().split(",")
            or headers.get("sec-websocket-accept") != expected
        ):
            raise InventoryError("Codex daemon WebSocket handshake is invalid")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_FRAME_BYTES:
            raise InventoryError("Codex daemon RPC request is oversized")
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + length.to_bytes(8, "big")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        try:
            self._set_timeout()
            self.socket.sendall(header + mask + masked)
        except (OSError, TimeoutError) as exc:
            raise InventoryError("Codex daemon RPC write failed") from exc

    def _read_frame(self) -> tuple[int, bytes]:
        self.frames += 1
        if self.frames > MAX_FRAMES:
            raise InventoryError("Codex daemon RPC frame limit exceeded")
        header = self._read_exact(2)
        final = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if masked or not final or header[0] & 0x70:
            raise InventoryError("Codex daemon RPC frame is unsupported")
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        if length > MAX_FRAME_BYTES:
            raise InventoryError("Codex daemon RPC frame is oversized")
        if opcode >= 0x8 and length > 125:
            raise InventoryError("Codex daemon control frame is oversized")
        return opcode, self._read_exact(length)

    def _message(self) -> Mapping[str, Any]:
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise InventoryError("Codex daemon RPC closed early")
            if opcode != 0x1:
                raise InventoryError("Codex daemon RPC frame type is unsupported")
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InventoryError("Codex daemon RPC response is malformed") from exc
            if not isinstance(value, Mapping):
                raise InventoryError("Codex daemon RPC response is malformed")
            return value

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        request_id: int | None = None,
    ) -> Mapping[str, Any]:
        selected_id = self.next_id if request_id is None else request_id
        if request_id is None:
            self.next_id += 1
        payload = json.dumps(
            {"id": selected_id, "method": method, "params": params},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_frame(0x1, payload)
        while True:
            response = self._message()
            if "id" not in response:
                keys = set(response)
                if (
                    keys not in ({"method", "params"}, {"method", "params", "emittedAtMs"})
                    or not isinstance(response.get("method"), str)
                    or not isinstance(response.get("params"), Mapping)
                    or (
                        "emittedAtMs" in response
                        and type(response["emittedAtMs"]) is not int
                    )
                ):
                    raise InventoryError("Codex daemon notification is malformed")
                continue
            if (
                type(response.get("id")) is not int
                or response["id"] != selected_id
                or set(response) != {"id", "result"}
            ):
                raise InventoryError("Codex daemon RPC response is invalid")
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise InventoryError("Codex daemon RPC result is invalid")
            return result

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        payload = json.dumps(
            {"method": method, "params": params},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_frame(0x1, payload)

    @staticmethod
    def _validate_initialize(result: Mapping[str, Any]) -> None:
        required = {"codexHome", "platformFamily", "platformOs", "userAgent"}
        if not required.issubset(result) or result.get("platformFamily") != "unix":
            raise InventoryError("Codex daemon initialize result is unsupported")
        if any(
            not isinstance(result.get(key), str)
            or not result[key]
            or len(result[key]) > 4096
            for key in required
        ):
            raise InventoryError("Codex daemon initialize result is malformed")
        if SUPPORTED_VERSION not in result["userAgent"]:
            raise InventoryError("Codex daemon initialize version is unsupported")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except InventoryError:
            pass
        self.socket.close()

    def __enter__(self) -> "WebSocketRpc":
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()


def _loaded_ids(rpc: Rpc) -> tuple[str, ...]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    result: list[str] = []
    for _ in range(MAX_LOADED_PAGES):
        try:
            page = rpc.request("thread/loaded/list", {"cursor": cursor, "limit": LOADED_PAGE_SIZE})
        except Exception as exc:
            raise InventoryError("Codex loaded-thread query refused") from exc
        if (
            set(page) - {"data", "nextCursor"}
            or not isinstance(page.get("data"), list)
            or len(page["data"]) > LOADED_PAGE_SIZE
        ):
            raise InventoryError("Codex loaded-thread response is malformed")
        for value in page["data"]:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 240
                or not _SESSION_ID.fullmatch(value)
                or value in seen_ids
            ):
                raise InventoryError("Codex loaded-thread response is malformed")
            seen_ids.add(value)
            result.append(value)
            if len(result) > MAX_LOADED_THREADS:
                raise InventoryError("Codex loaded-thread limit exceeded")
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            return tuple(result)
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > 4096
            or any(ord(char) < 32 for char in next_cursor)
            or next_cursor in seen_cursors
        ):
            raise InventoryError("Codex loaded-thread pagination is invalid")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise InventoryError("Codex loaded-thread pagination limit exceeded")


def loaded_thread_rows(
    rpc: Rpc,
    *,
    observed_at: float,
    hostname: str,
) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = []
    required = {
        "cliVersion",
        "createdAt",
        "cwd",
        "ephemeral",
        "id",
        "modelProvider",
        "preview",
        "projectId",
        "sessionId",
        "source",
        "status",
        "turns",
        "updatedAt",
    }
    for thread_id in _loaded_ids(rpc):
        try:
            result = rpc.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
        except Exception as exc:
            raise InventoryError("Codex thread read refused") from exc
        if set(result) != {"thread"} or not isinstance(result.get("thread"), Mapping):
            raise InventoryError("Codex thread response is malformed")
        thread = result["thread"]
        if (
            not required.issubset(thread)
            or thread.get("id") != thread_id
            or thread.get("cliVersion") != SUPPORTED_VERSION
            or thread.get("turns") != []
        ):
            raise InventoryError("Codex thread response is malformed")
        status_value = thread.get("status")
        if not isinstance(status_value, Mapping):
            raise InventoryError("Codex thread status is malformed")
        status_type = status_value.get("type")
        if status_type == "idle" and set(status_value) == {"type"}:
            continue
        if status_type == "active":
            flags = status_value.get("activeFlags")
            if (
                set(status_value) != {"type", "activeFlags"}
                or not isinstance(flags, list)
                or any(flag not in {"waitingOnApproval", "waitingOnUserInput"} for flag in flags)
            ):
                raise InventoryError("Codex thread status is malformed")
            rows.append(
                {
                    "id": thread_id,
                    "status": "running",
                    "last_activity": observed_at,
                    "host": hostname,
                    "actor": "codex-cli",
                }
            )
            continue
        raise InventoryError("Codex loaded thread has an unsafe status")
    return tuple(rows)


def query_loaded_threads(
    version: DaemonVersion,
    *,
    observed_at: float,
    hostname: str,
) -> tuple[Mapping[str, Any], ...]:
    try:
        with WebSocketRpc(version.socket_path) as rpc:
            return loaded_thread_rows(rpc, observed_at=observed_at, hostname=hostname)
    except InventoryError:
        raise
    except Exception as exc:
        raise InventoryError("Codex daemon RPC failed") from exc


def observe_codex(
    processes: Sequence[ProcessRecord] | None,
    *,
    observed_at: float,
    proc_root: Path = Path("/proc"),
    hostname: str | None = None,
) -> CodexObservation:
    initial = tuple(scan_processes(proc_root) if processes is None else processes)
    kinds = {process.identity: control_process_kind(process) for process in initial}
    if "unsupported" in kinds.values():
        raise InventoryError("unsupported Codex app-server process found")
    listeners = tuple(process for process in initial if kinds[process.identity] == "listener")
    proxies = tuple(process for process in initial if kinds[process.identity] == "proxy")
    if not listeners:
        if proxies:
            raise InventoryError("partial Codex daemon infrastructure found")
        return CodexObservation(initial, frozenset(), ())
    if len(listeners) != 1:
        raise InventoryError("Codex daemon listener is not unique")

    version = read_daemon_version()
    before = capture_daemon_identity(version, listeners[0], proc_root=proc_root)
    try:
        sessions = query_loaded_threads(
            version,
            observed_at=observed_at,
            hostname=hostname or socket.gethostname(),
        )
    except Exception as exc:
        raise InventoryError("Codex daemon RPC refused") from exc

    final = scan_processes(proc_root)
    final_kinds = {process.identity: control_process_kind(process) for process in final}
    if "unsupported" in final_kinds.values():
        raise InventoryError("unsupported Codex app-server process found")
    final_listeners = tuple(
        process for process in final if final_kinds[process.identity] == "listener"
    )
    if len(final_listeners) != 1:
        raise InventoryError("Codex daemon listener changed during inventory")
    final_version = read_daemon_version()
    after = capture_daemon_identity(final_version, final_listeners[0], proc_root=proc_root)
    if final_version != version or after != before:
        raise InventoryError("Codex daemon identity changed during inventory")

    covered = {final_listeners[0].identity}
    for process in final:
        if final_kinds[process.identity] != "proxy":
            continue
        if not _proxy_covered(process, version, after):
            raise InventoryError("Codex proxy is unbound")
        covered.add(process.identity)
    return CodexObservation(final, frozenset(covered), sessions)
