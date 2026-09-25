import asyncio
import sqlite3
import sys

import pytest

from anywhere_computer.engine import Engine


@pytest.mark.parametrize('revoke_device', [False, True])
async def test_revoked_http_grant_stops_engine_owned_watch(
    tmp_path, monkeypatch, revoke_device,
):
    from anywhere_computer import direct_mcp_sessions
    from anywhere_computer.authorization import (
        AuthorizationStore,
        current_grant_read_only,
        pkce_s256,
    )

    class Peer:
        cleanup_confirmed = False

        def __init__(self, command, cwd):
            pass

        async def open(self):
            pass

        async def close(self):
            self.cleanup_confirmed = True

        async def call(self, name, arguments):
            assert name in {'subchat_queue_watch', 'subchat_status'}
            data = ({'submission_operation_id': arguments['operation_id'], 'state': 'watching'}
                    if name == 'subchat_queue_watch' else
                    {'state': 'queued', 'queue_watch': {'state': 'watching'}})
            return {'isError': False, 'structuredContent': {'state': 'completed', 'data': data}}

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', Peer)
    monkeypatch.setattr(direct_mcp_sessions, 'WATCH_POLL_SECONDS', .01)
    resource = 'https://computer.example/mcp'
    store = AuthorizationStore(tmp_path / 'authority', resource=resource,
                               known_tools=frozenset({'mcp_session_open', 'mcp_call'}))
    store.register_client('client', frozenset({'https://client.example/callback'}))
    store.enroll_device('owner', 'device', frozenset({'mcp_session_open', 'mcp_call'}))
    code = store.approve(owner='owner', device='device', client='client',
                         redirect='https://client.example/callback', resource=resource,
                         tools=frozenset({'mcp_session_open', 'mcp_call'}),
                         challenge=pkce_s256('x' * 43))
    token = store.exchange_code(code=code, verifier='x' * 43, client='client',
                                redirect='https://client.example/callback', resource=resource)
    grant = store.verify(token.value, resource=resource)
    assert grant is not None
    engine = Engine(tmp_path / 'engine')
    try:
        engine.bind_http_watch_grant(grant.grant_id, store.database)
        assert current_grant_read_only(store.database, grant.grant_id) == grant
        opened = await engine.direct_mcp_sessions.open([sys.executable], tmp_path,
                                                       owner=grant.grant_id)
        sid = opened['session_id']
        other = await engine.direct_mcp_sessions.open([sys.executable], tmp_path,
                                                      owner=grant.grant_id)
        await engine.direct_mcp_sessions.stop(other['session_id'], owner=grant.grant_id)
        assert grant.grant_id in engine._http_watch_grants
        await engine.direct_mcp_sessions.call(sid, 'subchat_queue_watch',
            {'operation_id': 'a' * 32}, owner=grant.grant_id)
        assert engine.direct_mcp_sessions.active_watch_count == 1
        if revoke_device:
            store.revoke_device(owner='owner', device='device')
        else:
            store.revoke(owner='owner', grant=grant.grant_id)
        assert current_grant_read_only(store.database, grant.grant_id) is None
        async with asyncio.timeout(2):
            while engine.direct_mcp_sessions.active_watch_count:
                await asyncio.sleep(.01)
        assert engine.direct_mcp_sessions.status(sid, owner=grant.grant_id)['state'] == (
            'authorization_lost')
        assert engine.direct_mcp_sessions.watch_history(owner=grant.grant_id)[0]['reason'] == (
            'authorization_lost')
        assert engine.direct_mcp_sessions.active_count == 0
        assert grant.grant_id not in engine._http_watch_grants
    finally:
        await engine.close()
        store.close()


