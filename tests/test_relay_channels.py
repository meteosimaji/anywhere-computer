import asyncio
import time

import pytest
from test_remote_transport import certificates as certificates
from websockets.asyncio.client import connect

from anywhere_computer.engine import Engine
from anywhere_computer.models import Reply, Request
from anywhere_computer.relay_channels import (
    PC_PROTOCOL,
    ChannelOutcomeUnknown,
    ChannelUnavailable,
    RelayChannels,
)
from anywhere_computer.relay_registry import RelayAccount, RelayRegistry
from anywhere_computer.remote_bridge import RemoteAgent


async def wait_connected(relay, device_id, previous=None):
    async with asyncio.timeout(10):
        while (relay._channels.get(device_id) is None
               or relay._channels.get(device_id) is previous):
            await asyncio.sleep(0.01)


@pytest.fixture
async def channel_setup(tmp_path, certificates):
    context, fingerprint = certificates
    registry = RelayRegistry(tmp_path / 'registry')
    account = RelayAccount(issuer='https://issuer.example', subject='owner')
    device = registry.register(account, enrollment_id='a' * 32, name='Test PC')
    registry.bind_channel(account, device.device_id, fingerprint=fingerprint('client'))
    relay = RelayChannels(registry, context('server', False))
    port = await relay.start()
    def client(name='client', *, protocol=PC_PROTOCOL):
        return connect(f'wss://localhost:{port}/pc', ssl=context(name, True),
                       subprotocols=[protocol], proxy=None, compression=None,
                       close_timeout=1)
    try:
        yield relay, registry, account, device.device_id, client
    finally:
        await relay.close()
        registry.close()


async def test_pc_initiates_and_recovers_commit_after_lost_reply(channel_setup, tmp_path):
    relay, registry, account, device_id, client = channel_setup
    engine = Engine(tmp_path / 'engine')
    # Synthetic authorization is intentional: this tests the PC transport only.
    agent = RemoteAgent(engine, {})
    agent.grant('isolated-grant', frozenset({'files_write', 'operations_get'}),
                expires_at=time.time() + 60)
    target = tmp_path / '日本語.txt'
    write = Request(operation_id='1' * 32, tool='files_write',
                    arguments={'path': str(target), 'text': '日本語 🚀'})
    received = []
    try:
        with pytest.raises(ChannelUnavailable, match='offline'):
            await relay.exchange(account, device_id, write)
        async with client() as pc:
            await wait_connected(relay, device_id)
            async def lose_reply():
                raw = await pc.recv()
                received.append(Request.model_validate_json(raw).tool)
                reply = Reply.model_validate_json(await agent.dispatch('isolated-grant', raw))
                assert reply.state == 'completed'
                await pc.close()
            responder = asyncio.create_task(lose_reply())
            with pytest.raises(ChannelOutcomeUnknown):
                await relay.exchange(account, device_id, write)
            await responder
        assert target.read_text(encoding='utf-8') == '日本語 🚀'
        async with client() as pc:
            await wait_connected(relay, device_id)
            recovery = Request(operation_id='2' * 32, tool='operations_get',
                               arguments={'operation_id': write.operation_id})
            async def recover():
                raw = await pc.recv()
                received.append(Request.model_validate_json(raw).tool)
                await pc.send(await agent.dispatch('isolated-grant', raw))
            responder = asyncio.create_task(recover())
            result = await relay.exchange(account, device_id, recovery)
            await responder
            assert result.data['state'] == 'completed'
            assert result.data['operation_id'] == write.operation_id
        assert received == ['files_write', 'operations_get']
    finally:
        await engine.close()


async def test_registry_owner_and_revocation_checked_on_live_connection(channel_setup):
    relay, registry, account, device_id, client = channel_setup
    request = Request(operation_id='3' * 32, tool='computer_status')
    async with client() as pc:
        await wait_connected(relay, device_id)
        other = RelayAccount(issuer=account.issuer, subject='other')
        with pytest.raises(ChannelUnavailable, match='registration'):
            await relay.exchange(other, device_id, request)
        registry.revoke(account, device_id)
        with pytest.raises(ChannelUnavailable, match='registration'):
            await relay.exchange(account, device_id, request)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pc.recv(), 0.05)


