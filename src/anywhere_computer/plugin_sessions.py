"""Owner-scoped, bounded lifetimes for stateful Codex MCP plugin contexts."""

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import JsonValue

from . import codex_plugins
from .codex_plugins import PluginCallOutcomeUnknown, PluginPreflightError

MAX_SESSIONS = 4
MAX_RETIRED = 128
IDLE_TIMEOUT = 300
REAPER_INTERVAL = 5.0
ACTIVITY_PROBE_INTERVAL = 30.0
ACTIVITY_PROBE_TIMEOUT = 15.0
SESSION_TOOLS = frozenset({
    "codex_plugin_session_open", "codex_plugin_session_status", "codex_plugin_session_close",
})
SessionState = Literal["opening", "open", "closed", "expired", "unusable"]


@dataclass
class _Entry:
    session_id: str
    owner: str | None = field(repr=False)
    context: codex_plugins.PluginContext = field(repr=False)
    idle_timeout: int
    last_used: float
    created: float = field(default_factory=time.time)
    state: SessionState = "opening"
    cleanup_confirmed: bool = False
    background_servers: set[tuple[str, str]] = field(default_factory=set)
    activity_digests: dict[tuple[str, str], str] = field(default_factory=dict)
    next_activity_probe: float = 0.0
    activity_probe_failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class PluginSessions:
    """A session is a resource, not permission to call tools or to retry effects.

    Owners come from the authenticated transport, never from tool arguments.
    The pool survives MCP client disconnects, not engine restarts. A lost runtime
    is never silently replaced with a new, empty plugin state.
    """

    def __init__(
        self, *, max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_sessions <= MAX_SESSIONS:
            raise ValueError("Plugin session capacity must be between 1 and 4")
        self.max_sessions = max_sessions
        self.clock = clock
        self.entries: dict[str, _Entry] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    def _owned(self, session_id: str, owner: str | None) -> _Entry:
        if re.fullmatch(r"[a-f0-9]{32}", session_id) is None:
            raise PluginPreflightError("invalid_session_id", "Use the ID returned by session_open")
        entry = self.entries.get(session_id)
        if entry is None or entry.owner != owner:
            raise PluginPreflightError(
                "session_not_found",
                "Use a session opened by this connection; engine restarts require a new session",
            )
        return entry

    def _describe(self, entry: _Entry) -> dict[str, JsonValue]:
        return {
            "session_id": entry.session_id, "cwd": entry.context.cwd,
            "state": "busy" if entry.lock.locked() and entry.state == "open" else entry.state,
            "created": entry.created, "idle_timeout": entry.idle_timeout,
            "idle_seconds": max(0.0, self.clock() - entry.last_used),
            "cleanup_confirmed": entry.cleanup_confirmed,
            "background_activity_protected": bool(entry.background_servers),
            "background_activity_probe_failures": entry.activity_probe_failures,
            "survives_client_disconnect": True, "survives_engine_restart": False,
            "inference_requested": False,
        }

    def _prune(self) -> None:
        retired = [key for key, entry in self.entries.items()
                   if entry.cleanup_confirmed and not entry.lock.locked()]
        for key in retired[:-MAX_RETIRED]:
            del self.entries[key]

    async def _retire(self, entry: _Entry, state: SessionState) -> None:
        entry.state = state
        try:
            await asyncio.wait_for(entry.context.close(), timeout=4)
        except (TimeoutError, OSError, ValueError, RuntimeError):
            # Keep a failed cleanup in the capacity budget; do not hide a live child.
            pass
        finally:
            entry.cleanup_confirmed = not entry.context.alive

    async def _refresh(self, entry: _Entry) -> None:
        if entry.state != "open":
            return
        if not entry.context.alive:
            await self._retire(entry, "unusable")
        elif self.clock() - entry.last_used >= entry.idle_timeout:
            if entry.background_servers:
                now = self.clock()
                if now < entry.next_activity_probe:
                    return
                try:
                    active = await asyncio.wait_for(
                        self._subchat_active(entry), timeout=ACTIVITY_PROBE_TIMEOUT,
                    )
                except (TimeoutError, OSError, ValueError, RuntimeError):
                    if not entry.context.alive:
                        await self._retire(entry, "unusable")
                        return
                    # Uncertain activity must not kill an in-flight generation.
                    entry.activity_probe_failures += 1
                    entry.next_activity_probe = now + ACTIVITY_PROBE_INTERVAL
                    return
                entry.activity_probe_failures = 0
                if active:
                    entry.next_activity_probe = now + ACTIVITY_PROBE_INTERVAL
                    return
            await self._retire(entry, "expired")

    async def _activity_digest(self, entry: _Entry, server: str,
                               activity_tool: str) -> str:
        key = (server, activity_tool)
        cached = entry.activity_digests.get(key)
        if cached is not None:
            return cached
        catalog = await entry.context.inspect(server=server, tool=activity_tool)
        rows = catalog.get("servers")
        if not isinstance(rows, list):
            raise PluginPreflightError(
                "plugin_activity_unavailable", "Update the Subchat plugin before using "
                "stateful calls through this bridge",
            )
        selected = next((row for row in rows if isinstance(row, dict)
                         and row.get("server") == server), None)
        tools = selected.get("tools") if selected is not None else None
        descriptor = (next((tool for tool in tools if isinstance(tool, dict)
                            and tool.get("name") == activity_tool), None)
                      if isinstance(tools, list) else None)
        digest = descriptor.get("catalog_sha256") if descriptor is not None else None
        if not isinstance(digest, str):
            raise PluginPreflightError(
                "plugin_activity_unavailable", "Update the Subchat plugin before using "
                "stateful calls through this bridge",
            )
        entry.activity_digests[key] = digest
        return digest

    async def _subchat_active(self, entry: _Entry) -> bool:
        for server, activity_tool in entry.background_servers:
            digest = await self._activity_digest(entry, server, activity_tool)
            result = await entry.context.call(server, activity_tool, {}, digest)
            content = result.get("structured_content")
            data = content.get("data") if isinstance(content, dict) else None
            count = data.get("active_count") if isinstance(data, dict) else None
            if (result.get("is_error") is True or not isinstance(content, dict)
                    or content.get("state") != "completed" or not isinstance(data, dict)
                    or not isinstance(count, int)
                    or isinstance(count, bool) or count < 0):
                raise ValueError("Subchat activity result is malformed")
            if count:
                return True
        return False

    async def open(
        self, cwd: str, *, owner: str | None, idle_timeout: int = IDLE_TIMEOUT,
    ) -> dict[str, JsonValue]:
        if self._closed:
            raise PluginPreflightError("sessions_closed", "The engine is shutting down")
        if not 30 <= idle_timeout <= 1800:
            raise ValueError("Plugin session idle timeout must be between 30 and 1800 seconds")
        await self.expire_idle()
        if self._closed:
            raise PluginPreflightError("sessions_closed", "The engine is shutting down")
        if sum(not entry.cleanup_confirmed for entry in self.entries.values()) >= self.max_sessions:
            raise PluginPreflightError("session_capacity", "Close an existing plugin session first")
        context = codex_plugins.PluginContext(cwd)
        entry = _Entry(uuid.uuid4().hex, owner, context, idle_timeout, self.clock())
        # Reserve capacity before the first await so concurrent opens cannot exceed it.
        self.entries[entry.session_id] = entry
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._expire_loop())
        async with entry.lock:
            try:
                await context.open()
                entry.state = "open"
                entry.last_used = self.clock()
            except asyncio.CancelledError:
                await self._retire(entry, "unusable")
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                await self._retire(entry, "unusable")
                raise PluginPreflightError(
                    "session_start_failed", "Check the installed Codex runtime and local MCP setup",
                    details={"session_id": entry.session_id,
                             "cleanup_confirmed": entry.cleanup_confirmed,
                             **(error.details if isinstance(error, PluginPreflightError) else {})},
                ) from error
        return self._describe(entry)

    @asynccontextmanager
    async def _lease(
        self, session_id: str, *, owner: str | None, cwd: str,
    ) -> AsyncIterator[_Entry]:
        entry = self._owned(session_id, owner)
        if codex_plugins._cwd(cwd) != entry.context.cwd:
            raise PluginPreflightError(
                "session_workspace_mismatch", "Use the workspace used when opening this session",
            )
        if entry.lock.locked():
            raise PluginPreflightError(
                "session_busy", "Inspect the active operation before retrying",
            )
        async with entry.lock:
            await self._refresh(entry)
            if entry.state != "open":
                raise PluginPreflightError(
                    f"session_{entry.state}",
                    "This session cannot continue; inspect uncertain operations "
                    "before opening another",
                )
            try:
                yield entry
            except PluginCallOutcomeUnknown:
                await self._retire(entry, "unusable")
                raise
            except PluginPreflightError as error:
                if error.code == "plugin_catalog_failed":
                    # Transport/catalog failure can leave unread RPC responses behind.
                    # Preserve stale-selection errors, but do not reuse a broken context.
                    await self._retire(entry, "unusable")
                raise
            except asyncio.CancelledError:
                await self._retire(entry, "unusable")
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                await self._retire(entry, "unusable")
                raise PluginPreflightError(
                    "session_unusable", "Plugin context failed before dispatch; open a new session",
                ) from error
            finally:
                if entry.state == "open":
                    entry.last_used = self.clock()

    async def inspect(
        self, session_id: str, *, owner: str | None, cwd: str,
        limit: int = codex_plugins.MAX_CATALOG, cursor: str | None = None,
        server: str | None = None, tool: str | None = None,
        query: str | None = None, summary: bool = False,
    ) -> dict[str, JsonValue]:
        codex_plugins._validate_inspection(limit, server, tool)
        codex_plugins._cursor(cursor)
        async with self._lease(session_id, owner=owner, cwd=cwd) as entry:
            result = await entry.context.inspect(
                limit=limit, cursor=cursor, server=server, tool=tool, query=query, summary=summary,
            )
            result["session_id"] = session_id
            # The shared inspector has already validated these catalog objects.
            for row in cast(list[dict[str, JsonValue]], result["servers"]):
                cast(dict[str, JsonValue], row["inspect_arguments"])["session_id"] = session_id
                for descriptor in cast(list[dict[str, JsonValue]], row["tools"]):
                    call_arguments = cast(dict[str, JsonValue], descriptor["call_arguments"])
                    call_arguments["session_id"] = session_id
            return result

    async def call(
        self, session_id: str, *, owner: str | None, cwd: str,
        server: str, tool: str, arguments: dict[str, JsonValue], catalog_sha256: str,
    ) -> dict[str, JsonValue]:
        codex_plugins._validate_call(server, tool, catalog_sha256)
        async with self._lease(session_id, owner=owner, cwd=cwd) as entry:
            activity_tool = codex_plugins._subchat_activity_tool(tool)
            if activity_tool is not None:
                await self._activity_digest(entry, server, activity_tool)
                # A wait can start a background recovery, even when this
                # session did not send the original operation.
                entry.background_servers.add((server, activity_tool))
            result = await entry.context.call(server, tool, arguments, catalog_sha256)
            return {**result, "session_id": session_id}

    async def status(self, session_id: str, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._owned(session_id, owner)
        if not entry.lock.locked():
            async with entry.lock:
                await self._refresh(entry)
        return self._describe(entry)

    async def stop(self, session_id: str, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._owned(session_id, owner)
        if entry.lock.locked():
            raise PluginPreflightError(
                "session_busy", "Do not terminate a session with an active call",
            )
        async with entry.lock:
            if entry.state == "open" and not entry.context.alive:
                await self._retire(entry, "unusable")
            if entry.state == "open" and entry.background_servers:
                try:
                    active = await asyncio.wait_for(
                        self._subchat_active(entry), timeout=ACTIVITY_PROBE_TIMEOUT,
                    )
                except (TimeoutError, OSError, ValueError, RuntimeError) as error:
                    if not entry.context.alive:
                        await self._retire(entry, "unusable")
                        return self._describe(entry)
                    entry.activity_probe_failures += 1
                    raise PluginPreflightError(
                        "session_busy", "Subchat activity could not be checked; keep this "
                        "session open and inspect the pending operation before closing it",
                    ) from error
                entry.activity_probe_failures = 0
                if active:
                    raise PluginPreflightError(
                        "session_busy", "Subchat background work is still active; wait for "
                        "it to finish before closing this session",
                    )
            if entry.state in {"opening", "open"} or not entry.cleanup_confirmed:
                await self._retire(entry, "closed")
        return self._describe(entry)

    async def expire_idle(self) -> None:
        for entry in list(self.entries.values()):
            if not entry.lock.locked():
                async with entry.lock:
                    await self._refresh(entry)
        self._prune()

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL)
            await self.expire_idle()

    async def close(self) -> None:
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        for entry in list(self.entries.values()):
            async with entry.lock:
                if not entry.cleanup_confirmed:
                    await self._retire(entry, "closed")
        self.entries.clear()
