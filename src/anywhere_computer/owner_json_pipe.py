"""Owner-scoped Windows JSON request/reply transport.

The local agent uses this transport across Windows logon sessions without reading or
distributing credentials.
Clients verify the native server process identity before sending request bytes, and the server
accepts only clients whose process token has the same owner SID.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import json
import math
import os
import re
import secrets
import struct
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

PROTOCOL = "anywhere-owner-json-v1"
DEFAULT_MAX_PAYLOAD_BYTES = 65_536
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_CONNECTIONS = 8
_HEADER = struct.Struct("<I")


class OwnerPipeError(RuntimeError):
    """Base error for the owner-scoped pipe transport."""


class OwnerPipeIdentityError(OwnerPipeError):
    """The native peer identity did not match trusted endpoint metadata."""


class OwnerPipeProtocolError(OwnerPipeError):
    """The peer sent malformed or unsupported framing."""


class OwnerPipeTimeout(OwnerPipeError):
    """A bounded connect, I/O, or dispatch operation timed out."""


class OwnerPipeCleanupError(OwnerPipeError):
    """The native worker did not finish bounded cleanup."""


@dataclass(frozen=True)
class OwnerPipeEndpoint:
    """Non-secret metadata binding a pipe name to one verified server process."""

    pipe_name: str
    server_id: str
    server_pid: int
    server_creation_time: float
    owner_sid: str
    server_executable: str
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    protocol: str = PROTOCOL

    def to_dict(self) -> dict[str, object]:
        return {
            "pipe_name": self.pipe_name,
            "server_id": self.server_id,
            "server_pid": self.server_pid,
            "server_creation_time": self.server_creation_time,
            "owner_sid": self.owner_sid,
            "server_executable": self.server_executable,
            "max_payload_bytes": self.max_payload_bytes,
            "protocol": self.protocol,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> OwnerPipeEndpoint:
        expected = {
            "pipe_name",
            "server_id",
            "server_pid",
            "server_creation_time",
            "owner_sid",
            "server_executable",
            "max_payload_bytes",
            "protocol",
        }
        if set(raw) != expected:
            raise OwnerPipeProtocolError("Invalid owner pipe endpoint fields")
        try:
            endpoint = cls(
                pipe_name=_require_string(raw["pipe_name"]),
                server_id=_require_string(raw["server_id"]),
                server_pid=_require_integer(raw["server_pid"]),
                server_creation_time=_require_number(raw["server_creation_time"]),
                owner_sid=_require_string(raw["owner_sid"]),
                server_executable=_require_string(raw["server_executable"]),
                max_payload_bytes=_require_integer(raw["max_payload_bytes"]),
                protocol=_require_string(raw["protocol"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerPipeProtocolError("Invalid owner pipe endpoint metadata") from error
        endpoint.validate()
        return endpoint

    def validate(self) -> None:
        if self.protocol != PROTOCOL:
            raise OwnerPipeProtocolError("Unsupported owner pipe protocol")
        if not re.fullmatch(r"\\\\\.\\pipe\\anywhere-owner-json-[a-f0-9]{48}", self.pipe_name):
            raise OwnerPipeProtocolError("Invalid owner pipe name")
        if not re.fullmatch(r"[a-f0-9]{64}", self.server_id):
            raise OwnerPipeProtocolError("Invalid owner pipe server identifier")
        if (
            type(self.server_pid) is not int
            or self.server_pid <= 0
            or not isinstance(self.server_creation_time, (int, float))
            or isinstance(self.server_creation_time, bool)
            or not math.isfinite(self.server_creation_time)
            or self.server_creation_time <= 0
        ):
            raise OwnerPipeProtocolError("Invalid owner pipe process identity")
        if not re.fullmatch(r"S-\d+(?:-\d+)+", self.owner_sid):
            raise OwnerPipeProtocolError("Invalid owner pipe SID")
        if (
            "\x00" in self.server_executable
            or len(self.server_executable) > 32_767
            or not Path(self.server_executable).is_absolute()
            or os.path.normpath(self.server_executable) != self.server_executable
        ):
            raise OwnerPipeProtocolError("Invalid owner pipe owner identity")
        if (
            type(self.max_payload_bytes) is not int
            or not 1 <= self.max_payload_bytes <= MAX_PAYLOAD_BYTES
        ):
            raise OwnerPipeProtocolError("Invalid owner pipe payload limit")


def _require_string(value: object) -> str:
    if type(value) is not str:
        raise TypeError("Expected string")
    return value


def _require_integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError("Expected integer")
    return value


def _require_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Expected finite number")
    rendered = float(value)
    if not math.isfinite(rendered):
        raise ValueError("Expected finite number")
    return rendered


def _win_error_code() -> int:
    if sys.platform == "win32":
        return ctypes.get_last_error()
    raise OSError("Windows error codes are unavailable on this platform")


if sys.platform == "win32" or TYPE_CHECKING:
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]


    class _WindowsApi:
        kernel: ctypes.CDLL
        security: ctypes.CDLL

        INVALID_HANDLE = ctypes.c_void_p(-1).value
        ERROR_FILE_NOT_FOUND = 2
        ERROR_BROKEN_PIPE = 109
        ERROR_SEM_TIMEOUT = 121
        ERROR_NO_DATA = 232
        ERROR_PIPE_BUSY = 231
        ERROR_PIPE_NOT_CONNECTED = 233
        ERROR_PIPE_CONNECTED = 535
        ERROR_OPERATION_ABORTED = 995

        def __init__(self) -> None:
            if sys.platform != "win32":
                raise OSError("Windows pipes are unavailable on this platform")
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.security = ctypes.WinDLL("advapi32", use_last_error=True)
            declarations = [
                (self.kernel, "GetCurrentProcess", [], wintypes.HANDLE),
                (self.kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
                (
                    self.kernel,
                    "CreateNamedPipeW",
                    [
                        wintypes.LPCWSTR,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        ctypes.POINTER(_SecurityAttributes),
                    ],
                    wintypes.HANDLE,
                ),
                (
                    self.kernel,
                    "ConnectNamedPipe",
                    [wintypes.HANDLE, wintypes.LPVOID],
                    wintypes.BOOL,
                ),
                (self.kernel, "DisconnectNamedPipe", [wintypes.HANDLE], wintypes.BOOL),
                (
                    self.kernel,
                    "CreateFileW",
                    [
                        wintypes.LPCWSTR,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.LPVOID,
                        wintypes.DWORD,
                        wintypes.DWORD,
                        wintypes.HANDLE,
                    ],
                    wintypes.HANDLE,
                ),
                (self.kernel, "WaitNamedPipeW", [wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
                (
                    self.kernel,
                    "ReadFile",
                    [
                        wintypes.HANDLE,
                        wintypes.LPVOID,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD),
                        wintypes.LPVOID,
                    ],
                    wintypes.BOOL,
                ),
                (
                    self.kernel,
                    "WriteFile",
                    [
                        wintypes.HANDLE,
                        wintypes.LPCVOID,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD),
                        wintypes.LPVOID,
                    ],
                    wintypes.BOOL,
                ),
                (
                    self.kernel,
                    "PeekNamedPipe",
                    [
                        wintypes.HANDLE,
                        wintypes.LPVOID,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD),
                        ctypes.POINTER(wintypes.DWORD),
                        ctypes.POINTER(wintypes.DWORD),
                    ],
                    wintypes.BOOL,
                ),
                (
                    self.kernel,
                    "GetNamedPipeClientProcessId",
                    [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)],
                    wintypes.BOOL,
                ),
                (
                    self.kernel,
                    "GetNamedPipeServerProcessId",
                    [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)],
                    wintypes.BOOL,
                ),
                (
                    self.kernel,
                    "OpenProcess",
                    [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                    wintypes.HANDLE,
                ),
                (
                    self.kernel,
                    "OpenThread",
                    [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                    wintypes.HANDLE,
                ),
                (self.kernel, "GetCurrentThread", [], wintypes.HANDLE),
                (self.security, "ImpersonateNamedPipeClient", [wintypes.HANDLE], wintypes.BOOL),
                (self.security, "RevertToSelf", [], wintypes.BOOL),
                (self.security, "OpenThreadToken",
                 [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)],
                 wintypes.BOOL),
                (self.kernel, "CancelSynchronousIo", [wintypes.HANDLE], wintypes.BOOL),
                (self.kernel, "LocalFree", [wintypes.HLOCAL], wintypes.HLOCAL),
                (
                    self.security,
                    "OpenProcessToken",
                    [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
                    wintypes.BOOL,
                ),
                (
                    self.security,
                    "GetTokenInformation",
                    [
                        wintypes.HANDLE,
                        ctypes.c_int,
                        wintypes.LPVOID,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD),
                    ],
                    wintypes.BOOL,
                ),
                (
                    self.security,
                    "ConvertSidToStringSidW",
                    [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)],
                    wintypes.BOOL,
                ),
                (
                    self.security,
                    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                    [
                        wintypes.LPCWSTR,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPVOID),
                        ctypes.POINTER(wintypes.DWORD),
                    ],
                    wintypes.BOOL,
                ),
            ]
            for library, name, arguments, result in declarations:
                function = getattr(library, name)
                function.argtypes = arguments
                function.restype = result

        @staticmethod
        def _last_error(message: str) -> OSError:
            return OSError(_win_error_code(), message)

        def close(self, handle: int | None) -> None:
            if handle not in (None, 0, self.INVALID_HANDLE):
                self.kernel.CloseHandle(handle)

        def process_sid(self, pid: int | None = None) -> str:
            process: int
            close_process = False
            if pid is None or pid == os.getpid():
                process = self.kernel.GetCurrentProcess()
            else:
                process = self.kernel.OpenProcess(0x1000, False, pid)
                if not process:
                    raise self._last_error("Could not inspect owner pipe process")
                close_process = True
            token = wintypes.HANDLE()
            try:
                if not self.security.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
                    raise self._last_error("Could not inspect owner pipe process token")
                return self._token_sid(token)
            finally:
                self.close(token.value)
                if close_process:
                    self.close(process)

        def _token_sid(self, token: wintypes.HANDLE) -> str:
            needed = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not 0 < needed.value <= 65_536:
                raise OwnerPipeIdentityError("Invalid owner pipe token metadata")
            buffer = ctypes.create_string_buffer(needed.value)
            if not self.security.GetTokenInformation(
                token, 1, buffer, needed.value, ctypes.byref(needed)
            ):
                raise self._last_error("Could not inspect owner pipe token")
            sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
            rendered = wintypes.LPWSTR()
            if not self.security.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
                raise self._last_error("Could not render owner pipe SID")
            try:
                if rendered.value is None:
                    raise OwnerPipeIdentityError("Missing owner pipe SID")
                return rendered.value
            finally:
                self.kernel.LocalFree(rendered)

        def client_sid(self, handle: int) -> str:
            # Inspect the identity attached by Windows to the request just read.
            # No access to the other logon session's process object is required.
            if not self.security.ImpersonateNamedPipeClient(handle):
                raise self._last_error("Could not identify owner pipe client")
            token = wintypes.HANDLE()
            try:
                if not self.security.OpenThreadToken(
                    self.kernel.GetCurrentThread(), 0x0008, True, ctypes.byref(token)
                ):
                    raise self._last_error("Could not inspect owner pipe client token")
                return self._token_sid(token)
            finally:
                self.close(token.value)
                if not self.security.RevertToSelf():
                    raise OwnerPipeIdentityError("Could not restore owner pipe server identity")

        def create_server_pipe(
            self,
            name: str,
            owner_sid: str,
            max_payload: int,
            max_instances: int,
            *,
            first: bool,
        ) -> int:
            descriptor = wintypes.LPVOID()
            sddl = f"D:P(A;;GA;;;{owner_sid})"
            if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None
            ):
                raise self._last_error("Could not create owner pipe security descriptor")
            try:
                attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
                access = 0x00000003 | (0x00080000 if first else 0)
                mode = 0x00000008  # PIPE_REJECT_REMOTE_CLIENTS; byte mode and blocking are zero.
                handle = self.kernel.CreateNamedPipeW(
                    name,
                    access,
                    mode,
                    max_instances,
                    max_payload + _HEADER.size,
                    max_payload + _HEADER.size,
                    0,
                    ctypes.byref(attributes),
                )
                if handle == self.INVALID_HANDLE:
                    raise self._last_error("Could not create owner pipe instance")
                return int(handle)
            finally:
                self.kernel.LocalFree(descriptor)

        def connect_server(self, handle: int) -> None:
            if not self.kernel.ConnectNamedPipe(handle, None):
                error = _win_error_code()
                if error != self.ERROR_PIPE_CONNECTED:
                    raise OSError(error, "Owner pipe connection failed")

        def open_client(self, name: str, timeout: float) -> int:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnerPipeTimeout("Timed out connecting to owner pipe")
                wait_ms = max(1, min(int(remaining * 1000), 100))
                if self.kernel.WaitNamedPipeW(name, wait_ms):
                    handle = self.kernel.CreateFileW(
                        name, 0xC0000000, 0, None, 3, 0x00110000, None
                    )
                    if handle != self.INVALID_HANDLE:
                        return int(handle)
                    error = _win_error_code()
                    if error not in (self.ERROR_FILE_NOT_FOUND, self.ERROR_PIPE_BUSY):
                        raise OSError(error, "Could not open owner pipe")
                else:
                    error = _win_error_code()
                    if error not in (
                        self.ERROR_FILE_NOT_FOUND,
                        self.ERROR_SEM_TIMEOUT,
                        self.ERROR_PIPE_BUSY,
                    ):
                        raise OSError(error, "Could not wait for owner pipe")

        def peer_pid(self, handle: int, *, server: bool) -> int:
            pid = wintypes.ULONG()
            function = (
                self.kernel.GetNamedPipeServerProcessId
                if server
                else self.kernel.GetNamedPipeClientProcessId
            )
            if not function(handle, ctypes.byref(pid)):
                raise self._last_error("Could not inspect owner pipe peer")
            return int(pid.value)

        def read_exact(
            self, handle: int, size: int, deadline: float, stopped: threading.Event | None = None
        ) -> bytes:
            result = bytearray()
            while len(result) < size:
                if stopped is not None and stopped.is_set():
                    raise OwnerPipeCleanupError("Owner pipe server is stopping")
                if time.monotonic() >= deadline:
                    raise OwnerPipeTimeout("Timed out reading owner pipe frame")
                available = wintypes.DWORD()
                if not self.kernel.PeekNamedPipe(
                    handle, None, 0, None, ctypes.byref(available), None
                ):
                    error = _win_error_code()
                    if error in (
                        self.ERROR_BROKEN_PIPE,
                        self.ERROR_NO_DATA,
                        self.ERROR_PIPE_NOT_CONNECTED,
                    ):
                        raise EOFError("Owner pipe peer closed")
                    raise OSError(error, "Could not inspect owner pipe frame")
                if available.value == 0:
                    time.sleep(0.005)
                    continue
                amount = min(size - len(result), int(available.value), 65_536)
                buffer = ctypes.create_string_buffer(amount)
                received = wintypes.DWORD()
                if not self.kernel.ReadFile(
                    handle, buffer, amount, ctypes.byref(received), None
                ):
                    error = _win_error_code()
                    if error in (
                        self.ERROR_BROKEN_PIPE,
                        self.ERROR_NO_DATA,
                        self.ERROR_PIPE_NOT_CONNECTED,
                    ):
                        raise EOFError("Owner pipe peer closed")
                    raise OSError(error, "Could not read owner pipe frame")
                if received.value == 0:
                    raise EOFError("Owner pipe peer closed")
                result.extend(buffer.raw[: received.value])
            return bytes(result)

        def write_all(
            self, handle: int, payload: bytes, timeout: float, thread_handle: int
        ) -> None:
            deadline = time.monotonic() + timeout
            completed = threading.Event()
            cancelled = threading.Event()

            def cancel_until_complete() -> None:
                remaining = max(0.0, deadline - time.monotonic())
                if completed.wait(remaining):
                    return
                cancelled.set()
                # Cover cancellation racing just before WriteFile begins.  Repeating against the
                # one dedicated worker thread is safe until this write operation signals done.
                while not completed.is_set():
                    self.kernel.CancelSynchronousIo(thread_handle)
                    completed.wait(0.01)

            watchdog = threading.Thread(
                target=cancel_until_complete,
                name="anywhere-owner-json-write-timeout",
                daemon=True,
            )
            watchdog.start()
            position = 0
            try:
                while position < len(payload):
                    if time.monotonic() >= deadline or cancelled.is_set():
                        raise OwnerPipeTimeout("Timed out writing owner pipe frame")
                    chunk = payload[position : position + 65_536]
                    buffer = ctypes.create_string_buffer(chunk)
                    written = wintypes.DWORD()
                    success = self.kernel.WriteFile(
                        handle, buffer, len(chunk), ctypes.byref(written), None
                    )
                    error = _win_error_code()
                    if not success:
                        if error == self.ERROR_OPERATION_ABORTED and cancelled.is_set():
                            raise OwnerPipeTimeout("Timed out writing owner pipe frame")
                        raise OSError(error, "Could not write owner pipe frame")
                    if written.value == 0:
                        raise OwnerPipeProtocolError("Owner pipe write made no progress")
                    position += written.value
            finally:
                completed.set()
                watchdog.join()

        def wait_for_disconnect(self, handle: int, timeout: float) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                available = wintypes.DWORD()
                if not self.kernel.PeekNamedPipe(
                    handle, None, 0, None, ctypes.byref(available), None
                ):
                    error = _win_error_code()
                    if error in (
                        self.ERROR_BROKEN_PIPE,
                        self.ERROR_NO_DATA,
                        self.ERROR_PIPE_NOT_CONNECTED,
                    ):
                        return
                    raise OSError(error, "Could not observe owner pipe disconnect")
                time.sleep(0.005)
            raise OwnerPipeTimeout("Timed out waiting for owner pipe reader to close")


    _WINDOWS = _WindowsApi()


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("Owner JSON pipes are available only on Windows")


def _canonical_executable(pid: int) -> str:
    return os.path.normcase(os.path.abspath(psutil.Process(pid).exe()))


def _validate_json(payload: bytes, maximum: int) -> None:
    if not 1 <= len(payload) <= maximum:
        raise OwnerPipeProtocolError(f"Owner pipe JSON must contain 1–{maximum} bytes")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OwnerPipeProtocolError("Owner pipe payload must be strict UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise OwnerPipeProtocolError("Owner pipe payload must be a JSON object")


def _encode_frame(payload: bytes, maximum: int) -> bytes:
    _validate_json(payload, maximum)
    return _HEADER.pack(len(payload)) + payload


def _read_frame(
    handle: int, maximum: int, timeout: float, stopped: threading.Event | None = None
) -> bytes:
    deadline = time.monotonic() + timeout
    header = _WINDOWS.read_exact(handle, _HEADER.size, deadline, stopped)
    (size,) = _HEADER.unpack(header)
    if not 1 <= size <= maximum:
        raise OwnerPipeProtocolError("Owner pipe frame exceeds its payload limit")
    payload = _WINDOWS.read_exact(handle, size, deadline, stopped)
    _validate_json(payload, maximum)
    return payload


def _hello(endpoint: OwnerPipeEndpoint) -> bytes:
    return json.dumps(
        {
            "protocol": endpoint.protocol,
            "server_id": endpoint.server_id,
            "server_pid": endpoint.server_pid,
            "server_creation_time": endpoint.server_creation_time,
            "owner_sid": endpoint.owner_sid,
            "server_executable": endpoint.server_executable,
            "max_payload_bytes": endpoint.max_payload_bytes,
        },
        separators=(",", ":"),
    ).encode()


class OwnerJsonPipeServer:
    """Bounded, reconnectable native server dispatching one JSON request per connection."""

    def __init__(
        self,
        handler: Callable[[bytes], Awaitable[bytes]],
        *,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        request_timeout: float = DEFAULT_TIMEOUT,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        _pipe_name: str | None = None,
    ) -> None:
        _require_windows()
        if not 1 <= max_payload_bytes <= MAX_PAYLOAD_BYTES:
            raise ValueError(f"Owner pipe payload limit must be 1–{MAX_PAYLOAD_BYTES} bytes")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Owner pipe timeout must be positive")
        if not 1 <= max_connections <= 64:
            raise ValueError("Owner pipe connection limit must be 1–64")
        self._handler = handler
        self._maximum = max_payload_bytes
        self._request_timeout = request_timeout
        self._max_connections = max_connections
        self._pipe_name = _pipe_name or (
            r"\\.\pipe\anywhere-owner-json-" + secrets.token_hex(24)
        )
        self._stopped = threading.Event()
        self._closing = threading.Event()
        self._handles: set[int] = set()
        self._handles_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_handle: int | None = None
        self._connections: set[threading.Thread] = set()
        self._connection_thread_handles: dict[threading.Thread, int] = {}
        self._connections_lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(max_connections)
        self._dispatches: set[concurrent.futures.Future[bytes]] = set()
        self._dispatches_lock = threading.Lock()
        self._fatal: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        process = psutil.Process(os.getpid())
        self.endpoint = OwnerPipeEndpoint(
            pipe_name=self._pipe_name,
            server_id=secrets.token_hex(32),
            server_pid=os.getpid(),
            server_creation_time=process.create_time(),
            owner_sid=_WINDOWS.process_sid(),
            server_executable=_canonical_executable(os.getpid()),
            max_payload_bytes=max_payload_bytes,
        )
        self.endpoint.validate()

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> OwnerPipeEndpoint:
        if self._thread is not None:
            raise RuntimeError("Owner pipe server has already been started")
        self._loop = loop or asyncio.get_running_loop()
        first = self._create_instance(first=True)
        self._thread = threading.Thread(
            target=self._serve, args=(first,), name="anywhere-owner-json", daemon=True
        )
        self._thread.start()
        return self.endpoint

    def _create_instance(self, *, first: bool) -> int:
        if self._closing.is_set():
            raise OwnerPipeCleanupError("Owner pipe server is stopping")
        handle = _WINDOWS.create_server_pipe(
            self._pipe_name,
            self.endpoint.owner_sid,
            self._maximum,
            self._max_connections + 1,
            first=first,
        )
        with self._handles_lock:
            self._handles.add(handle)
        return handle

    def _release_handle(self, handle: int) -> None:
        with self._handles_lock:
            owned = handle in self._handles
            self._handles.discard(handle)
        if owned:
            _WINDOWS.kernel.DisconnectNamedPipe(handle)
            _WINDOWS.close(handle)

    def _serve(self, first: int) -> None:
        current: int | None = first
        self._thread_handle = _WINDOWS.kernel.OpenThread(
            0x0001, False, threading.get_native_id()
        )
        try:
            while current is not None and not self._closing.is_set():
                next_handle: int | None = None
                capacity_acquired = False
                try:
                    while not self._closing.is_set():
                        if self._capacity.acquire(timeout=0.05):
                            capacity_acquired = True
                            break
                    if not capacity_acquired:
                        break
                    _WINDOWS.connect_server(current)
                    if self._closing.is_set():
                        break
                    next_handle = self._create_instance(first=False)
                    worker = threading.Thread(
                        target=self._connection_worker,
                        args=(current,),
                        name="anywhere-owner-json-client",
                        daemon=True,
                    )
                    with self._connections_lock:
                        self._connections.add(worker)
                    worker.start()
                    current = None  # Ownership moved to the connection worker.
                except (EOFError, OSError, OwnerPipeError, concurrent.futures.CancelledError):
                    if capacity_acquired:
                        self._capacity.release()
                    if self._closing.is_set():
                        break
                except BaseException as error:
                    if capacity_acquired:
                        self._capacity.release()
                    self._fatal = error
                    self._closing.set()
                    self._stopped.set()
                    break
                finally:
                    if current is not None:
                        self._release_handle(current)
                current = next_handle
        finally:
            if current is not None:
                self._release_handle(current)

    def _connection_worker(self, handle: int) -> None:
        current = threading.current_thread()
        thread_handle = _WINDOWS.kernel.OpenThread(0x0001, False, threading.get_native_id())
        if not thread_handle:
            self._release_handle(handle)
            self._capacity.release()
            with self._connections_lock:
                self._connections.discard(current)
            return
        with self._connections_lock:
            self._connection_thread_handles[current] = int(thread_handle)
        try:
            self._serve_connection(handle, int(thread_handle))
        except (EOFError, OSError, OwnerPipeError, concurrent.futures.CancelledError):
            pass
        finally:
            self._release_handle(handle)
            self._capacity.release()
            with self._connections_lock:
                self._connections.discard(current)
                self._connection_thread_handles.pop(current, None)
            _WINDOWS.close(int(thread_handle))

    def _serve_connection(self, handle: int, thread_handle: int) -> None:
        _WINDOWS.write_all(
            handle,
            _encode_frame(_hello(self.endpoint), self._maximum),
            self._request_timeout,
            thread_handle,
        )
        request = _read_frame(handle, self._maximum, self._request_timeout, self._stopped)
        if _WINDOWS.client_sid(handle) != self.endpoint.owner_sid:
            raise OwnerPipeIdentityError("Owner pipe client SID does not match server owner")
        if self._loop is None:
            raise OwnerPipeCleanupError("Owner pipe server loop is unavailable")
        async def invoke() -> bytes:
            return await self._handler(request)

        future = asyncio.run_coroutine_threadsafe(invoke(), self._loop)
        with self._dispatches_lock:
            self._dispatches.add(future)

        def finished(completed: concurrent.futures.Future[bytes]) -> None:
            with self._dispatches_lock:
                self._dispatches.discard(completed)

        future.add_done_callback(finished)
        deadline = time.monotonic() + self._request_timeout
        while True:
            if self._stopped.is_set():
                raise OwnerPipeCleanupError("Owner pipe transport stopped during dispatch")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The dispatcher may have committed an operation.  A transport timeout is never
                # evidence that execution did not happen, so leave the future running for drain.
                raise OwnerPipeTimeout("Owner pipe dispatcher timed out")
            try:
                response = future.result(timeout=min(remaining, 0.05))
                break
            except concurrent.futures.TimeoutError:
                continue
        _WINDOWS.write_all(
            handle,
            _encode_frame(response, self._maximum),
            self._request_timeout,
            thread_handle,
        )
        # Keep the native instance alive until the client has consumed the buffered reply and
        # closed.  This avoids the reply-loss race without an unbounded FlushFileBuffers call.
        _WINDOWS.wait_for_disconnect(handle, self._request_timeout)

    def _stop_accepting(self, timeout: float) -> None:
        self._closing.set()
        if self._thread_handle:
            _WINDOWS.kernel.CancelSynchronousIo(self._thread_handle)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise OwnerPipeCleanupError("Owner pipe worker did not stop")
        _WINDOWS.close(self._thread_handle)
        self._thread_handle = None

    def _stop_transport(self, timeout: float) -> None:
        self._stop_accepting(timeout)
        self._stopped.set()
        with self._connections_lock:
            thread_handles = tuple(self._connection_thread_handles.values())
        for thread_handle in thread_handles:
            _WINDOWS.kernel.CancelSynchronousIo(thread_handle)
        deadline = time.monotonic() + timeout
        while True:
            with self._connections_lock:
                workers = tuple(self._connections)
            if not workers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OwnerPipeCleanupError("Owner pipe connection workers did not stop")
            for worker in workers:
                worker.join(min(remaining, 0.05))

    def _pending_dispatches(self) -> tuple[concurrent.futures.Future[bytes], ...]:
        with self._dispatches_lock:
            return tuple(future for future in self._dispatches if not future.done())

    def close(self, timeout: float = 3.0) -> None:
        """Stop transport threads and drain dispatch without cancelling committed work.

        Callers running on the dispatch event loop must use ``await aclose()`` whenever work may
        still be active, otherwise blocking that loop would prevent a safe drain.
        """

        self._stop_transport(timeout)
        pending = self._pending_dispatches()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if pending and running is self._loop:
            raise OwnerPipeCleanupError("Active dispatch requires await server.aclose()")
        deadline = time.monotonic() + timeout
        for future in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OwnerPipeCleanupError("Owner pipe dispatch did not drain")
            try:
                future.result(timeout=remaining)
            except concurrent.futures.TimeoutError as error:
                raise OwnerPipeCleanupError("Owner pipe dispatch did not drain") from error
            except Exception:
                # Dispatcher failures are owned by the operation ledger, not transport cleanup.
                pass
        if self._fatal is not None:
            raise OwnerPipeCleanupError("Owner pipe worker failed") from self._fatal

    async def aclose(self, timeout: float = 3.0, *, drain_connections: bool = False) -> None:
        if drain_connections:
            # Stop admission while admitted clients consume their replies. Do not
            # signal _stopped until those workers finish or the drain budget expires.
            await asyncio.to_thread(self._stop_accepting, timeout)
            deadline = time.monotonic() + timeout
            while self.active_connection_count and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        await asyncio.to_thread(self._stop_transport, timeout)
        deadline = time.monotonic() + timeout
        while self._pending_dispatches():
            if time.monotonic() >= deadline:
                raise OwnerPipeCleanupError("Owner pipe dispatch did not drain")
            await asyncio.sleep(0.01)
        if self._fatal is not None:
            raise OwnerPipeCleanupError("Owner pipe worker failed") from self._fatal

    @property
    def worker_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def active_connection_count(self) -> int:
        with self._connections_lock:
            return len(self._connections)


def _verify_server_identity(handle: int, endpoint: OwnerPipeEndpoint) -> None:
    native_pid = _WINDOWS.peer_pid(handle, server=True)
    if native_pid != endpoint.server_pid:
        raise OwnerPipeIdentityError("Owner pipe server PID does not match endpoint")
    current_sid = _WINDOWS.process_sid()
    server_sid = _WINDOWS.process_sid(native_pid)
    if server_sid != endpoint.owner_sid or server_sid != current_sid:
        raise OwnerPipeIdentityError("Owner pipe server SID does not match current user")
    try:
        process = psutil.Process(native_pid)
        if abs(process.create_time() - endpoint.server_creation_time) > 0.001:
            raise OwnerPipeIdentityError("Owner pipe server creation time does not match endpoint")
        if _canonical_executable(native_pid) != os.path.normcase(
            os.path.abspath(endpoint.server_executable)
        ):
            raise OwnerPipeIdentityError("Owner pipe server executable does not match endpoint")
    except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
        raise OwnerPipeIdentityError("Could not verify owner pipe server process") from error


def _verify_hello(payload: bytes, endpoint: OwnerPipeEndpoint) -> None:
    try:
        hello = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OwnerPipeIdentityError("Invalid owner pipe server hello") from error
    expected = {
        "protocol": endpoint.protocol,
        "server_id": endpoint.server_id,
        "server_pid": endpoint.server_pid,
        "server_creation_time": endpoint.server_creation_time,
        "owner_sid": endpoint.owner_sid,
        "server_executable": endpoint.server_executable,
        "max_payload_bytes": endpoint.max_payload_bytes,
    }
    if hello != expected:
        raise OwnerPipeIdentityError("Owner pipe server hello does not match endpoint")


def request_owner_json_pipe(
    endpoint: OwnerPipeEndpoint, payload: bytes, *, timeout: float = DEFAULT_TIMEOUT
) -> bytes:
    """Send one JSON request after native server identity verification, then read one reply."""

    _require_windows()
    endpoint.validate()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Owner pipe timeout must be positive")
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise OwnerPipeTimeout("Owner pipe request deadline expired")
        return value

    frame = _encode_frame(payload, endpoint.max_payload_bytes)
    handle = _WINDOWS.open_client(endpoint.pipe_name, remaining())
    thread_handle = _WINDOWS.kernel.OpenThread(0x0001, False, threading.get_native_id())
    if not thread_handle:
        _WINDOWS.close(handle)
        raise OSError("Could not open owner pipe client thread")
    try:
        _verify_server_identity(handle, endpoint)
        hello = _read_frame(handle, endpoint.max_payload_bytes, remaining())
        _verify_hello(hello, endpoint)
        _WINDOWS.write_all(handle, frame, remaining(), int(thread_handle))
        return _read_frame(handle, endpoint.max_payload_bytes, remaining())
    finally:
        _WINDOWS.close(handle)
        _WINDOWS.close(int(thread_handle))


async def request_owner_json_pipe_async(
    endpoint: OwnerPipeEndpoint, payload: bytes, *, timeout: float = DEFAULT_TIMEOUT
) -> bytes:
    """Async adapter that keeps native blocking I/O outside the asyncio event-loop thread."""

    return await asyncio.to_thread(request_owner_json_pipe, endpoint, payload, timeout=timeout)