async def test_replacement_invalidates_old_inflight_reply_without_offline_queue(channel_setup):
    relay, _, account, device_id, client = channel_setup
    request = Request(operation_id='4' * 32, tool='computer_status')
    async with client() as first:
        await wait_connected(relay, device_id)
        old = relay._channels[device_id]
        pending = asyncio.create_task(relay.exchange(account, device_id, request))
        assert Request.model_validate_json(await first.recv()).operation_id == request.operation_id
        with pytest.raises(ChannelUnavailable, match='busy'):
            await relay.exchange(account, device_id, request)
        async with client() as second:
            await wait_connected(relay, device_id, old)
            with pytest.raises(ChannelOutcomeUnknown):
                await pending
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second.recv(), 0.05)
            assert relay._channels[device_id] is not old


async def test_unregistered_certificate_never_becomes_an_execution_target(channel_setup):
    relay, _, account, device_id, client = channel_setup
    async with client('stranger') as pc:
        await asyncio.wait_for(pc.wait_closed(), 5)
        assert pc.close_code == 1008
    with pytest.raises(ChannelUnavailable, match='offline'):
        await relay.exchange(account, device_id,
                             Request(operation_id='5' * 32, tool='computer_status'))


async def test_wrong_reply_identity_and_timeout_close_without_replay(channel_setup):
    relay, _, account, device_id, client = channel_setup
    request = Request(operation_id='6' * 32, tool='computer_status')
    async with client() as pc:
        await wait_connected(relay, device_id)
        pending = asyncio.create_task(relay.exchange(account, device_id, request))
        await pc.recv()
        await pc.send(Reply(operation_id='7' * 32, state='completed',
                            data={'private': 'must not return'}).model_dump_json().encode())
        with pytest.raises(ChannelOutcomeUnknown):
            await pending
        await asyncio.wait_for(pc.wait_closed(), 5)
    async with client() as pc:
        await wait_connected(relay, device_id)
        pending = asyncio.create_task(relay.exchange(account, device_id, request, timeout=0.05))
        assert Request.model_validate_json(await pc.recv()).operation_id == request.operation_id
        with pytest.raises(ChannelOutcomeUnknown):
            await pending
        await asyncio.wait_for(pc.wait_closed(), 5)


async def test_two_registered_pcs_route_concurrently_by_id_not_display_name(
    channel_setup, certificates,
):
    relay, registry, account, first_id, client = channel_setup
    _, fingerprint = certificates
    second = registry.register(account, enrollment_id='b' * 32, name='Test PC')
    registry.bind_channel(account, second.device_id, fingerprint=fingerprint('stranger'))
    async with client() as first, client('stranger') as other:
        await wait_connected(relay, first_id)
        await wait_connected(relay, second.device_id)
        async def response(pc, marker):
            request = Request.model_validate_json(await pc.recv())
            await pc.send(Reply(operation_id=request.operation_id, state='completed',
                                data={'marker': marker}).model_dump_json().encode())
        responders = [asyncio.create_task(response(first, 'first')),
                      asyncio.create_task(response(other, 'second'))]
        # The same external operation ID on different PC channels isn't a route key.
        request = Request(operation_id='8' * 32, tool='computer_status')
        results = await asyncio.gather(
            relay.exchange(account, first_id, request),
            relay.exchange(account, second.device_id, request),
        )
        await asyncio.gather(*responders)
        assert [result.data['marker'] for result in results] == ['first', 'second']


async def test_isolated_listener_rejects_public_bind_and_unverified_tls(tmp_path, certificates):
    import ssl
    context, _ = certificates
    registry = RelayRegistry(tmp_path / 'registry')
    relay = RelayChannels(registry, context('server', False))
    try:
        with pytest.raises(ValueError, match='loopback'):
            await relay.start('0.0.0.0')
        insecure = context('server', False)
        insecure.verify_mode = ssl.CERT_NONE
        with pytest.raises(ValueError, match='certificate verification'):
            RelayChannels(registry, insecure)
    finally:
        await relay.close()
        registry.close()
