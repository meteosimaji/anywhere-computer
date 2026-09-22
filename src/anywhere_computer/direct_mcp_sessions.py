"""Owner-scoped direct MCP sessions; no implicit restart after response loss."""

import asyncio
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    watches: dict[str, '_Watch'] = field(default_factory=dict)


@dataclass
class _Watch:
    deadline: float
    state: str = 'watching'
    reason: str | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class DirectMCPSessions:
    """Reserve capacity before spawning and retain unconfirmed cleanup failures."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 journal: sqlite3.Connection | None = None) -> None:
        self.entries: dict[str, _Entry] = {}
        self._closed = False
        self.clock = clock
        self._reaper: asyncio.Task[None] | None = None
        self.journal = journal
        if journal is not None:
            with journal:
                journal.execute('CREATE TABLE IF NOT EXISTS subchat_queue_watch_leases ('
                                'session_id TEXT NOT NULL, operation_id TEXT NOT NULL, '
                                'owner TEXT, state TEXT NOT NULL, reason TEXT, '
                                'expires_at REAL NOT NULL, PRIMARY KEY(session_id, operation_id))')
                journal.execute("UPDATE subchat_queue_watch_leases SET state='stopped', "
                                "reason='engine_restart' WHERE state='watching'")

    def _record_watch(self, session_id: str, entry: _Entry, identity: str,
                      watch: _Watch) -> None:
        if self.journal is None:
            return
        with self.journal:
            self.journal.execute('INSERT INTO subchat_queue_watch_leases '
                                 '(session_id, operation_id, owner, state, reason, expires_at) '
                                 'VALUES (?,?,?,?,?,?) ON CONFLICT(session_id, operation_id) '
                                 'DO UPDATE SET state=excluded.state, reason=excluded.reason, '
                                 'expires_at=excluded.expires_at',
                                 (session_id, identity, entry.owner, watch.state, watch.reason,
                                  time.time() + max(0, watch.deadline - self.clock())))

    def watch_history(self, *, owner: str | None) -> list[dict[str, JsonValue]]:
        if self.journal is None:
            return []
        rows = self.journal.execute('SELECT session_id, operation_id, state, reason, expires_at '
                                    'FROM subchat_queue_watch_leases WHERE owner IS ? '
                                    'ORDER BY expires_at DESC LIMIT 100', (owner,)).fetchall()
        return [{'session_id': sid, 'operation_id': identity, 'state': state,
                 'reason': reason, 'expires_at': expiry}
                for sid, identity, state, reason, expiry in rows]

    @property
    def active_count(self) -> int:
        return sum(entry.state == 'opening' or not entry.context.cleanup_confirmed
                   for entry in self.entries.values())

    @property
    def active_watch_count(self) -> int:
        return sum(watch.state == 'watching' for entry in self.entries.values()
                   for watch in entry.watches.values())

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
                'survives_client_disconnect': True, 'survives_engine_restart': False,
                'queue_watches': {identity: {'state': watch.state, 'reason': watch.reason,
                                  'lease_remaining_seconds': max(0, watch.deadline - self.clock())}
                                  for identity, watch in entry.watches.items()}}

    async def _retire(self, entry: _Entry, state: str = 'closed',
                      session_id: str | None = None) -> None:
        entry.state = state
        for identity, watch in entry.watches.items():
            if watch.state == 'watching':
                watch.state = 'stopped'
                watch.reason = ('lease_expired' if self.clock() >= watch.deadline else
                                'engine_shutdown' if self._closed else state)
                watch.stop.set()
                if session_id is not None:
                    self._record_watch(session_id, entry, identity, watch)
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
                await self._retire(entry, session_id=session_id)
                raise
        return self.status(session_id, owner=owner)

    @asynccontextmanager
    async def _lease(self, session_id: str, owner: str | None) -> AsyncIterator[_Entry]:
        entry = self._owned(session_id, owner)
        if entry.lock.locked():
            raise RuntimeError('Direct MCP session is busy; inspect the existing operation')
        async with entry.lock:
            if (entry.state == 'open' and self.clock() - entry.last_used >= entry.idle_timeout
                    and not any(watch.state == 'watching' and
                                self.clock() < watch.deadline
                                for watch in entry.watches.values())):
                await self._retire(entry, 'expired', session_id=session_id)
            if entry.state != 'open':
                raise RuntimeError('Direct MCP session is closed; no automatic restart performed')
            try:
                yield entry
            except BaseException:
                await self._retire(entry, session_id=session_id)
                raise
            finally:
                entry.last_used = self.clock()

    async def expire_idle(self) -> None:
        for session_id, entry in list(self.entries.items()):
            if entry.lock.locked() or entry.state != 'open':
                continue
            async with entry.lock:
                if (self.clock() - entry.last_used >= entry.idle_timeout
                        and not any(watch.state == 'watching' and
                                    self.clock() < watch.deadline
                                    for watch in entry.watches.values())):
                    await self._retire(entry, 'expired', session_id=session_id)

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
            result = await entry.context.call(name, arguments)
            if name == 'subchat_queue_watch':
                self._sync_watch(session_id, entry, arguments, result)
            return result

    @staticmethod
    def _watch_reply(result: dict[str, JsonValue]) -> dict[str, Any] | None:
        structured = result.get('structuredContent')
        if result.get('isError') or not isinstance(structured, dict):
            return None
        if structured.get('state') != 'completed':
            return None
        data = structured.get('data')
        return data if isinstance(data, dict) else None

    def _sync_watch(self, session_id: str, entry: _Entry, arguments: dict[str, JsonValue],
                    result: dict[str, JsonValue]) -> None:
        data = self._watch_reply(result)
        identity = arguments.get('operation_id')
        if data is None or not isinstance(identity, str) or len(identity) != 32:
            return
        if data.get('submission_operation_id') != identity:
            return
        old = entry.watches.get(identity)
        if arguments.get('enabled', True) is False:
            if old is not None and old.state == 'watching':
                old.state, old.reason = 'stopped', 'explicit_cancel'
                old.stop.set()
                self._record_watch(session_id, entry, identity, old)
            return
        if data.get('state') != 'watching' or old is not None and old.state == 'watching':
            return
        lease = arguments.get('lease_seconds', 900)
        if not isinstance(lease, int) or isinstance(lease, bool) or not 30 <= lease <= 1800:
            return
        watch = _Watch(deadline=self.clock() + lease)
        entry.watches[identity] = watch
        self._record_watch(session_id, entry, identity, watch)
        watch.task = asyncio.create_task(self._watch_loop(session_id, entry, identity, watch))

    async def _watch_loop(self, session_id: str, entry: _Entry, identity: str,
                          watch: _Watch) -> None:
        try:
            while watch.state == 'watching' and entry.state == 'open':
                remaining = watch.deadline - self.clock()
                if remaining <= 0:
                    watch.state, watch.reason = 'stopped', 'lease_expired'
                    async with entry.lock:
                        if entry.state == 'open':
                            await entry.context.call('subchat_queue_watch', {
                                'operation_id': identity, 'enabled': False})
                    return
                try:
                    await asyncio.wait_for(watch.stop.wait(), timeout=min(5, remaining))
                    return
                except TimeoutError:
                    pass
                async with entry.lock:
                    if entry.state != 'open' or watch.state != 'watching':
                        return
                    result = await entry.context.call('subchat_status', {
                        'operation_id': identity})
                    data = self._watch_reply(result)
                    if data is None:
                        watch.state, watch.reason = 'stopped', 'status_failed'
                        return
                    observed = data.get('queue_watch')
                    if isinstance(observed, dict) and observed.get('state') == 'stopped':
                        watch.state, watch.reason = 'stopped', str(observed.get('reason'))
                        return
                    if data.get('state') not in {'queued', 'sending', 'submitted'}:
                        watch.state, watch.reason = 'stopped', 'queue_left'
                        return
        except asyncio.CancelledError:
            watch.state, watch.reason = 'stopped', 'engine_shutdown'
            raise
        except Exception:
            watch.state, watch.reason = 'stopped', 'observation_failed'
        finally:
            self._record_watch(session_id, entry, identity, watch)

    async def stop(self, session_id: str, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._owned(session_id, owner)
        if entry.lock.locked():
            raise RuntimeError('Direct MCP session is busy; inspect the existing operation')
        async with entry.lock:
            await self._retire(entry, session_id=session_id)
        return self.status(session_id, owner=owner)

    async def close(self) -> None:
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        for session_id, entry in self.entries.items():
            async with entry.lock:
                await self._retire(entry, session_id=session_id)
        tasks = [watch.task for entry in self.entries.values() for watch in entry.watches.values()
                 if watch.task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
