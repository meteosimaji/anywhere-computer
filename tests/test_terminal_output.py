import asyncio
import shlex
import sys

from anywhere_computer.models import SessionOutput, StartSession
from anywhere_computer.sessions import Sessions


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
