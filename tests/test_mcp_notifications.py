"""Optional asynchronous server notices preserve stdio JSON-RPC framing."""

import asyncio
import io
import json
import queue

from anywhere_computer.mcp_server import MCPSession, serve_stdio


class Input:
    def __init__(self):
        self.lines: queue.Queue[bytes] = queue.Queue()

    def readline(self, _limit):
        return self.lines.get()


async def test_optional_notification_pump_writes_only_after_initialize():
    source = Input()
    destination = io.BytesIO()

    async def catalog():
        return []

    async def execute(_request):
        raise AssertionError('No tool call expected')

    session = MCPSession(catalog, execute)
    notices: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    session.notifications = notices
    task = asyncio.create_task(serve_stdio(session, source, destination))
    try:
        notices.put_nowait({'jsonrpc': '2.0', 'method': 'notifications/message',
                            'params': {'level': 'info', 'data': {'event': 'completed'}}})
        await asyncio.sleep(.02)
        assert destination.getvalue() == b''
        source.lines.put(json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '1'}}}).encode() + b'\n')
        source.lines.put(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        async with asyncio.timeout(2):
            while b'notifications/message' not in destination.getvalue():
                await asyncio.sleep(.01)
        packets = [json.loads(line) for line in destination.getvalue().splitlines()]
        assert {packet.get('method') for packet in packets} == {
            None, 'notifications/message'}
        assert packets[0]['result']['capabilities']['logging'] == {}
        assert packets[-1]['params']['data'] == {'event': 'completed'}
    finally:
        source.lines.put(b'')
        await task


async def test_notification_logging_level_is_negotiated():
    async def catalog():
        return []

    async def execute(_request):
        raise AssertionError('No tool call expected')

    session = MCPSession(catalog, execute)
    session.notifications = asyncio.Queue(maxsize=1)
    initialized = await session.handle({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'test', 'version': '1'}}})
    assert initialized['result']['capabilities']['logging'] == {}
    await session.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    changed = await session.handle({'jsonrpc': '2.0', 'id': 2,
                                    'method': 'logging/setLevel',
                                    'params': {'level': 'warning'}})
    assert changed['result'] == {}
    assert session.logging_level == 'warning'
    invalid = await session.handle({'jsonrpc': '2.0', 'id': 3,
                                    'method': 'logging/setLevel',
                                    'params': {'level': 'garbage'}})
    assert invalid['error']['code'] == -32602
