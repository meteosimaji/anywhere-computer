import asyncio

import pytest
from test_relay_channels import channel_setup as channel_setup
from test_relay_channels import wait_connected
from test_relay_grants import execution as execution
from test_remote_transport import certificates as certificates

from anywhere_computer.mcp_server import PROTOCOL_VERSION
from anywhere_computer.relay_client import PCRelayClient
from anywhere_computer.relay_grants import ExecutionRejected, ExecutionVerifier
from anywhere_computer.relay_mcp import RelayMCPBackend


async def test_mcp_discovery_write_refresh_and_session_binding(
    channel_setup, execution, certificates,
):
    relay, _, account, device_id, _ = channel_setup
    pc, sign, current, root, claims = execution
    context, _ = certificates
    pc.device_id = device_id
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    token = {'value': sign({'device_id': device_id})}
    verifier = ExecutionVerifier(pc.verifier.tokens, is_current=lambda _: current['active'])
    backend = RelayMCPBackend(relay, verifier, account=account, device_id=device_id,
                              token=lambda: token['value'])
    task = asyncio.create_task(client.run())
    try:
        await wait_connected(relay, device_id)
        session = backend.mcp_session()
        initialized = await session.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
                       'clientInfo': {'name': 'isolated-mcp-test', 'version': '1'}}})
        assert 'result' in initialized
        await session.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        catalog = await session.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert {t['name'] for t in catalog['result']['tools']} == {'files_write', 'operations_get'}
        target = root / 'MCP日本語.txt'
        write = await session.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'files_write', 'arguments': {
                'path': str(target), 'text': 'MCP 🚀', 'request_id': 'f' * 32}}})
        assert write['result']['structuredContent']['state'] == 'completed'
        assert target.read_text(encoding='utf-8') == 'MCP 🚀'
        token['value'] = sign({'device_id': device_id, 'exp': claims['exp'] + 60})
        recovered = await session.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'operations_get', 'arguments': {
                'operation_id': 'f' * 32, 'request_id': '1' * 32}}})
        assert recovered['result']['structuredContent']['data']['state'] == 'completed'
        token['value'] = sign({'device_id': device_id, 'grant_id': 'd' * 32})
        with pytest.raises(ExecutionRejected, match='binding changed'):
            await backend.catalog()
        token['value'] = sign({'device_id': device_id})
        current['active'] = False
        with pytest.raises(ExecutionRejected):
            await backend.catalog()
    finally:
        await client.stop()
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.parametrize("lose_reply", [False, True])
async def test_http_to_real_pc_write_refresh_and_revocation(
    channel_setup, execution, certificates, lose_reply, monkeypatch,
):
    import httpx
    from test_http_mcp import HEADERS, INITIALIZE

    from anywhere_computer.relay_mcp import relay_http_mcp

    relay, _, account, device_id, _ = channel_setup
    pc, sign, current, root, claims = execution
    context, _ = certificates
    pc.device_id = device_id
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    original_dispatch = pc.dispatch_frame
    lost = False
    writes = 0

    async def dispatch(payload):
        nonlocal lost, writes
        if b'"tool":"files_write"' in payload:
            writes += 1
        result = await original_dispatch(payload)
        if lose_reply and not lost and b'"tool":"files_write"' in payload:
            lost = True
            await relay._channels[device_id].socket.close(code=1012)
        return result

    monkeypatch.setattr(pc, 'dispatch_frame', dispatch)
    adapter = relay_http_mcp(relay, pc.verifier, account=account, device_id=device_id)
    http_port = await adapter.start()
    task = asyncio.create_task(client.run())
    try:
        await wait_connected(relay, device_id)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{http_port}',
                                     headers=HEADERS) as http:
            http.headers['Authorization'] = 'Bearer ' + sign({'device_id': device_id})
            initialized = await http.post('/mcp', json=INITIALIZE)
            assert initialized.status_code == 200
            http.headers['MCP-Session-Id'] = initialized.headers['MCP-Session-Id']
            await http.post('/mcp', json={'jsonrpc': '2.0',
                                         'method': 'notifications/initialized'})
            target = root / 'HTTP中継.txt'
            write = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 2,
                'method': 'tools/call', 'params': {'name': 'files_write', 'arguments': {
                    'path': str(target), 'text': 'HTTP 🚀', 'request_id': 'e' * 32}}})
            assert write.json()['result']['structuredContent']['state'] == (
                'unknown' if lose_reply else 'completed')
            if lose_reply:
                await wait_connected(relay, device_id)
            assert target.read_text(encoding='utf-8') == 'HTTP 🚀'
            http.headers['Authorization'] = 'Bearer ' + sign({
                'device_id': device_id, 'exp': claims['exp'] + 60})
            recovered = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 3,
                'method': 'tools/call', 'params': {'name': 'operations_get', 'arguments': {
                    'operation_id': 'e' * 32, 'request_id': '2' * 32}}})
            assert recovered.json()['result']['structuredContent']['data']['state'] == 'completed'
            assert writes == 1
            assert lost == lose_reply
            http.headers['Authorization'] = 'Bearer ' + sign({
                'device_id': device_id, 'grant_id': 'c' * 32})
            assert (await http.post('/mcp', json=INITIALIZE)).status_code == 404
            current['active'] = False
            assert (await http.post('/mcp', json=INITIALIZE)).status_code == 401
        with pytest.raises(RuntimeError, match='No authenticated'):
            adapter.current_bearer()
    finally:
        await adapter.close()
        await client.stop()
        await asyncio.wait_for(task, timeout=10)
