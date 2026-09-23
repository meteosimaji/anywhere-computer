"""Real ConPTY integration, executed by the Windows CI runner."""

import asyncio
import os
import subprocess
import sys

import pytest

from anywhere_computer.models import SessionInput, SessionOutput, SessionResize, StartSession
from anywhere_computer.sessions import Sessions

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY")


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
    finally:
        await sessions.close()
    assert sessions.get(identity).output_eof


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
