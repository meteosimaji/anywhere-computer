import asyncio

import pytest
from test_relay_channels import channel_setup as channel_setup
from test_relay_channels import wait_connected
from test_relay_grants import execution as execution
from test_remote_transport import certificates as certificates
from websockets.exceptions import ConnectionClosed

from anywhere_computer.models import Request
from anywhere_computer.relay_channels import ChannelOutcomeUnknown
from anywhere_computer.relay_client import PCRelayClient
from anywhere_computer.relay_grants import ExecutionEnvelope, ExecutionVerifier


async def test_client_reconnects_without_replaying_and_stops(
    channel_setup, execution, certificates, monkeypatch,
):
    relay, _, account, device_id, _ = channel_setup
    pc, sign, _, root, _ = execution
    context, _ = certificates
    pc.device_id = device_id
    received = []
    original = pc.dispatch_frame
    async def tracked(payload):
        received.append(ExecutionEnvelope.model_validate_json(payload).request.tool)
        result = await original(payload)
        if received == ['files_write']:
            await relay._channels[device_id].socket.close(code=1012, reason='Lost committed reply')
        return result
    monkeypatch.setattr(pc, 'dispatch_frame', tracked)
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    task = asyncio.create_task(client.run())
    try:
        await wait_connected(relay, device_id)
        with pytest.raises(RuntimeError, match='already running'):
            await client.run()
        verifier = ExecutionVerifier(pc.verifier.tokens, is_current=lambda _: True)
        token = sign({'device_id': device_id})
        target = root / '継続.txt'
        write = Request(operation_id='c' * 32, tool='files_write',
                        arguments={'path': str(target), 'text': '継続 🚀'})
        old = relay._channels[device_id]
        with pytest.raises(ChannelOutcomeUnknown):
            await relay.exchange_authorized(verifier, account, device_id, token, write)
        await wait_connected(relay, device_id, previous=old)
        result = await relay.exchange_authorized(
            verifier, account, device_id, token,
            Request(operation_id='d' * 32, tool='operations_get',
                    arguments={'operation_id': write.operation_id}),
        )
        assert result.data['state'] == 'completed'
        assert target.read_text(encoding='utf-8') == '継続 🚀'
        assert received == ['files_write', 'operations_get']
        assert client.state == 'connected'
    finally:
        await client.stop()
        await asyncio.wait_for(task, timeout=10)
    assert client.state == 'stopped'


async def test_replaced_client_does_not_reconnect_fight(channel_setup, execution, certificates):
    relay, _, _, device_id, connect = channel_setup
    pc, _, _, _, _ = execution
    context, _ = certificates
    pc.device_id = device_id
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    task = asyncio.create_task(client.run())
    try:
        await wait_connected(relay, device_id)
        previous = relay._channels[device_id]
        async with connect():
            await wait_connected(relay, device_id, previous=previous)
            await asyncio.wait_for(task, timeout=10)
            assert client.state == 'stopped'
    finally:
        await client.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_revoked_registration_stops_retrying(channel_setup, execution, certificates):
    relay, registry, account, device_id, _ = channel_setup
    pc, _, _, _, _ = execution
    context, _ = certificates
    registry.revoke(account, device_id)
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(client.run(), timeout=10)
    assert client.state == 'failed'


async def test_engine_io_failure_is_not_classified_as_reconnectable_network(
    channel_setup, execution, certificates, monkeypatch,
):
    relay, _, account, device_id, _ = channel_setup
    pc, sign, _, _, _ = execution
    context, _ = certificates
    pc.device_id = device_id
    calls = []
    async def failed_engine(payload):
        calls.append(payload)
        raise OSError('isolated engine storage failure')
    monkeypatch.setattr(pc, 'dispatch_frame', failed_engine)
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    task = asyncio.create_task(client.run())
    try:
        await wait_connected(relay, device_id)
        with pytest.raises(ChannelOutcomeUnknown):
            await relay.exchange_authorized(
                pc.verifier, account, device_id, sign({'device_id': device_id}),
                Request(operation_id='e' * 32, tool='__catalog'),
            )
        async with asyncio.timeout(2):
            with pytest.raises(RuntimeError, match='execution failed'):
                await asyncio.shield(task)
        assert client.state == 'failed' and len(calls) == 1
    finally:
        await client.stop()
        if not task.done():
            await asyncio.wait_for(task, timeout=10)


async def test_stop_during_handshake_does_not_leave_connected_client(
    channel_setup, execution, certificates, monkeypatch,
):
    from contextlib import asynccontextmanager

    from anywhere_computer import relay_client

    relay, _, _, device_id, _ = channel_setup
    pc, _, _, _, _ = execution
    context, _ = certificates
    entered, release = asyncio.Event(), asyncio.Event()
    real_connect = relay_client.connect
    attempts = []

    @asynccontextmanager
    async def delayed_connect(*args, **kwargs):
        attempts.append(1)
        async with real_connect(*args, **kwargs) as socket:
            entered.set()
            await release.wait()
            yield socket

    monkeypatch.setattr(relay_client, 'connect', delayed_connect)
    port = relay._server.sockets[0].getsockname()[1]
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert client.state == 'connecting'
        await client.stop()
        release.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
        assert client.state == 'stopped'
        assert client._socket is None
        assert attempts == [1]
    finally:
        release.set()
        await client.stop()
        await asyncio.wait_for(task, timeout=5)