async def test_shared_agent_receives_http_watch_authority(tmp_path, monkeypatch):
    from anywhere_computer import direct_mcp_sessions
    from anywhere_computer.authorization import AuthorizationStore, pkce_s256
    from anywhere_computer.connection import exchange, exchange_remote, serve
    from anywhere_computer.models import Request

    class Peer:
        cleanup_confirmed = False

        def __init__(self, command, cwd):
            pass

        async def open(self):
            pass

        async def close(self):
            self.cleanup_confirmed = True

        async def call(self, name, arguments):
            data = ({'submission_operation_id': arguments['operation_id'], 'state': 'watching'}
                    if name == 'subchat_queue_watch' else
                    {'state': 'queued', 'queue_watch': {'state': 'watching'}})
            return {'isError': False, 'structuredContent': {'state': 'completed', 'data': data}}

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', Peer)
    monkeypatch.setattr(direct_mcp_sessions, 'WATCH_POLL_SECONDS', .01)
    monkeypatch.setattr('anywhere_computer.connection.local_credential', lambda _: 'fixture')
    resource = 'https://computer.example/mcp'
    authority = AuthorizationStore(tmp_path / 'auth', resource=resource,
                                   known_tools=frozenset({'mcp_session_open', 'mcp_call'}))
    authority.register_client('client', frozenset({'https://client.example/callback'}))
    authority.enroll_device('owner', 'device', frozenset({'mcp_session_open', 'mcp_call'}))
    code = authority.approve(owner='owner', device='device', client='client',
                             redirect='https://client.example/callback', resource=resource,
                             tools=frozenset({'mcp_session_open', 'mcp_call'}),
                             challenge=pkce_s256('x' * 43))
    token = authority.exchange_code(code=code, verifier='x' * 43, client='client',
                                    redirect='https://client.example/callback', resource=resource)
    grant = authority.verify(token.value, resource=resource)
    assert grant is not None
    directory = tmp_path / 'agent'
    shutdown = asyncio.Event()
    server = asyncio.create_task(serve(directory, credential='fixture', shutdown=shutdown))
    try:
        async with asyncio.timeout(5):
            while not (directory / 'agent.json').exists():
                await asyncio.sleep(.01)
        allowed = frozenset({'mcp_session_open', 'mcp_call'})
        opened = await exchange_remote(directory, grant.grant_id, allowed,
            Request(operation_id='1' * 32, tool='mcp_session_open',
                    arguments={'command': [sys.executable], 'cwd': str(tmp_path)}),
            authorization_database=authority.database)
        assert opened.state == 'completed'
        armed = await exchange_remote(directory, grant.grant_id, allowed,
            Request(operation_id='2' * 32, tool='mcp_call', arguments={
                'session_id': opened.data['session_id'], 'name': 'subchat_queue_watch',
                'arguments': {'operation_id': 'a' * 32}}),
            authorization_database=authority.database)
        assert armed.state == 'completed'
        assert (await exchange(directory, '__status')).data['active_resources'][
            'subchat_queue_watches'] == 1
        authority.revoke(owner='owner', grant=grant.grant_id)
        async with asyncio.timeout(2):
            while (await exchange(directory, '__status')).data['active_resources'][
                    'subchat_queue_watches']:
                await asyncio.sleep(.01)
    finally:
        shutdown.set()
        await asyncio.wait_for(server, 10)
        authority.close()


async def test_bounded_subchat_watch_owns_outer_session_and_records_stop(tmp_path, monkeypatch):
    from anywhere_computer import direct_mcp_sessions

    now = [0.0]
    journal = sqlite3.connect(tmp_path / 'watches.sqlite3')

    class Peer:
        cleanup_confirmed = False

        def __init__(self, command, cwd):
            pass

        async def open(self):
            pass

        async def close(self):
            self.cleanup_confirmed = True

        async def call(self, name, arguments):
            if name == 'subchat_queue_watch':
                state = 'disabled' if arguments.get('enabled') is False else 'watching'
                data = {'submission_operation_id': arguments['operation_id'], 'state': state}
            elif name == 'subchat_activity':
                data = {'active_count': 0}
            else:
                data = {'state': 'queued', 'queue_watch': {'state': 'watching'}}
            return {'isError': False, 'structuredContent': {'state': 'completed', 'data': data}}

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', Peer)
    pool = direct_mcp_sessions.DirectMCPSessions(clock=lambda: now[0], journal=journal)
    identity = 'a' * 32
    try:
        opened = await pool.open([sys.executable], tmp_path, owner='owner', idle_timeout=30)
        sid = opened['session_id']
        await pool.call(sid, 'subchat_queue_watch', {'operation_id': identity,
                        'lease_seconds': 600}, owner='owner')
        now[0] = 301
        await pool.expire_idle()
        assert pool.status(sid, owner='owner')['state'] == 'open'
        assert pool.active_watch_count == 1
        with pytest.raises(RuntimeError, match='queue watch is active'):
            await pool.stop(sid, owner='owner')
        assert pool.watch_history(owner='other') == []
        assert pool.watch_history(owner='owner')[0]['state'] == 'watching'
        await pool.call(sid, 'subchat_queue_watch', {'operation_id': identity,
                        'enabled': False}, owner='owner')
        assert pool.active_watch_count == 0
        assert pool.watch_history(owner='owner')[0]['reason'] == 'explicit_cancel'
        now[0] = 332
        await pool.expire_idle()
        assert pool.status(sid, owner='owner')['state'] == 'expired'

        second = await pool.open([sys.executable], tmp_path, owner='owner', idle_timeout=30)
        await pool.call(second['session_id'], 'subchat_queue_watch',
                        {'operation_id': identity}, owner='owner')
        restarted = direct_mcp_sessions.DirectMCPSessions(journal=journal)
        assert restarted.watch_history(owner='owner')[0]['reason'] == 'engine_restart'
        await restarted.close()
    finally:
        await pool.close()
        journal.close()


