import asyncio
import sys

import pytest

from anywhere_computer.direct_mcp import DirectMCPContext, DirectMCPOutcomeUnknown
from anywhere_computer.engine import Engine
from anywhere_computer.models import Empty, Request


async def test_real_stdio_server_retains_state_across_operation_tasks(tmp_path):
    server = tmp_path / 'server.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
server = FastMCP('synthetic-direct-mcp')
count = 0
@server.tool()
def increment(amount: int) -> int:
    global count
    count += amount
    return count
server.run(transport='stdio')
''', encoding='utf-8')
    context = DirectMCPContext([sys.executable, '-I', str(server)], tmp_path)
    await asyncio.create_task(context.open())
    assert not context.cleanup_confirmed
    try:
        catalog = await asyncio.create_task(context.list_tools())
        assert catalog['tools'][0]['name'] == 'increment'
        first = await asyncio.create_task(context.call('increment', {'amount': 40}))
        second = await asyncio.create_task(context.call('increment', {'amount': 2}))
        assert first['content'][0]['text'] == '40'
        assert second['content'][0]['text'] == '42'
    finally:
        await asyncio.create_task(context.close())
    assert context.cleanup_confirmed
    with pytest.raises(RuntimeError, match='not connected'):
        await context.call('increment', {'amount': 1})


def test_relative_server_command_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        DirectMCPContext(['python', 'server.py'], tmp_path)


async def test_real_effect_then_disconnect_is_unknown_and_never_replayed(tmp_path):
    server = tmp_path / 'disconnect.py'
    server.write_text('''import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP('synthetic-disconnect')
@server.tool()
def mutate() -> str:
    target = Path('effects.txt')
    with target.open('a', encoding='utf-8') as stream:
        stream.write('effect\\n')
        stream.flush()
        os.fsync(stream.fileno())
    os._exit(19)
server.run(transport='stdio')
''', encoding='utf-8')
    context = DirectMCPContext([sys.executable, '-I', str(server)], tmp_path)
    await context.open()
    engine = Engine(tmp_path / 'engine')
    async def mutate(_):
        return await context.call('mutate', {})
    engine.register('fixture_mutate', 'Synthetic disconnect fixture', Empty, mutate)
    try:
        request = Request(operation_id='a' * 32, tool='fixture_mutate')
        result = await engine.execute(request)
        assert result.state == 'unknown'
        assert result.data['error_code'] == 'direct_mcp_outcome_unknown'
        assert engine.ledger.get(request.operation_id) == result
        assert await engine.execute(request) == result
        with pytest.raises(RuntimeError, match='not connected'):
            await context.call('mutate', {})
        assert (tmp_path / 'effects.txt').read_text(encoding='utf-8') == 'effect\n'
    finally:
        await context.close()
        await engine.close()


async def test_peer_exception_body_is_not_returned(tmp_path, monkeypatch):
    context = DirectMCPContext([sys.executable], tmp_path)
    class BrokenSession:
        async def call_tool(self, *args):
            raise RuntimeError('synthetic-secret-not-for-diagnostics')
    monkeypatch.setattr(context, '_connected', lambda: BrokenSession())
    with pytest.raises(DirectMCPOutcomeUnknown) as caught:
        await context.call('fixture', {})
    assert 'synthetic-secret' not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_initialization_failure_returns_fixed_error(tmp_path):
    context = DirectMCPContext([sys.executable, '-I', '-c', 'raise SystemExit(2)'], tmp_path)
    with pytest.raises(RuntimeError, match='did not initialize'):
        await context.open()
    await context.close()


async def test_transport_cleanup_failure_is_not_hidden_by_finished_task(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from anywhere_computer import bounded_mcp_stdio

    @asynccontextmanager
    async def broken_transport(server, *, lifecycle):
        lifecycle.cleanup_confirmed = False
        raise RuntimeError('synthetic-private-cleanup-error')
        yield  # pragma: no cover - async context manager protocol

    monkeypatch.setattr(bounded_mcp_stdio, 'bounded_stdio', broken_transport)
    context = DirectMCPContext([sys.executable], tmp_path)
    with pytest.raises(RuntimeError, match='cleanup is incomplete') as caught:
        await context.open()
    assert 'synthetic-private' not in str(caught.value)
    assert context._task.done()
    assert not context.cleanup_confirmed
    with pytest.raises(RuntimeError, match='cleanup is incomplete'):
        await context.close()


async def test_catalog_cursor_round_trip_preserves_opaque_value(tmp_path, monkeypatch):
    from mcp.types import ListToolsResult, Tool

    received = []

    class PaginatedSession:
        async def list_tools(self, cursor=None):
            received.append(cursor)
            if cursor is None:
                return ListToolsResult(tools=[], nextCursor='opaque/日本語+=')
            return ListToolsResult(tools=[Tool(name='last', inputSchema={'type': 'object'})])

    context = DirectMCPContext([sys.executable], tmp_path)
    monkeypatch.setattr(context, '_connected', lambda: PaginatedSession())
    first = await context.list_tools()
    second = await context.list_tools(first['nextCursor'])
    assert received == [None, 'opaque/日本語+=']
    assert second['tools'][0]['name'] == 'last'
    assert second.get('nextCursor') is None


async def test_sdk_timeout_is_classified_without_exposing_message(tmp_path, monkeypatch):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    class TimedOut:
        async def call_tool(self, *args):
            raise McpError(ErrorData(code=408, message='synthetic-private-timeout-details'))

    context = DirectMCPContext([sys.executable], tmp_path)
    monkeypatch.setattr(context, '_connected', lambda: TimedOut())
    with pytest.raises(DirectMCPOutcomeUnknown) as caught:
        await context.call('fixture', {})
    assert caught.value.failure_kind == 'timeout'
    assert 'synthetic-private' not in str(caught.value)
