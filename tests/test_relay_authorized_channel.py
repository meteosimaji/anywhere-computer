import asyncio

import pytest
from test_relay_channels import channel_setup as channel_setup
from test_relay_channels import wait_connected
from test_relay_grants import execution as execution
from test_remote_transport import certificates as certificates

from anywhere_computer.models import Reply, Request
from anywhere_computer.relay_channels import ChannelOutcomeUnknown
from anywhere_computer.relay_grants import ExecutionRejected, ExecutionVerifier


async def test_signed_wss_write_lost_reply_and_refreshed_recovery(channel_setup, execution):
    relay, _, account, device_id, client = channel_setup
    pc, sign, _, root, claims = execution
    pc.device_id = device_id
    verifier = ExecutionVerifier(pc.verifier.tokens, is_current=lambda _: True)
    token = sign({'device_id': device_id})
    target = root / '中継.txt'
    write = Request(operation_id='6' * 32, tool='files_write',
                    arguments={'path': str(target), 'text': '中継 🚀'})
    calls = []
    async with client() as socket:
        await wait_connected(relay, device_id)
        async def lose_reply():
            payload = await socket.recv()
            calls.append('write')
            result = Reply.model_validate_json(await pc.dispatch_frame(payload))
            assert result.state == 'completed'
            await socket.close()
        responder = asyncio.create_task(lose_reply())
        with pytest.raises(ChannelOutcomeUnknown):
            await relay.exchange_authorized(verifier, account, device_id, token, write)
        await responder
    assert target.read_text(encoding='utf-8') == '中継 🚀'
    async with client() as socket:
        await wait_connected(relay, device_id)
        async def recover():
            payload = await socket.recv()
            calls.append('lookup')
            await socket.send(await pc.dispatch_frame(payload))
        responder = asyncio.create_task(recover())
        result = await relay.exchange_authorized(
            verifier, account, device_id,
            sign({'device_id': device_id, 'exp': claims['exp'] + 60}),
            Request(operation_id='7' * 32, tool='operations_get',
                    arguments={'operation_id': write.operation_id}),
        )
        await responder
        assert result.state == 'completed' and result.data['state'] == 'completed'
    assert calls == ['write', 'lookup']


async def test_pc_rechecks_revocation_after_relay_dispatch(channel_setup, execution):
    relay, _, account, device_id, client = channel_setup
    pc, sign, current, root, _ = execution
    pc.device_id = device_id
    verifier = ExecutionVerifier(pc.verifier.tokens, is_current=lambda _: True)
    target = root / 'denied.txt'
    request = Request(operation_id='8' * 32, tool='files_write',
                      arguments={'path': str(target), 'text': 'must not write'})
    async with client() as socket:
        await wait_connected(relay, device_id)
        async def revoked_pc():
            payload = await socket.recv()
            current['active'] = False
            await socket.send(await pc.dispatch_frame(payload))
        responder = asyncio.create_task(revoked_pc())
        result = await relay.exchange_authorized(
            verifier, account, device_id, sign({'device_id': device_id}), request,
        )
        await responder
        assert result.state == 'failed' and not target.exists()


async def test_enrollment_token_is_not_sent_to_pc(channel_setup, execution):
    relay, _, account, device_id, client = channel_setup
    pc, sign, _, _, _ = execution
    async with client() as socket:
        await wait_connected(relay, device_id)
        with pytest.raises(ExecutionRejected):
            await relay.exchange_authorized(
                pc.verifier, account, device_id,
                sign({'device_id': device_id, 'scope': 'device:enroll'}),
                Request(operation_id='9' * 32, tool='__catalog'),
            )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(socket.recv(), 0.05)


async def test_plain_requests_and_malformed_frames_do_not_reach_engine(execution):
    pc, _, _, root, _ = execution
    target = root / 'unsigned.txt'
    request = Request(operation_id='a' * 32, tool='files_write',
                      arguments={'path': str(target), 'text': 'must not write'})
    for payload in (request.model_dump_json().encode(), b'{"token":"secret-fixture"}'):
        with pytest.raises(ExecutionRejected) as error:
            await pc.dispatch_frame(payload)
        assert 'secret-fixture' not in str(error.value)
    assert not target.exists()


async def test_relay_withholds_reply_when_grant_revoked_during_execution(channel_setup, execution):
    relay, _, account, device_id, client = channel_setup
    pc, sign, _, root, _ = execution
    pc.device_id = device_id
    relay_state = {'active': True}
    verifier = ExecutionVerifier(pc.verifier.tokens, is_current=lambda _: relay_state['active'])
    target = root / 'committed.txt'
    request = Request(operation_id='b' * 32, tool='files_write',
                      arguments={'path': str(target), 'text': 'committed'})
    async with client() as socket:
        await wait_connected(relay, device_id)
        async def revoke_before_reply():
            payload = await socket.recv()
            reply = await pc.dispatch_frame(payload)
            relay_state['active'] = False
            await socket.send(reply)
        responder = asyncio.create_task(revoke_before_reply())
        with pytest.raises(ChannelOutcomeUnknown, match='withheld'):
            await relay.exchange_authorized(
                verifier, account, device_id, sign({'device_id': device_id}), request,
            )
        await responder
    assert target.read_text(encoding='utf-8') == 'committed'
