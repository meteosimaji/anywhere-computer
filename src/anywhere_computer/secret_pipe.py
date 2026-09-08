"""One-shot credential handoff through OS pipes, never a regular file."""

import errno
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType


class SecretPipeCleanupError(RuntimeError):
    """A writer might remain alive; do not start another credential handoff."""


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    class _WindowsPipe:
        def __init__(self, name: str) -> None:
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.security = ctypes.WinDLL("advapi32", use_last_error=True)
            # Explicit signatures are essential on 64-bit Windows: handles are pointers.
            declarations = [
                (self.kernel, "GetCurrentProcess", [], wintypes.HANDLE),
                (self.kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
                (self.kernel, "LocalFree", [wintypes.HLOCAL], wintypes.HLOCAL),
                (
                    self.kernel,
                    "OpenThread",
                    [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                    wintypes.HANDLE,
                ),
                (self.kernel, "CancelSynchronousIo", [wintypes.HANDLE], wintypes.BOOL),
                (
                    self.kernel,
                    "ConnectNamedPipe",
                    [wintypes.HANDLE, wintypes.LPVOID],
                    wintypes.BOOL,
                ),
                (self.kernel, "FlushFileBuffers", [wintypes.HANDLE], wintypes.BOOL),
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
            for library, name_, arguments, result in declarations:
                function = getattr(library, name_)
                function.argtypes, function.restype = arguments, result
            descriptor = wintypes.LPVOID()
            sddl = "D:P(A;;GA;;;" + self._user_sid() + ")"
            self._check(
                self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl,
                    1,
                    ctypes.byref(descriptor),
                    None,
                )
            )
            try:
                attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
                # Outbound byte pipe, one instance, no remote clients, no inherited handle.
                self.handle = self.kernel.CreateNamedPipeW(
                    name,
                    0x00080002,
                    0x00000008,
                    1,
                    8192,
                    0,
                    0,
                    ctypes.byref(attributes),
                )
                if self.handle == ctypes.c_void_p(-1).value:
                    raise OSError("Could not create private credential pipe")
            finally:
                self.kernel.LocalFree(descriptor)
            self.thread_handle: int | None = None

        @staticmethod
        def _check(success: object) -> None:
            if not success:
                raise OSError("Windows credential pipe operation failed")

        def _user_sid(self) -> str:
            token = wintypes.HANDLE()
            self._check(
                self.security.OpenProcessToken(
                    self.kernel.GetCurrentProcess(),
                    0x0008,
                    ctypes.byref(token),
                )
            )
            try:
                needed = wintypes.DWORD()
                self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
                if not 0 < needed.value <= 65536:
                    raise OSError("Could not determine credential pipe owner")
                buffer = ctypes.create_string_buffer(needed.value)
                self._check(
                    self.security.GetTokenInformation(
                        token,
                        1,
                        buffer,
                        needed.value,
                        ctypes.byref(needed),
                    )
                )
                # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is PSID.
                sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
                rendered = wintypes.LPWSTR()
                self._check(self.security.ConvertSidToStringSidW(sid, ctypes.byref(rendered)))
                try:
                    if rendered.value is None:
                        raise OSError("Could not determine credential pipe owner")
                    return rendered.value
                finally:
                    self.kernel.LocalFree(rendered)
            finally:
                self.kernel.CloseHandle(token)

        def send(self, payload: bytes, stopped: threading.Event) -> None:
            self.thread_handle = self.kernel.OpenThread(0x0001, False, threading.get_native_id())
            if not self.thread_handle:
                raise OSError("Could not open credential writer thread")
            if stopped.is_set():
                return
            connected = self.kernel.ConnectNamedPipe(self.handle, None)
            if not connected and ctypes.get_last_error() != 535:  # ERROR_PIPE_CONNECTED
                raise OSError("Credential pipe connection failed")
            if stopped.is_set():
                return
            written = wintypes.DWORD()
            self._check(
                self.kernel.WriteFile(
                    self.handle,
                    payload,
                    len(payload),
                    ctypes.byref(written),
                    None,
                )
            )
            if written.value != len(payload):
                raise OSError("Credential pipe write was incomplete")
            if not stopped.is_set():
                # Preserve the buffered bytes until the reader consumes them, then close for EOF.
                self._check(self.kernel.FlushFileBuffers(self.handle))

        def cancel(self) -> None:
            if self.thread_handle is not None:
                self.kernel.CancelSynchronousIo(self.thread_handle)

        def close(self) -> None:
            if self.thread_handle is not None:
                self.kernel.CloseHandle(self.thread_handle)

        def finish(self) -> None:
            self.kernel.CloseHandle(self.handle)


class SecretPipe:
    """Single consumer, at most 4096 bytes; use only with a trusted local process.

    The parent must close this context even when the consumer never opens the pipe.
    A same-user process can read process memory or race the pipe; this is not a
    sandbox against the current user or administrators.
    """

    def __init__(self, payload: bytes) -> None:
        if not 1 <= len(payload) <= 4096:
            raise ValueError("Credential pipe payload must contain 1–4096 bytes")
        self._payload = payload
        self._stopped = threading.Event()
        self._finished = threading.Event()
        self._failed = False
        self._thread: threading.Thread | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if sys.platform == "win32":
            self.path = "\\\\.\\pipe\\anywhere-credential-" + secrets.token_hex(24)
            self._windows = _WindowsPipe(self.path)
        else:
            self._temporary = tempfile.TemporaryDirectory(prefix="anywhere-credential-")
            self.path = str(Path(self._temporary.name) / "input")
            os.mkfifo(self.path, 0o600)

    def _send_posix(self) -> None:
        if sys.platform == "win32":
            raise RuntimeError("POSIX credential pipe is unavailable on Windows")
        descriptor: int | None = None
        try:
            while not self._stopped.is_set():
                try:
                    descriptor = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        raise
                    self._stopped.wait(0.01)
            if descriptor is None:
                return
            position = 0
            while position < len(self._payload) and not self._stopped.is_set():
                try:
                    position += os.write(descriptor, self._payload[position:])
                except BlockingIOError:
                    self._stopped.wait(0.01)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _send(self) -> None:
        try:
            if sys.platform == "win32":
                self._windows.send(self._payload, self._stopped)
            else:
                self._send_posix()
        except Exception:
            self._failed = True  # Never retain or print an exception containing a credential.
        finally:
            if sys.platform == "win32":
                self._windows.finish()
            self._payload = b""
            self._finished.set()

    def __enter__(self) -> "SecretPipe":
        if self._thread is not None:
            raise RuntimeError("Credential pipe is single-use")
        self._thread = threading.Thread(target=self._send, name="anywhere-credential", daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout: float = 15) -> None:
        if not self._finished.wait(timeout):
            raise TimeoutError("Consumer did not read its credential pipe")
        if self._failed or self._stopped.is_set():
            raise RuntimeError("Credential pipe handoff failed")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopped.set()
        if self._thread is not None:
            deadline = time.monotonic() + 3
            while self._thread.is_alive() and time.monotonic() < deadline:
                if sys.platform == "win32":
                    # Repeat to cover cancellation racing entry into the next blocking call.
                    self._windows.cancel()
                self._thread.join(0.02)
            if self._thread.is_alive():
                raise SecretPipeCleanupError("Credential writer did not stop; process must exit")
        if sys.platform == "win32":
            self._windows.close()
        if self._temporary is not None:
            self._temporary.cleanup()
        self._payload = b""
