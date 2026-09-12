import asyncio
import shlex
import sys

from anywhere_computer.models import SessionOutput, StartSession
from anywhere_computer.sessions import Sessions


async def test_utf8_pages_preserve_characters_even_with_one_byte_limit(tmp_path):
    sessions = Sessions()
    executable = f'"{sys.executable}"' if sys.platform == "win32" else shlex.quote(sys.executable)
    started = await sessions.start(StartSession(
        command=executable + ' -c "import sys; sys.stdout.buffer.write(bytes.fromhex('
        "'e697a5e69cace8aa9ef09f9982'" + '))"', cwd=str(tmp_path),
    ))
    identity = started["session_id"]
    try:
        await asyncio.wait_for(sessions.get(identity).reader, 5)
        cursor, pieces = 0, []
        for _ in range(20):
            page = sessions.output(SessionOutput(session_id=identity, cursor=cursor, limit=1))
            if cursor == page["end_cursor"]:
                break
            assert page["next_cursor"] > cursor
            pieces.append(page["text"])
            cursor = page["next_cursor"]
        assert ''.join(pieces) == "日本語🙂"
        assert cursor == 13
    finally:
        await sessions.close()


async def test_tail_cursor_and_absolute_continuation(tmp_path):
    sessions = Sessions()
    executable = f'"{sys.executable}"' if sys.platform == "win32" else shlex.quote(sys.executable)
    started = await sessions.start(StartSession(
        command=executable + ' -c "print(1234567890)"', cwd=str(tmp_path),
    ))
    identity = started["session_id"]
    session = sessions.get(identity)
    try:
        await asyncio.wait_for(session.reader, 5)
        raw = bytes(session.output)
        tail = sessions.output(SessionOutput(session_id=identity, cursor=-5, limit=2))
        assert tail["text"] == raw[-5:-3].decode()
        assert tail["next_cursor"] == len(raw) - 3
        remainder = sessions.output(SessionOutput(
            session_id=identity, cursor=tail["next_cursor"],
        ))
        assert remainder["text"] == raw[-3:].decode()
        assert tail["dropped_bytes"] == 0
        all_output = sessions.output(SessionOutput(session_id=identity, cursor=-100000))
        assert all_output["text"] == raw.decode()
        assert all_output["dropped_bytes"] == 0
        # Model bounded-buffer eviction without generating 8 MiB of output.
        session.first_cursor = 4
        del session.output[:4]
        clipped = sessions.output(SessionOutput(session_id=identity, cursor=-100000))
        assert clipped["dropped_bytes"] == 4
        assert clipped["text"] == raw[4:].decode()
    finally:
        await sessions.close()


async def test_wait_output_wakes_on_input_and_does_not_stop_process_on_timeout(tmp_path):
    from anywhere_computer.models import SessionInput

    sessions = Sessions()
    executable = f'"{sys.executable}"' if sys.platform == "win32" else shlex.quote(sys.executable)
    started = await sessions.start(StartSession(
        command=executable + ' -u -c "print(input())"', cwd=str(tmp_path),
    ))
    identity = started["session_id"]
    try:
        empty = await sessions.wait_output(SessionOutput(session_id=identity, wait_ms=20))
        assert empty["text"] == "" and empty["state"] == "running"
        waiting = asyncio.create_task(sessions.wait_output(
            SessionOutput(session_id=identity, wait_ms=3000),
        ))
        await sessions.send(SessionInput(session_id=identity, text="delivered\n"))
        result = await asyncio.wait_for(waiting, 5)
        assert "delivered" in result["text"]
        await asyncio.wait_for(sessions.get(identity).reader, 5)
        ended = await asyncio.wait_for(sessions.wait_output(SessionOutput(
            session_id=identity, cursor=result["next_cursor"], wait_ms=30000,
        )), 1)
        assert ended["state"] == "exited"
    finally:
        await sessions.close()
