"""Agent-owned processes outlive individual MCP clients."""

import asyncio
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


@dataclass
class Session:
    session_id: str
    process: asyncio.subprocess.Process
    created: float
    output: bytearray = field(default_factory=bytearray)
    first_cursor: int = 0
    reader: asyncio.Task[None] | None = None


class Sessions:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    async def start(self, args: StartSession) -> dict[str, JsonValue]:
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
        # cmd.exe uses its own command-line quoting, not argv quoting.
        # asyncio's shell transport constructs /c "..." correctly on Windows.
        if sys.platform == "win32" and Path(shell).name.lower() == "cmd.exe":
            process = await asyncio.create_subprocess_shell(
                args.command,
                executable=shell,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                shell,
                "-c",
                args.command,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != "nt",
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
            "started": session.created,
            "first_cursor": session.first_cursor,
            "end_cursor": session.first_cursor + len(session.output),
        }

    async def send(self, args: SessionInput) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        if session.process.returncode is not None or session.process.stdin is None:
            raise ValueError("Session is not accepting input")
        session.process.stdin.write(args.text.encode())
        await asyncio.wait_for(session.process.stdin.drain(), 10)
        return {"session_id": args.session_id, "bytes_sent": len(args.text.encode())}

    def output(self, args: SessionOutput) -> dict[str, JsonValue]:
        session = self.get(args.session_id)
        start = max(args.cursor, session.first_cursor)
        position = start - session.first_cursor
        chunk = bytes(session.output[position : position + args.limit])
        return {
            **self.describe(session),
            "text": chunk.decode("utf-8", errors="replace"),
            "next_cursor": start + len(chunk),
            "dropped_bytes": max(0, start - args.cursor),
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
