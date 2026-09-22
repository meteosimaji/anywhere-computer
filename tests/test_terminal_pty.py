"""Exercise a real POSIX terminal while retaining the existing pipe contract."""

import asyncio
import os
import shlex
import sys

import pytest

from anywhere_computer.models import SessionInput, SessionOutput, SessionResize, StartSession
from anywhere_computer.sessions import Sessions

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX PTY")


async def _start(tmp_path, source: str):
    script = tmp_path / "terminal_child.py"
    script.write_text(source)
    sessions = Sessions()
    started = await sessions.start(StartSession(
        command=f"{shlex.quote(sys.executable)} -u {shlex.quote(str(script))}",
        cwd=str(tmp_path), interactive=True, rows=31, columns=91,
    ))
    return sessions, started["session_id"]


async def test_real_pty_unicode_resize_interrupt_and_cursor_recovery(tmp_path):
    sessions, identity = await _start(tmp_path, '''import fcntl, os, signal, struct, sys, termios
def size():
    rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0'*8))
    return f'{rows}x{cols}'
signal.signal(signal.SIGWINCH, lambda *_: print('RESIZED ' + size(), flush=True))
signal.signal(signal.SIGINT, lambda *_: print('INTERRUPTED', flush=True))
print(f'TTY={os.isatty(0)} SIZE={size()}', flush=True)
for line in sys.stdin:
    print('ECHO=' + line.strip(), flush=True)
''')
    try:
        page = await sessions.wait_output(SessionOutput(session_id=identity, wait_ms=3000))
        assert "TTY=True SIZE=31x91" in page["text"]
        cursor = page["next_cursor"]
        sent = await sessions.send(SessionInput(
            session_id=identity, text="日本語🙂\n", wait_ms=3000,
            wait_for_prompt="ECHO=日本語🙂",
        ))
        assert sent["prompt_matched"]
        assert sent["bytes_sent"] == len("日本語🙂\n".encode())
        recovered = sessions.output(SessionOutput(session_id=identity, cursor=cursor, limit=1))
        assert recovered["next_cursor"] > cursor
        resized = await sessions.resize(SessionResize(session_id=identity, rows=45, columns=120))
        assert resized["rows"] == 45 and resized["columns"] == 120
        size_output = await sessions.wait_output(SessionOutput(
            session_id=identity, cursor=sent["next_cursor"], wait_ms=3000,
        ))
        assert "RESIZED 45x120" in size_output["text"]
        interrupted = await sessions.send(SessionInput(
            session_id=identity, text="\x03", wait_ms=3000,
            wait_for_prompt="INTERRUPTED",
        ))
        assert interrupted["prompt_matched"]
        eof = await sessions.send(SessionInput(session_id=identity, text="\x04", wait_ms=3000))
        assert eof["bytes_sent"] == 1
        await asyncio.wait_for(sessions.get(identity).reader, 5)
        final = sessions.output(SessionOutput(session_id=identity, cursor=0))
        assert final["output_eof"] and final["state"] == "exited"
        assert "ECHO=日本語🙂" in final["text"]
    finally:
        await sessions.close()


async def test_pipe_default_and_resize_rejection(tmp_path):
    sessions = Sessions()
    started = await sessions.start(StartSession(
        command="echo pipe", cwd=str(tmp_path), shell="/bin/sh",
    ))
    try:
        assert started["interactive"] is False
        with pytest.raises(ValueError, match="not an active interactive"):
            await sessions.resize(SessionResize(session_id=started["session_id"],
                                                rows=25, columns=80))
        await asyncio.wait_for(sessions.get(started["session_id"]).reader, 5)
        assert "pipe" in sessions.output(SessionOutput(session_id=started["session_id"]))["text"]
    finally:
        await sessions.close()
