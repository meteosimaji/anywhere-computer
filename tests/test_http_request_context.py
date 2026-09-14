import asyncio
import json

import pytest
from test_http_mcp import INITIALIZE

from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.mcp_server import MCPSession


async def test_bearer_is_scoped_to_request_and_not_inherited_by_child():
    gate = asyncio.Event()
    seen = []

    async def authenticate(token):
        return 'owner'

    async def catalog():
        before = adapter.current_bearer()
        seen.append(before)
        if len(seen) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        assert adapter.current_bearer() == before

        async def child():
            with pytest.raises(RuntimeError, match='No authenticated'):
                adapter.current_bearer()

        await asyncio.create_task(child())
        return []

    async def execute(request):
        raise AssertionError('Not used')

    adapter = HTTPMCP(authenticate, lambda _: MCPSession(catalog, execute))

    async def request(token):
        headers = {'authorization': 'Bearer ' + token,
                   'accept': 'application/json, text/event-stream',
                   'content-type': 'application/json'}
        _, _, result_headers = await adapter._dispatch(
            'POST', headers, json.dumps(INITIALIZE).encode())
        headers['mcp-session-id'] = result_headers['MCP-Session-Id']
        await adapter._dispatch('POST', headers, json.dumps({
            'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode())
        result = await adapter._dispatch('POST', headers, json.dumps({
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}).encode())
        assert result[1]['result']['tools'] == []
        with pytest.raises(RuntimeError, match='No authenticated'):
            adapter.current_bearer()

    await asyncio.gather(request('one'), request('two'))
    assert set(seen) == {'one', 'two'}
