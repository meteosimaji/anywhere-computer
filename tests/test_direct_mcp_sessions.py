import sys

import pytest

from anywhere_computer.engine import Engine


async def test_large_paginated_catalog_keeps_empty_filtered_page_cursor_and_last_tool(tmp_path):
    server = tmp_path / 'catalog.py'
    server.write_text('''import asyncio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
m = Server('large-catalog')
cursor = 'opaque/日本語+='
@m.list_tools()
async def catalog(request: types.ListToolsRequest) -> types.ListToolsResult:
    page = request.params.cursor if request.params else None
    assert page in (None, cursor)
    start, stop = (0, 80) if page is None else (80, 118)
    return types.ListToolsResult(tools=[types.Tool(
        name=f'tool_{i:03}', description='ordinary ' * 100 + ('needle' if i == 117 else ''),
        inputSchema={'type': 'object', 'properties': {}},
    ) for i in range(start, stop)], nextCursor=cursor if page is None else None)
@m.call_tool()
async def call(name: str, arguments: dict):
    assert name == 'tool_117'
    return [types.TextContent(type='text', text='42')]
async def main():
    async with stdio_server() as (read, write):
        await m.run(read, write, m.create_initialization_options())
asyncio.run(main())
''', encoding='utf-8')
    engine = Engine(tmp_path / 'engine')
    pool = engine.direct_mcp_sessions
    try:
        opened = await pool.open([sys.executable, '-I', str(server)], tmp_path, owner=None)
        sid = opened['session_id']
        first = await pool.tools(sid, owner=None)
        second = await pool.tools(sid, owner=None, cursor=first['nextCursor'])
        assert len(first['tools']) == 80
        assert len(second['tools']) == 38
        assert len({row['name'] for row in first['tools'] + second['tools']}) == 118
        filtered = await pool.tools(sid, owner=None, query='NEEDLE', summary=True)
        assert filtered['tools'] == []
        assert filtered['nextCursor'] == 'opaque/日本語+='
        last = await pool.tools(sid, owner=None, query='NEEDLE', summary=True,
                                cursor=filtered['nextCursor'])
        assert [row['name'] for row in last['tools']] == ['tool_117']
        assert 'inputSchema' not in last['tools'][0]
        exact = await pool.tools(sid, owner=None, name='tool_117', cursor=first['nextCursor'])
        assert 'inputSchema' in exact['tools'][0]
        result = await pool.call(sid, 'tool_117', {}, owner=None)
        assert result['content'][0]['text'] == '42'
    finally:
        await engine.close()


async def test_real_session_ownership_and_update_blocker(tmp_path):
    server = tmp_path / 'server.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
server = FastMCP('pool-fixture')
count = 0
@server.tool()
def increment() -> int:
    global count
    count += 1
    return count
server.run(transport='stdio')
''')
    engine = Engine(tmp_path / 'engine')
    pool = engine.direct_mcp_sessions
    try:
        result = await pool.open([sys.executable, '-I', str(server)], tmp_path, owner='owner-a')
        session_id = result['session_id']
        assert engine.status()['update_blocked']
        assert engine.status()['active_resources']['direct_mcp_sessions'] == 1
        brief = await pool.tools(session_id, owner='owner-a', summary=True, query='INCREMENT')
        assert [tool['name'] for tool in brief['tools']] == ['increment']
        assert 'inputSchema' not in brief['tools'][0]
        exact = await pool.tools(session_id, owner='owner-a', name='increment')
        assert 'inputSchema' in exact['tools'][0]
        assert exact['filter_scope'] == 'current_page'
        empty = await pool.tools(session_id, owner='owner-a', query='not-present')
        assert empty['tools'] == [] and empty['received_tool_count'] == 1
        with pytest.raises(ValueError, match='not found'):
            await pool.call(session_id, 'increment', {}, owner='owner-b')
        for expected in ['1', '2']:
            result = await pool.call(session_id, 'increment', {}, owner='owner-a')
            assert result['content'][0]['text'] == expected
        await pool.stop(session_id, owner='owner-a')
        assert not engine.status()['update_blocked']
        assert pool.status(session_id, owner='owner-a')['cleanup_confirmed']
    finally:
        await engine.close()


async def test_concurrent_open_reserves_capacity_before_start(tmp_path, monkeypatch):
    import asyncio

    from anywhere_computer import direct_mcp_sessions

    started = asyncio.Event()
    release = asyncio.Event()
    opened = 0

    class WaitingContext:
        cleanup_confirmed = False

        def __init__(self, command, cwd):
            pass

        async def open(self):
            nonlocal opened
            opened += 1
            if opened == 4:
                started.set()
            await release.wait()

        async def close(self):
            self.cleanup_confirmed = True

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', WaitingContext)
    pool = direct_mcp_sessions.DirectMCPSessions()
    pending = [asyncio.create_task(pool.open([sys.executable], tmp_path, owner=None))
               for _ in range(4)]
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(RuntimeError, match='capacity'):
            await pool.open([sys.executable], tmp_path, owner=None)
        assert opened == 4
    finally:
        release.set()
        await asyncio.gather(*pending)
        await pool.close()
    assert pool.active_count == 0


async def test_registered_tools_execute_and_recover_without_repeating_effect(tmp_path):
    import uuid

    from anywhere_computer.models import Request

    server = tmp_path / 'registered.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
m = FastMCP('registered')
n = 0
@m.tool()
def increment() -> int:
    global n
    n += 1
    return n
m.run(transport='stdio')
''')
    engine = Engine(tmp_path / 'engine')

    async def execute(tool, arguments, peer='first'):
        return await engine.execute(Request(operation_id=uuid.uuid4().hex, tool=tool,
                                            arguments=arguments), peer=peer)

    try:
        opened = await execute('mcp_session_open', {
            'command': [sys.executable, '-I', str(server)], 'cwd': str(tmp_path),
        })
        assert opened.state == 'completed'
        sid = {'session_id': opened.data['session_id']}
        catalog = await execute('mcp_tools', sid)
        assert catalog.data['tools'][0]['name'] == 'increment'
        denied = await execute('mcp_call', {**sid, 'name': 'increment'}, peer='second')
        assert denied.state == 'failed'
        request = Request(operation_id=uuid.uuid4().hex, tool='mcp_call',
                          arguments={**sid, 'name': 'increment'})
        first = await engine.execute(request, peer='first')
        assert first.data['content'][0]['text'] == '1'
        assert await engine.execute(request, peer='first') == first
        second = await execute('mcp_call', {**sid, 'name': 'increment'})
        assert second.data['content'][0]['text'] == '2'
        closed = await execute('mcp_session_close', sid)
        assert closed.data['cleanup_confirmed']
    finally:
        await engine.close()