async def test_subchat_watch_is_visible_as_engine_update_blocker(tmp_path, monkeypatch):
    from anywhere_computer import direct_mcp_sessions

    class Peer:
        cleanup_confirmed = False

        def __init__(self, command, cwd):
            pass

        async def open(self):
            pass

        async def close(self):
            self.cleanup_confirmed = True

        async def call(self, name, arguments):
            return {'isError': False, 'structuredContent': {'state': 'completed', 'data': {
                'submission_operation_id': arguments['operation_id'], 'state': 'watching'}}}

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', Peer)
    engine = Engine(tmp_path / 'engine')
    try:
        opened = await engine.direct_mcp_sessions.open([sys.executable], tmp_path, owner=None)
        await engine.direct_mcp_sessions.call(opened['session_id'], 'subchat_queue_watch',
            {'operation_id': 'f' * 32}, owner=None)
        status = engine.status()
        assert status['active_resources']['subchat_queue_watches'] == 1
        assert 'subchat_queue_watches' in status['update_blockers']
        assert status['update_blocked'] is True
    finally:
        await engine.close()


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


@pytest.mark.parametrize('tool', [
    'subchat_send', 'subchat_recover', 'subchat_wait', 'subchat_message',
])
async def test_idle_expiry_preserves_subchat_background_work(tmp_path, monkeypatch, tool):
    from anywhere_computer import direct_mcp_sessions

    now = [0.0]

    class Peer:
        cleanup_confirmed = False
        active_count = 1
        activity_error = False

        def __init__(self, command, cwd):
            self.calls = []

        async def open(self):
            pass

        async def close(self):
            self.cleanup_confirmed = True

        async def call(self, name, arguments):
            self.calls.append(name)
            if name == 'subchat_activity':
                if self.activity_error:
                    raise RuntimeError('activity unavailable')
                data = {'active_count': self.active_count}
            else:
                data = {'state': 'sending'}
            return {'isError': False, 'structuredContent': {
                'state': 'completed', 'data': data}}

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', Peer)
    pool = direct_mcp_sessions.DirectMCPSessions(clock=lambda: now[0])
    try:
        opened = await pool.open([sys.executable], tmp_path, owner='owner', idle_timeout=30)
        sid = opened['session_id']
        entry = pool.entries[sid]
        await pool.call(sid, tool, {}, owner='owner')
        with pytest.raises(RuntimeError, match='Subchat background activity'):
            await pool.stop(sid, owner='owner')
        assert pool.status(sid, owner='owner')['state'] == 'open'
        assert not entry.context.cleanup_confirmed
        now[0] = 31
        await pool.call(sid, 'subchat_status', {}, owner='owner')
        assert pool.status(sid, owner='owner')['state'] == 'open'
        now[0] = 62
        await pool.expire_idle()
        assert pool.status(sid, owner='owner')['state'] == 'open'
        assert entry.context.calls[-1] == 'subchat_activity'

        now[0] = 93
        entry.context.activity_error = True
        await pool.expire_idle()
        assert pool.status(sid, owner='owner')['state'] == 'open'
        with pytest.raises(RuntimeError, match='could not be checked'):
            await pool.stop(sid, owner='owner')
        assert pool.status(sid, owner='owner')['state'] == 'open'
        assert not entry.context.cleanup_confirmed

        entry.context.activity_error = False
        entry.context.active_count = 0
        await pool.expire_idle()
        assert pool.status(sid, owner='owner')['state'] == 'expired'
    finally:
        await pool.close()


async def test_hung_subchat_activity_probe_does_not_block_idle_reaper(tmp_path, monkeypatch):
    from anywhere_computer import direct_mcp_sessions

    now = [0.0]

    class Peer:
        cleanup_confirmed = False

        def __init__(self, command, cwd):
            self.probes = 0
            self.cancelled = False

        async def open(self):
            pass

        async def close(self):
            self.cleanup_confirmed = True

        async def call(self, name, arguments):
            if name == 'subchat_activity':
                self.probes += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return {'isError': False, 'structuredContent': {
                'state': 'completed', 'data': {'state': 'sending'}}}

    monkeypatch.setattr(direct_mcp_sessions, 'DirectMCPContext', Peer)
    monkeypatch.setattr(direct_mcp_sessions, 'ACTIVITY_PROBE_TIMEOUT', .01)
    pool = direct_mcp_sessions.DirectMCPSessions(clock=lambda: now[0])
    try:
        sid = (await pool.open([sys.executable], tmp_path, owner=None,
                               idle_timeout=30))['session_id']
        peer = pool.entries[sid].context
        await pool.call(sid, 'subchat_send', {}, owner=None)
        now[0] = 31
        await asyncio.wait_for(pool.expire_idle(), timeout=.5)
        assert pool.status(sid, owner=None)['state'] == 'open'
        assert peer.probes == 1 and not peer.cancelled
        await asyncio.wait_for(pool.expire_idle(), timeout=.5)
        assert peer.probes == 1
    finally:
        await pool.close()
    assert peer.cancelled


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
