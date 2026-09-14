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
