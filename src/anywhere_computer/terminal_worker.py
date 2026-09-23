"""Keep an OS ownership anchor alive until a terminal's process family is empty.

Executed by absolute path in an isolated interpreter. No agent state is imported.
POSIX commands inherit this worker's session/group; Windows commands inherit its Job.
"""

import ctypes
import importlib
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol


class _ConPTY(Protocol):
    def write(self, text: str) -> int: ...
    def set_size(self, columns: int, rows: int) -> None: ...

if sys.platform == "win32":
    class WindowsJob:
        """Assign ourselves before spawning, so even short-lived children stay in the Job."""

        def __init__(self) -> None:
            from ctypes import wintypes

            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD),
                ]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
                ]

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
            self.kernel.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
            ]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            try:
                if not self.kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not self.kernel.AssignProcessToJobObject(
                    self.handle, self.kernel.GetCurrentProcess(),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            except OSError:
                self.kernel.CloseHandle(self.handle)
                raise

        def others_alive(self) -> bool:
            # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four int64 and four DWORD fields.
            info = (ctypes.c_uint64 * 6)()
            if not self.kernel.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            active = ctypes.c_uint32.from_buffer(info, 40).value
            return active > 1

else:
    class WindowsJob:
        def __init__(self) -> None:
            raise RuntimeError("Windows Job Objects require Windows")

        def others_alive(self) -> bool:
            raise RuntimeError("Windows Job Objects require Windows")


def group_others_alive() -> bool:
    """Our live group leader prevents this PGID being recycled during enumeration."""
    if sys.platform == "win32":
        raise RuntimeError("POSIX process groups require POSIX")
    group = os.getpgrp()
    own = os.getpid()
    snapshot = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,pgid=,stat="],
        text=True, timeout=2, start_new_session=True,
    )
    for row in snapshot.splitlines():
        pid, pgid, status = row.split(maxsplit=2)
        if int(pid) != own and int(pgid) == group and not status.startswith("Z"):
            return True
    return False


def _conpty_input(pty: _ConPTY) -> None:
    """Relay framed input and resize requests to the Windows console."""
    stream = sys.stdin.buffer
    while header := stream.read(5):
        if len(header) != 5:
            break
        kind, length = header[:1], int.from_bytes(header[1:], "big")
        if length > 400000:
            break
        payload = stream.read(length)
        if len(payload) != length:
            break
        try:
            if kind == b"I":
                pty.write(payload.decode("utf-8"))
            elif kind == b"R" and length == 8:
                sequence, rows, columns = struct.unpack("!IHH", payload)
                try:
                    pty.set_size(columns, rows)
                except (OSError, RuntimeError):
                    status = b"E"
                else:
                    status = b"A"
                sys.stderr.buffer.write(b"\0" + status + sequence.to_bytes(4, "big"))
                sys.stderr.buffer.flush()
            else:
                break
        except (OSError, RuntimeError):
            break


def main_conpty(shell: str, command: str, rows: int, columns: int) -> int:
    """Keep ConPTY and the process Job alive until its process family exits."""
    if os.name != "nt":
        raise RuntimeError("ConPTY requires Windows")
    winpty = importlib.import_module("winpty")

    job = WindowsJob()
    pty = winpty.PTY(columns, rows, backend=winpty.Backend.ConPTY)
    arguments = ["/c", command] if Path(shell).name.lower() == "cmd.exe" else ["-c", command]
    pty.spawn(shell, cmdline=" " + subprocess.list2cmdline(arguments), cwd=os.getcwd())
    threading.Thread(target=_conpty_input, args=(pty,), daemon=True).start()
    # Poll nonblocking so a dead command cannot leave the ownership worker stuck
    # in a blocking read. ConPTY output is UTF-8 and uses the same byte cursor.
    while True:
        chunk = pty.read(blocking=False)
        if chunk:
            sys.stdout.buffer.write(chunk.encode("utf-8"))
            sys.stdout.buffer.flush()
        if not job.others_alive():
            # Drain any output buffered during process teardown.
            for _ in range(10):
                chunk = pty.read(blocking=False)
                if chunk:
                    sys.stdout.buffer.write(chunk.encode("utf-8"))
                    sys.stdout.buffer.flush()
                time.sleep(0.01)
            break
        time.sleep(0.01)
    return pty.get_exitstatus() or 0


def main() -> int:
    if sys.argv[1:2] == ["--conpty"]:
        shell, command, rows, columns = sys.argv[2:]
        return main_conpty(shell, command, int(rows), int(columns))
    shell, command = sys.argv[1:]
    job = WindowsJob() if os.name == "nt" else None
    if os.name != "nt":
        # Group signals reach the command too. Retain the anchor until children exit.
        signal.signal(signal.SIGTERM, lambda *_: None)
        signal.signal(signal.SIGINT, lambda *_: None)
    if os.name == "nt" and Path(shell).name.lower() == "cmd.exe":
        child = subprocess.Popen(command, shell=True, executable=shell)
    else:
        child = subprocess.Popen([shell, "-c", command])
    code = child.wait()
    while True:
        try:
            remaining = job.others_alive() if job else group_others_alive()
        except (OSError, subprocess.SubprocessError, ValueError):
            remaining = True
        if not remaining:
            break
        time.sleep(0.05)
    # Let process teardown close the Windows Job handle, after its other members exit.
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