async def test_idle_expiry_ignores_status_and_protects_active_call(tmp_path):
    from anywhere_computer.direct_mcp_sessions import DirectMCPSessions

    now = [0.0]
    pool = DirectMCPSessions(clock=lambda: now[0])
    server = tmp_path / 'idle.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
m = FastMCP('idle')
m.run(transport='stdio')
''')
    try:
        result = await pool.open([sys.executable, '-I', str(server)], tmp_path,
                                 owner=None, idle_timeout=30)
        sid = result['session_id']
        now[0] = 29
        assert pool.status(sid, owner=None)['idle_seconds'] == 29
        await pool.expire_idle()
        assert pool.active_count == 1
        # A held operation lease models an in-flight call, regardless of duration.
        async with pool._lease(sid, None):
            now[0] = 100
            await pool.expire_idle()
            assert pool.active_count == 1
        now[0] = 129
        await pool.expire_idle()
        assert pool.active_count == 1
        now[0] = 130
        await pool.expire_idle()
        assert pool.active_count == 0
        assert pool.status(sid, owner=None)['state'] == 'expired'
        with pytest.raises(RuntimeError, match='no automatic restart'):
            await pool.tools(sid, owner=None)
    finally:
        await pool.close()


async def test_real_direct_image_reaches_mcp_projection_and_ledger(tmp_path):
    import uuid

    from mcp.types import CallToolResult, ImageContent

    from anywhere_computer.mcp_server import _reply_result
    from anywhere_computer.models import Request

    png = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
           '/x8AAwMCAO+a5XcAAAAASUVORK5CYII=')
    server = tmp_path / 'image.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
m = FastMCP('image')
@m.tool()
def picture() -> ImageContent:
    return ImageContent(type='image', mimeType='image/png', data=''' + repr(png) + ''')
m.run(transport='stdio')
''')
    engine = Engine(tmp_path / 'engine')
    try:
        opened = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool='mcp_session_open', arguments={
                'command': [sys.executable, '-I', str(server)], 'cwd': str(tmp_path),
            }))
        request = Request(operation_id=uuid.uuid4().hex, tool='mcp_call', arguments={
            'session_id': opened.data['session_id'], 'name': 'picture',
        })
        reply = await engine.execute(request)
        assert reply.state == 'completed'
        wire = CallToolResult.model_validate(_reply_result('mcp_call', reply))
        assert isinstance(wire.content[1], ImageContent)
        assert wire.content[1].data == png
        assert png not in wire.content[0].text
        assert engine.ledger.get(request.operation_id).data['content'][0]['data'] == png
        recovery = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool='operations_get', arguments={'operation_id': request.operation_id}))
        recovered = CallToolResult.model_validate(_reply_result('operations_get', recovery))
        assert recovered.content[1].data == png
    finally:
        await engine.close()
