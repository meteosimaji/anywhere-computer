"""Real ConPTY integration, executed by the Windows CI runner."""

import asyncio
import io
import os
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from anywhere_computer import terminal_worker
from anywhere_computer.models import SessionInput, SessionOutput, SessionResize, StartSession
from anywhere_computer.sessions import Session, Sessions

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY")


async def _until(sessions: Sessions, identity: str, marker: str) -> dict:
    cursor = 0
    seen = ""
    async with asyncio.timeout(10):
        while marker not in seen:
            page = await sessions.wait_output(SessionOutput(
                session_id=identity, cursor=cursor, wait_ms=250,
            ))
            seen += str(page["text"])
            cursor = int(page["next_cursor"])
            if page["output_eof"]:
                raise AssertionError(f"ConPTY closed before {marker!r}: {seen!r}")
    return {"text": seen, "next_cursor": cursor}


@windows_only
async def test_conpty_console_unicode_resize_and_cursor(tmp_path):
    script = tmp_path / "console.py"
    script.write_text('''import ctypes, msvcrt, os, shutil, sys
from ctypes import wintypes
mode = wintypes.DWORD()
console = ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(0), ctypes.byref(mode))
print('CONSOLE=' + str(bool(console)), flush=True)
for line in sys.stdin:
    if line.strip() == 'size':
        print('SIZE=' + str(shutil.get_terminal_size().columns), flush=True)
    else:
        print('ECHO=' + line.strip(), flush=True)
''')
    sessions = Sessions()
    started = await sessions.start(StartSession(
        command=subprocess.list2cmdline([sys.executable, "-u", str(script)]),
        cwd=str(tmp_path), shell=os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
        interactive=True, rows=30, columns=90,
    ))
    identity = started["session_id"]
    try:
        ready = await _until(sessions, identity, "CONSOLE=True")
        assert "CONSOLE=True" in ready["text"]
        echo = await sessions.send(SessionInput(
            session_id=identity, text="日本語🙂\r", wait_ms=10000,
            wait_for_prompt="ECHO=日本語🙂",
        ))
        assert echo["prompt_matched"]
        size = await sessions.resize(SessionResize(session_id=identity, rows=45, columns=120))
        assert size["columns"] == 120
        measured = await sessions.send(SessionInput(
            session_id=identity, text="size\r", wait_ms=10000,
            wait_for_prompt="SIZE=120",
        ))
        assert measured["prompt_matched"]
        page = sessions.output(SessionOutput(session_id=identity, cursor=ready["next_cursor"]))
        assert "ECHO=日本語🙂" in page["text"]
        assert page["next_cursor"] <= page["end_cursor"]
        await sessions.send(SessionInput(session_id=identity, text="\x1a\r"))
        await asyncio.wait_for(sessions.get(identity).reader, 10)
        assert sessions.get(identity).output_eof
    finally:
        await sessions.close()
    assert sessions.get(identity).output_eof


@windows_only
async def test_conpty_interrupt_and_cleanup(tmp_path):
    script = tmp_path / "interrupt.py"
    script.write_text('''import sys, time
print('READY', flush=True)
try:
    while True: time.sleep(0.1)
except KeyboardInterrupt:
    print('INTERRUPTED', flush=True)
''')
    sessions = Sessions()
    started = await sessions.start(StartSession(
        command=subprocess.list2cmdline([sys.executable, "-u", str(script)]),
        cwd=str(tmp_path), shell=os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
        interactive=True,
    ))
    identity = started["session_id"]
    try:
        ready = await _until(sessions, identity, "READY")
        assert "READY" in ready["text"]
        interrupted = await sessions.send(SessionInput(
            session_id=identity, text="\x03", wait_ms=10000,
            wait_for_prompt="INTERRUPTED",
        ))
        assert interrupted["prompt_matched"]
        await asyncio.wait_for(sessions.get(identity).reader, 10)
        assert sessions.get(identity).output_eof
    finally:
        await sessions.close()


