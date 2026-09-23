"""Agent-owned processes outlive individual MCP clients."""

import asyncio
import codecs
import errno
import os
import signal
import struct
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from pydantic import JsonValue

from .execution_environment import with_tool_path
from .files import absolute_path
from .models import SessionInput, SessionOutput, SessionResize, StartSession

if os.name != "nt":
    import fcntl
    import pty
    import termios

    # Windows mypy uses stdlib stubs without these POSIX attributes.
    _ioctl = getattr(fcntl, "ioctl")  # noqa: B009
    _openpty = getattr(pty, "openpty")  # noqa: B009
    _TIOCSCTTY = getattr(termios, "TIOCSCTTY")  # noqa: B009
    _TIOCSWINSZ = getattr(termios, "TIOCSWINSZ")  # noqa: B009

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
    master_fd: int | None = None
    interactive: bool = False
    rows: int | None = None
    columns: int | None = None


def _make_controlling_terminal() -> None:
    """Called in the child after start_new_session and before exec."""
    _ioctl(0, _TIOCSCTTY, 0)


async def _fd_ready(fd: int, *, writing: bool = False) -> None:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    register = loop.add_writer if writing else loop.add_reader
    unregister = loop.remove_writer if writing else loop.remove_reader
    register(fd, ready.set_result, None)
    try:
        await ready
    finally:
        unregister(fd)


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
        command = (sys.executable, "-I", str(Path(__file__).with_name("terminal_worker.py")),
                   shell, args.command)
        master_fd: int | None = None
        if args.interactive and os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).with_name("terminal_worker.py")),
                "--conpty", shell, args.command, str(args.rows), str(args.columns),
                cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=with_tool_path(os.environ),
            )
        elif args.interactive:
            master_fd, slave_fd = _openpty()
            try:
                _ioctl(slave_fd, _TIOCSWINSZ,
                            struct.pack("HHHH", args.rows, args.columns, 0, 0))
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=cwd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    start_new_session=True, preexec_fn=_make_controlling_terminal,
                    env=with_tool_path(os.environ),
                )
                os.set_blocking(master_fd, False)
            except BaseException:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
        else:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != "nt", env=with_tool_path(os.environ),
            )
        session = Session(uuid.uuid4().hex, process, time.time(), master_fd=master_fd,
                          interactive=args.interactive,
                          rows=args.rows if args.interactive else None,
                          columns=args.columns if args.interactive else None)
        self.sessions[session.session_id] = session
        session.reader = asyncio.create_task(self._read(session))
        return self.describe(session)

    async def _read(self, session: Session) -> None:
        try:
            if session.master_fd is None:
                assert session.process.stdout is not None
                while chunk := await session.process.stdout.read(16384):
                    self._append_output(session, chunk)
            else:
                while True:
                    await _fd_ready(session.master_fd)
                    try:
                        chunk = os.read(session.master_fd, 16384)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break  # Linux signals PTY EOF with EIO.
                        raise
                    if not chunk:
                        break
                    self._append_output(session, chunk)
        finally:
            session.output_eof = True
            if session.master_fd is not None:
                os.close(session.master_fd)
                session.master_fd = None
            await session.process.wait()

    @staticmethod
    def _append_output(session: Session, chunk: bytes) -> None:
        session.output.extend(chunk)
        overflow = len(session.output) - OUTPUT_CAP
        if overflow > 0:
            del session.output[:overflow]
            session.first_cursor += overflow

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
            "interactive": session.interactive,
            "rows": session.rows,
            "columns": session.columns,
        }

    async def send(self, args: SessionInput) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        try:
            await asyncio.wait_for(session.input_lock.acquire(), 10)
        except TimeoutError as error:
            raise ValueError("Session input is busy; no input was sent") from error
        try:
            if session.process.returncode is not None or (session.process.stdin is None
                                                        and session.master_fd is None):
                raise ValueError("Session is not accepting input")
            cursor = session.first_cursor + len(session.output)
            encoded = args.text.encode()
            try:
                if session.interactive and os.name == "nt":
                    assert session.process.stdin is not None
                    session.process.stdin.write(b"I" + len(encoded).to_bytes(4, "big") + encoded)
                    await asyncio.wait_for(session.process.stdin.drain(), 10)
                elif session.master_fd is None:
                    assert session.process.stdin is not None
                    session.process.stdin.write(encoded)
                    await asyncio.wait_for(session.process.stdin.drain(), 10)
                else:
                    await asyncio.wait_for(self._write_pty(session.master_fd, encoded), 10)
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

    @staticmethod
    async def _write_pty(fd: int, encoded: bytes) -> None:
        offset = 0
        while offset < len(encoded):
            try:
                offset += os.write(fd, encoded[offset:])
            except BlockingIOError:
                await _fd_ready(fd, writing=True)

    async def resize(self, args: SessionResize) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        if not session.interactive or session.process.returncode is not None:
            raise ValueError("Session is not an active interactive terminal")
        if os.name == "nt":
            assert session.process.stdin is not None
            payload = struct.pack("!HH", args.rows, args.columns)
            async with session.input_lock:
                session.process.stdin.write(b"R" + len(payload).to_bytes(4, "big") + payload)
                await asyncio.wait_for(session.process.stdin.drain(), 10)
        else:
            assert session.master_fd is not None
            _ioctl(session.master_fd, _TIOCSWINSZ,
                        struct.pack("HHHH", args.rows, args.columns, 0, 0))
        session.rows, session.columns = args.rows, args.columns
        return self.describe(session)

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
