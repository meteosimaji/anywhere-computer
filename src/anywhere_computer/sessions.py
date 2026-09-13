"""Agent-owned processes outlive individual MCP clients."""

import asyncio
import codecs
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from pydantic import JsonValue

from .files import absolute_path
from .models import SessionInput, SessionOutput, StartSession

OUTPUT_CAP = 8 * 1024 * 1024


class TerminalInputOutcomeUnknown(RuntimeError):
    """Input may have reached the child before delivery or observation failed."""

    def __init__(self, *, bytes_attempted: int, failure_kind: str) -> None:
        super().__init__(
            "Terminal input may have been delivered; outcome is unknown. "
            "Recover the operation and inspect terminal output before another input; "
            "do not automatically resend it"
        )
        self.bytes_attempted = bytes_attempted
        self.failure_kind = failure_kind


@dataclass
class Session:
    session_id: str
    process: asyncio.subprocess.Process
    created: float
    output: bytearray = field(default_factory=bytearray)
    first_cursor: int = 0
    reader: asyncio.Task[None] | None = None
    output_eof: bool = False
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Sessions:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._start_lock = asyncio.Lock()

    async def start(self, args: StartSession) -> dict[str, JsonValue]:
        # Admission and registration must be atomic across the spawning await.
        # Only process creation is serialized, not running terminal sessions.
        async with self._start_lock:
            return await self._start(args)

    async def _start(self, args: StartSession) -> dict[str, JsonValue]:
        if sum(item.process.returncode is None for item in self.sessions.values()) >= 32:
            raise ValueError("32 active sessions; stop one before starting another")
        if len(self.sessions) >= 128:
            for key in list(self.sessions):
                if self.sessions[key].process.returncode is not None:
                    del self.sessions[key]
                    break
        cwd = absolute_path(args.cwd)
        if not cwd.is_dir():
            raise ValueError("Working directory does not exist")
        shell = args.shell or (
            os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe")
            if os.name == "nt"
            else os.environ.get("SHELL", "/bin/sh")
        )
        if not Path(shell).is_absolute():
            raise ValueError("Shell must be an absolute executable path")
        # A persistent group/Job owner outlives short-lived shells and their children.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("terminal_worker.py")),
            shell, args.command, cwd=cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != "nt",
        )
        session = Session(uuid.uuid4().hex, process, time.time())
        self.sessions[session.session_id] = session
        session.reader = asyncio.create_task(self._read(session))
        return self.describe(session)

    async def _read(self, session: Session) -> None:
        assert session.process.stdout is not None
        while chunk := await session.process.stdout.read(16384):
            session.output.extend(chunk)
            overflow = len(session.output) - OUTPUT_CAP
            if overflow > 0:
                del session.output[:overflow]
                session.first_cursor += overflow
        session.output_eof = True
        await session.process.wait()

    def get(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            raise ValueError("Session not found on this agent instance")
        return self.sessions[session_id]

    def describe(self, session: Session) -> dict[str, JsonValue]:
        return {
            "session_id": session.session_id,
            "pid": session.process.pid,
            "state": "running" if session.process.returncode is None else "exited",
            "exit_code": session.process.returncode,
            "output_eof": session.output_eof,
            "started": session.created,
            "first_cursor": session.first_cursor,
            "end_cursor": session.first_cursor + len(session.output),
        }

    async def send(self, args: SessionInput) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        try:
            await asyncio.wait_for(session.input_lock.acquire(), 10)
        except TimeoutError as error:
            raise ValueError("Session input is busy; no input was sent") from error
        try:
            if session.process.returncode is not None or session.process.stdin is None:
                raise ValueError("Session is not accepting input")
            cursor = session.first_cursor + len(session.output)
            encoded = args.text.encode()
            try:
                session.process.stdin.write(encoded)
                await asyncio.wait_for(session.process.stdin.drain(), 10)
                sent: dict[str, JsonValue] = {
                    "session_id": args.session_id, "bytes_sent": len(encoded),
                }
                if not args.wait_ms and args.wait_for_prompt is None:
                    return sent
                return {**sent, **await self._wait_response(session, args, cursor)}
            except Exception as error:
                # write/drain failure cannot prove that no bytes reached the child.
                # The same applies to observation errors after delivery. Preserve
                # the operation ID and avoid exposing free-form stream errors.
                raise TerminalInputOutcomeUnknown(
                    bytes_attempted=len(encoded),
                    failure_kind="timeout" if isinstance(error, TimeoutError)
                    else "input_or_observation_failed",
                ) from None
        finally:
            session.input_lock.release()

    async def _wait_response(
        self, session: Session, args: SessionInput, cursor: int,
    ) -> dict[str, JsonValue]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + args.wait_ms / 1000
        prompt = args.wait_for_prompt.encode() if args.wait_for_prompt is not None else None
        while True:
            page = self.output(SessionOutput(
                session_id=args.session_id, cursor=cursor, limit=args.output_limit,
            ))
            position = max(0, cursor - session.first_cursor)
            raw = bytes(session.output[position:position + args.output_limit])
            # Match bytes across reader chunks, before decoding potentially split UTF-8.
            matched = prompt is not None and prompt in raw
            remaining = deadline - loop.time()
            reason = None
            if page["dropped_bytes"]:
                reason = "output_dropped"
            elif matched:
                reason = "prompt"
            elif prompt is None and raw:
                reason = "output"
            elif len(raw) >= args.output_limit:
                reason = "output_limit"
            elif session.reader is not None and session.reader.done():
                reason = "exited" if session.process.returncode is not None else "output_closed"
            elif remaining <= 0:
                reason = "timeout"
            if reason is not None:
                return {
                    **page, "start_cursor": cursor, "wait_reason": reason,
                    "prompt_matched": matched, "elapsed_ms": round((loop.time() - started) * 1000),
                }
            await asyncio.sleep(min(0.02, remaining))

    async def wait_output(self, args: SessionOutput) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        deadline = asyncio.get_running_loop().time() + args.wait_ms / 1000
        while True:
            result = self.output(args)
            remaining = deadline - asyncio.get_running_loop().time()
            if (result["text"] or remaining <= 0
                    or (session.reader is not None and session.reader.done())):
                return result
            await asyncio.sleep(min(0.02, remaining))

    def output(self, args: SessionOutput) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        end = session.first_cursor + len(session.output)
        requested = end + args.cursor if args.cursor < 0 else args.cursor
        start = max(requested, session.first_cursor)
        position = start - session.first_cursor
        chunk = bytes(session.output[position : position + args.limit])
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(chunk, final=session.output_eof and position + len(chunk)
                              >= len(session.output))
        # A tiny page may need up to three extra bytes to return one whole code point.
        # Otherwise leave the incomplete suffix for the next cursor/poll.
        while not text and decoder.getstate()[0] and len(chunk) < args.limit + 3:
            index = position + len(chunk)
            if index >= len(session.output):
                break
            extra = bytes(session.output[index:index + 1])
            chunk += extra
            text += decoder.decode(extra, final=session.output_eof
                                   and index + 1 == len(session.output))
        consumed = len(chunk) - len(decoder.getstate()[0])
        return {
            **self.describe(session),
            "text": text,
            "next_cursor": start + consumed,
            "dropped_bytes": max(0, start - max(0, requested)),
        }

    async def stop(self, session_id: str) -> dict[str, JsonValue]:
        session = self.get(session_id)
        if session.process.returncode is None:
            try:
                if sys.platform != "win32":
                    os.killpg(session.process.pid, signal.SIGTERM)
                else:
                    await asyncio.to_thread(self._terminate_tree, session.process.pid)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(session.process.wait(), 3)
            except TimeoutError:
                try:
                    if sys.platform != "win32":
                        os.killpg(session.process.pid, signal.SIGKILL)
                    else:
                        session.process.kill()
                except ProcessLookupError:
                    pass
                await session.process.wait()
        if session.reader:
            try:
                await asyncio.wait_for(asyncio.shield(session.reader), 2)
            except TimeoutError:
                session.reader.cancel()
        return self.describe(session)

    @staticmethod
    def _terminate_tree(pid: int) -> None:
        try:
            parent = psutil.Process(pid)
            descendants = parent.children(recursive=True)
            for process in reversed(descendants):
                try:
                    process.terminate()
                except psutil.NoSuchProcess:
                    pass
            parent.terminate()
            _, alive = psutil.wait_procs([*descendants, parent], timeout=2)
            for process in alive:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(key) for key in self.sessions))
