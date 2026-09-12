"""A direct stdio MCP context with an owning task for the SDK's stream lifetime."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue

from .bounded_mcp_stdio import StdioLifecycle

if TYPE_CHECKING:
    from mcp import ClientSession


class DirectMCPOutcomeUnknown(RuntimeError):
    """The request may have executed; callers must recover or inspect, not replay."""

    def __init__(self, message: str, *, failure_kind: str = 'response_invalid_or_lost') -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


class DirectMCPContext:
    """Connect to an installed server without Codex or a model-sampling callback.

    This is an internal transport, not yet a public tool or an installation API.
    Context entry and exit stay in one task, even when calls arrive from different
    durable operation tasks. Closing never creates a replacement server.
    """

    def __init__(self, command: list[str], cwd: Path) -> None:
        if (not command or len(command) > 128 or not Path(command[0]).is_absolute()
                or not Path(command[0]).is_file() or not os.access(command[0], os.X_OK)
                or any(len(value) > 4096 or '\x00' in value for value in command)):
            raise ValueError('Direct MCP requires an absolute executable and bounded argv')
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError('Direct MCP requires an existing absolute working directory')
        self.command = list(command)
        self.cwd = cwd
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._failed = False
        self._lifecycle = StdioLifecycle()

    @property
    def cleanup_confirmed(self) -> bool:
        """Whether the owning task ended and transport cleanup was confirmed."""
        return ((self._task is None or self._task.done())
                and self._lifecycle.cleanup_confirmed)

    async def _run(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters

            from .bounded_mcp_stdio import bounded_stdio

            async with bounded_stdio(StdioServerParameters(
                command=self.command[0], args=self.command[1:], cwd=str(self.cwd),
            ), lifecycle=self._lifecycle) as streams:
                async with ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception:
            # SDK/peer exceptions can contain arguments or credentials. Preserve
            # the failed state, never expose their free-form body to the caller.
            self._failed = True
        finally:
            self._session = None
            self._ready.set()

    async def open(self) -> None:
        if self._task is not None:
            raise RuntimeError('Direct MCP context has already been started')
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=35)
            if self._session is None:
                await self._task
                raise RuntimeError('Direct MCP server did not initialize')
        except BaseException:
            await self.close()
            raise

    def _connected(self) -> 'ClientSession':
        if (self._stop.is_set() or self._failed or self._session is None
                or self._task is None or self._task.done()):
            raise RuntimeError('Direct MCP is not connected; no automatic restart performed')
        return self._session

    async def list_tools(self, cursor: str | None = None) -> dict[str, JsonValue]:
        async with self._lock:
            session = self._connected()
            try:
                result = await session.list_tools(cursor=cursor)
            except Exception:
                await self._shutdown()
                raise RuntimeError('Direct MCP catalog request failed; session retired') from None
            return cast(dict[str, JsonValue], result.model_dump(mode='json', by_alias=True))

    async def call(self, name: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        async with self._lock:
            session = self._connected()
            try:
                result = await session.call_tool(name, arguments)
            except asyncio.CancelledError:
                self._stop.set()
                raise
            except Exception as error:
                from mcp.shared.exceptions import McpError

                timed_out = isinstance(error, TimeoutError) or (
                    isinstance(error, McpError) and error.error.code == 408
                )
                try:
                    await self._shutdown()
                except RuntimeError:
                    pass  # The original operation still has an unknown outcome.
                raise DirectMCPOutcomeUnknown(
                    'Direct MCP response was lost or invalid; outcome is unknown. '
                    'Inspect the operation and target before making another call',
                    failure_kind='timeout' if timed_out else
                        'response_invalid_or_lost',
                ) from None
            return cast(dict[str, JsonValue], result.model_dump(mode='json', by_alias=True))

    async def _shutdown(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except TimeoutError:
                raise RuntimeError(
                    'Direct MCP cleanup is incomplete; session remains retired',
                ) from None
            if not self.cleanup_confirmed:
                raise RuntimeError('Direct MCP cleanup is incomplete; session remains retired')

    async def close(self) -> None:
        async with self._lock:
            await self._shutdown()