class _InputWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def drain(self) -> None:
        pass


async def test_conpty_resize_waits_for_matching_worker_ack():
    writer = _InputWriter()
    control = asyncio.StreamReader()
    process = SimpleNamespace(stdin=writer, stderr=control, returncode=None, pid=1)
    session = Session("test", process, time.time(), interactive=True, rows=30, columns=90)
    session.control_reader = asyncio.create_task(Sessions._read_control(session))
    try:
        resize = asyncio.create_task(Sessions._resize_conpty(session, 45, 120))
        await asyncio.sleep(0)
        assert writer.frames == [b"R" + struct.pack("!I", 8) + struct.pack("!IHH", 1, 45, 120)]
        assert not resize.done()  # drain completed; set_size has not acknowledged.
        control.feed_data(b"\0A" + struct.pack("!I", 999))
        await asyncio.sleep(0)
        assert not resize.done()  # A late ACK for another request cannot complete this one.
        control.feed_data(b"\0A" + struct.pack("!I", 1))
        await asyncio.wait_for(resize, 1)
    finally:
        control.feed_eof()
        await session.control_reader


async def test_conpty_resize_worker_error_and_closed_control_stream():
    writer = _InputWriter()
    control = asyncio.StreamReader()
    process = SimpleNamespace(stdin=writer, stderr=control, returncode=None, pid=1)
    session = Session("test", process, time.time(), interactive=True)
    session.control_reader = asyncio.create_task(Sessions._read_control(session))
    first = asyncio.create_task(Sessions._resize_conpty(session, 45, 120))
    await asyncio.sleep(0)
    control.feed_data(b"\0E" + struct.pack("!I", 1))
    with pytest.raises(RuntimeError, match="resize failed"):
        await asyncio.wait_for(first, 1)
    second = asyncio.create_task(Sessions._resize_conpty(session, 45, 120))
    await asyncio.sleep(0)
    control.feed_eof()
    with pytest.raises(RuntimeError, match="control stream closed"):
        await asyncio.wait_for(second, 1)
    await session.control_reader
    with pytest.raises(RuntimeError, match="control stream closed"):
        await Sessions._resize_conpty(session, 45, 120)
    assert len(writer.frames) == 2


async def test_conpty_worker_startup_error_remains_visible():
    control = asyncio.StreamReader()
    process = SimpleNamespace(stderr=control)
    session = Session("test", process, time.time(), interactive=True)
    reader = asyncio.create_task(Sessions._read_control(session))
    control.feed_data(b"Traceback: startup failed\n")
    control.feed_eof()
    await reader
    assert bytes(session.output) == b"Traceback: startup failed\n"
    assert session.control_closed


def test_conpty_worker_ack_follows_resize_and_reports_failure(monkeypatch):
    class Console:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def set_size(self, columns: int, rows: int) -> None:
            self.calls.append((columns, rows))
            assert response.getvalue() == (
                b"" if columns == 120 else b"\0A" + struct.pack("!I", 1)
            )
            if columns == 121:
                raise OSError("resize rejected")

        def write(self, text: str) -> int:
            raise AssertionError("Unexpected input")

    requests = (
        b"R" + struct.pack("!I", 8) + struct.pack("!IHH", 1, 45, 120)
        + b"R" + struct.pack("!I", 8) + struct.pack("!IHH", 2, 45, 121)
    )
    response = io.BytesIO()
    monkeypatch.setattr(terminal_worker.sys, "stderr", SimpleNamespace(buffer=response))
    console = Console()
    terminal_worker._conpty_input(console, io.BytesIO(requests))
    assert console.calls == [(120, 45), (121, 45)]
    assert response.getvalue() == b"\0A" + struct.pack("!I", 1) + b"\0E" + struct.pack("!I", 2)


