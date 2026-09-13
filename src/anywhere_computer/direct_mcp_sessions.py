"""Owner-scoped direct MCP sessions; no implicit restart after response loss."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from .direct_mcp import DirectMCPContext


@dataclass
class _Entry:
    owner: str | None = field(repr=False)
    context: DirectMCPContext = field(repr=False)
    state: str = 'opening'
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    idle_timeout: int = 300


class DirectMCPSessions:
    """Reserve capacity before spawning and retain unconfirmed cleanup failures."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.entries: dict[str, _Entry] = {}
        self._closed = False
        self.clock = clock
        self._reaper: asyncio.Task[None] | None = None

    @property
    def active_count(self) -> int:
        return sum(entry.state == 'opening' or not entry.context.cleanup_confirmed
                   for entry in self.entries.values())

    def _owned(self, session_id: str, owner: str | None) -> _Entry:
        entry = self.entries.get(session_id)
        if entry is None or entry.owner != owner:
            raise ValueError('Direct MCP session not found for this connection')
        return entry

    def status(self, session_id: str, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._owned(session_id, owner)
        return {'session_id': session_id, 'state': entry.state,
                'idle_timeout': entry.idle_timeout,
                'idle_seconds': max(0.0, self.clock() - entry.last_used),
                'busy': entry.lock.locked(), 'cleanup_confirmed': entry.context.cleanup_confirmed,
                'survives_client_disconnect': True, 'survives_engine_restart': False}

    async def _retire(self, entry: _Entry, state: str = 'closed') -> None:
        entry.state = state
        try:
            await entry.context.close()
        except RuntimeError:
            entry.state = 'cleanup_incomplete'

    async def open(self, command: list[str], cwd: Path, *, owner: str | None,
                   idle_timeout: int = 300,
                   ) -> dict[str, JsonValue]:
        if not 30 <= idle_timeout <= 1800:
            raise ValueError('Direct MCP idle timeout must be between 30 and 1800 seconds')
        if self._closed:
            raise RuntimeError('Direct MCP sessions are shutting down')
        if self.active_count >= 4:
            raise RuntimeError('Direct MCP capacity reached; close an existing session')
        # Only discard historical records whose process cleanup was confirmed.
        retired = [key for key, entry in self.entries.items()
                   if entry.state != 'opening' and entry.context.cleanup_confirmed
                   and not entry.lock.locked()]
        for key in retired[:-127]:
            del self.entries[key]
        entry = _Entry(owner, DirectMCPContext(command, cwd),
                       last_used=self.clock(), idle_timeout=idle_timeout)
        session_id = uuid.uuid4().hex
        self.entries[session_id] = entry
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._expire_loop())
        async with entry.lock:
            try:
                await entry.context.open()
                entry.state = 'open'
                entry.last_used = self.clock()
            except BaseException:
                await self._retire(entry)
                raise
        return self.status(session_id, owner=owner)

    @asynccontextmanager
    async def _lease(self, session_id: str, owner: str | None) -> AsyncIterator[_Entry]:
        entry = self._owned(session_id, owner)
        if entry.lock.locked():
            raise RuntimeError('Direct MCP session is busy; inspect the existing operation')
        async with entry.lock:
            if entry.state == 'open' and self.clock() - entry.last_used >= entry.idle_timeout:
                await self._retire(entry, 'expired')
            if entry.state != 'open':
                raise RuntimeError('Direct MCP session is closed; no automatic restart performed')
            try:
                yield entry
            except BaseException:
                await self._retire(entry)
                raise
            finally:
                entry.last_used = self.clock()

    async def expire_idle(self) -> None:
        for entry in list(self.entries.values()):
            if entry.lock.locked() or entry.state != 'open':
                continue
            async with entry.lock:
                if self.clock() - entry.last_used >= entry.idle_timeout:
                    await self._retire(entry, 'expired')

    async def _expire_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(5)
            await self.expire_idle()

    async def tools(self, session_id: str, *, owner: str | None,
                    cursor: str | None = None, summary: bool = False,
                    query: str | None = None, name: str | None = None) -> dict[str, JsonValue]:
        async with self._lease(session_id, owner) as entry:
            page = await entry.context.list_tools(cursor=cursor)
            if not summary and query is None and name is None:
                return page
            rows = page.get('tools')
            if not isinstance(rows, list):
                raise ValueError('Direct MCP catalog has no tools list')
            selected: list[JsonValue] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('Direct MCP catalog has an invalid tool')
                tool_name = row.get('name')
                description = row.get('description') or ''
                if not isinstance(tool_name, str) or not isinstance(description, str):
                    raise ValueError('Direct MCP catalog has invalid tool text')
                if name is not None and name != tool_name:
                    continue
                if query is not None and query.casefold() not in (
                    tool_name + '\n' + description
                ).casefold():
                    continue
                selected.append({
                    'name': tool_name, 'description': description[:160],
                    'description_truncated': len(description) > 160,
                } if summary else row)
            return {**page, 'tools': selected, 'filter_scope': 'current_page',
                    'received_tool_count': len(rows), 'matched_tool_count': len(selected)}

    async def call(self, session_id: str, name: str, arguments: dict[str, JsonValue], *,
                   owner: str | None) -> dict[str, JsonValue]:
        async with self._lease(session_id, owner) as entry:
            return await entry.context.call(name, arguments)

    async def stop(self, session_id: str, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._owned(session_id, owner)
        if entry.lock.locked():
            raise RuntimeError('Direct MCP session is busy; inspect the existing operation')
        async with entry.lock:
            await self._retire(entry)
        return self.status(session_id, owner=owner)

    async def close(self) -> None:
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        for entry in self.entries.values():
            async with entry.lock:
                await self._retire(entry)