def test_conpty_input_accepts_partial_raw_reads(monkeypatch):
    class ShortRead(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 1))

    class Console:
        def __init__(self) -> None:
            self.text = ""

        def write(self, text: str) -> int:
            self.text += text
            return len(text)

    payload = "日本語".encode()
    stream = ShortRead(b"I" + len(payload).to_bytes(4, "big") + payload)
    console = Console()
    monkeypatch.setattr(terminal_worker.sys, "stdin", SimpleNamespace(
        buffer=SimpleNamespace(raw=stream),
    ))
    terminal_worker._conpty_input(console)
    assert console.text == "日本語"


@pytest.mark.parametrize("case", ["job_empty", "shell_exited", "shell_alive"])
def test_conpty_read_error_only_means_eof_after_shell_exit(monkeypatch, case):
    class WinptyError(Exception):
        pass

    class Console:
        def __init__(self, *_args, **_kwargs) -> None:
            self.reads = 0

        def spawn(self, *_args, **_kwargs) -> None:
            pass

        def read(self, *, blocking: bool) -> str:
            assert not blocking
            self.reads += 1
            if self.reads == 1:
                return "OUTPUT"
            raise WinptyError("closed output")

        def get_exitstatus(self) -> int | None:
            return None if case == "shell_alive" else 0

    console = Console()
    output = io.BytesIO()
    remaining_checks = 0

    def others_alive() -> bool:
        nonlocal remaining_checks
        if console.reads == 1:
            return True
        remaining_checks += 1
        return case != "job_empty" and remaining_checks <= 2

    monkeypatch.setattr(terminal_worker, "os", SimpleNamespace(name="nt", getcwd=lambda: "/tmp"))
    monkeypatch.setattr(terminal_worker, "WindowsJob", lambda: SimpleNamespace(
        others_alive=others_alive,
    ))
    monkeypatch.setattr(terminal_worker.importlib, "import_module", lambda _: SimpleNamespace(
        PTY=lambda *_args, **_kwargs: console,
        Backend=SimpleNamespace(ConPTY=object()), WinptyError=WinptyError,
    ))
    monkeypatch.setattr(terminal_worker.sys, "stdin", SimpleNamespace(
        buffer=SimpleNamespace(raw=io.BytesIO()),
    ))
    monkeypatch.setattr(terminal_worker.sys, "stdout", SimpleNamespace(buffer=output))
    if case == "shell_alive":
        with pytest.raises(WinptyError, match="closed output"):
            terminal_worker.main_conpty("C:/Windows/System32/cmd.exe", "echo output", 24, 80)
    else:
        assert terminal_worker.main_conpty(
            "C:/Windows/System32/cmd.exe", "echo output", 24, 80,
        ) == 0
    assert output.getvalue() == b"OUTPUT"


def test_conpty_cmdline_preserves_quotes_for_spaced_paths():
    command = subprocess.list2cmdline([
        r"C:\Program Files\Anywhere Computer\runtime\python.exe",
        "-u",
        r"C:\Users\Example User\test script.py",
    ])
    assert terminal_worker._conpty_shell_cmdline("C:/Windows/System32/cmd.exe", command) == (
        f' /s /c "{command}"'
    )


@windows_only
async def test_conpty_launches_quoted_command_with_spaced_script_path(tmp_path):
    script_dir = tmp_path / "spaced directory"
    script_dir.mkdir()
    script = script_dir / "console test.py"
    script.write_text("print('SPACED_COMMAND_STARTED', flush=True)\n")
    sessions = Sessions()
    started = await sessions.start(StartSession(
        command=f'"{sys.executable}" -u "{script}"',
        cwd=str(tmp_path), shell=os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
        interactive=True,
    ))
    try:
        ready = await _until(sessions, started["session_id"], "SPACED_COMMAND_STARTED")
        assert "SPACED_COMMAND_STARTED" in ready["text"]
        await asyncio.wait_for(sessions.get(started["session_id"]).reader, 10)
        assert sessions.get(started["session_id"]).process.returncode == 0
    finally:
        await sessions.close()
