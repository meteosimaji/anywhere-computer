import asyncio
import json

import pytest
from test_relay_channels import wait_connected
from test_relay_enrollment import registration as registration
from test_relay_enrollment import signed
from test_remote_transport import certificates as certificates
from websockets.asyncio.client import connect

from anywhere_computer.relay_channels import SIGNED_PC_PROTOCOL, RelayChannels
from anywhere_computer.relay_registry import RelayAccount


@pytest.mark.parametrize('invalid', [None, 'other_account', 'execution_scope', 'expired'])
async def test_first_tls_connection_binds_only_authenticated_enrolled_device(
    registration, certificates, invalid,
):
    key, registry, service, claims = registration
    context, fingerprint = certificates
    token = signed(key, claims)
    device = service.register(token, enrollment_id='a' * 32, name='PC')
    altered = dict(claims)
    if invalid == 'other_account':
        altered['sub'] = 'other'
    elif invalid == 'execution_scope':
        altered['scope'] = 'files_write'
    elif invalid == 'expired':
        altered['exp'] = 1
    relay = RelayChannels(registry, context('server', False), enrollment=service)
    port = await relay.start()
    try:
        async with connect(
            f'wss://localhost:{port}/pc/enroll', ssl=context('client', True), proxy=None,
            subprotocols=[SIGNED_PC_PROTOCOL], additional_headers={
                'Authorization': 'Bearer ' + signed(key, altered),
                'X-Anywhere-Device': device.device_id,
                # A claimed fingerprint must never override the actual TLS peer.
                'X-Anywhere-Fingerprint': fingerprint('stranger'),
            },
        ) as pc:
            if invalid is not None:
                await asyncio.wait_for(pc.wait_closed(), 2)
                assert pc.close_code == 1008
                assert registry.db.execute('SELECT COUNT(*) FROM relay_channels').fetchone()[0] == 0
            else:
                receipt = json.loads(await asyncio.wait_for(pc.recv(), 2))
                assert receipt == {'version': 1, 'state': 'bound',
                                   'device_id': device.device_id,
                                   'fingerprint': fingerprint('client')}
                assert device.device_id not in relay._channels
                assert registry.channel_device(fingerprint('client')) == (
                    RelayAccount(issuer=claims['iss'], subject=claims['sub']), device,
                )
                with pytest.raises(ValueError):
                    registry.channel_device(fingerprint('stranger'))
                # Registration establishes a connection, not an operation or AI grant.
                await asyncio.wait_for(pc.wait_closed(), 2)
                assert pc.close_code == 1000
        if invalid is None:
            previous = relay._channels.get(device.device_id)
            async with connect(
                f'wss://localhost:{port}/pc', ssl=context('client', True), proxy=None,
                subprotocols=[SIGNED_PC_PROTOCOL],
            ):
                await wait_connected(relay, device.device_id, previous=previous)
                assert registry.channel_device(fingerprint('client'))[1] == device
    finally:
        await relay.close()


async def test_pc_enrolls_then_runs_signed_file_operation(registration, certificates, tmp_path):
    import time

    from cryptography.hazmat.primitives import serialization
    from test_client_tokens import MemoryVault

    from anywhere_computer.engine import Engine
    from anywhere_computer.enrollment_credentials import EnrollmentCredentials, EnrollmentToken
    from anywhere_computer.enrollment_http import EnrollmentHTTPReply
    from anywhere_computer.models import Request
    from anywhere_computer.registration_client import RegistrationClient
    from anywhere_computer.relay_client import PCRelayClient
    from anywhere_computer.relay_grants import AuthorizedRelayAgent, ExecutionVerifier
    from anywhere_computer.relay_tokens import SignedRelayToken
    from anywhere_computer.remote_bridge import RemoteAgent

    key, registry, service, claims = registration
    context, fingerprint = certificates
    token = signed(key, claims)
    credentials = EnrollmentCredentials(tmp_path / 'credentials', issuer=claims['iss'],
                                        client=claims['azp'], profile='test', vault=MemoryVault())
    credentials.save('b' * 32, EnrollmentToken(access_token=token, token_type='Bearer',
                     expires_in=60, scope='device:enroll'), requested_at=time.time())

    def registration_wire(endpoint, fields, bearer):
        if endpoint.endswith('/account'):
            return EnrollmentHTTPReply(200, service.account(bearer).model_dump())
        return EnrollmentHTTPReply(200, service.register(bearer, **fields).model_dump())

    registration_client = RegistrationClient(
        tmp_path / 'registration', credentials, endpoint='https://relay.example/register',
        account_endpoint='https://relay.example/account', wire=registration_wire,
    )
    saved = registration_client.register(attempt_id='b' * 32, name='PC')
    device, owner = saved.device, saved.owner
    assert device is not None and owner is not None
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    verifier = ExecutionVerifier(SignedRelayToken(
        issuer=claims['iss'], audience='https://relay.example/execute', client='ai',
        public_keys={'fixture': public},
    ), is_current=lambda _: True)
    engine = Engine(tmp_path / 'pc-engine')
    pc = AuthorizedRelayAgent(RemoteAgent(engine, {}), verifier,
                              account=owner, device_id=device.device_id)
    relay = RelayChannels(registry, context('server', False), enrollment=service)
    port = await relay.start()
    client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
    task = None
    try:
        pc.device_id = '0' * 32
        with pytest.raises(ValueError, match='confirmed registration'):
            await registration_client.enroll_channel(client, fingerprint=fingerprint('client'))
        pc.device_id = device.device_id
        pc.account = RelayAccount(issuer=owner.issuer, subject='different-owner')
        with pytest.raises(ValueError, match='confirmed registration'):
            await registration_client.enroll_channel(client, fingerprint=fingerprint('client'))
        pc.account = owner
        assert registry.db.execute('SELECT COUNT(*) FROM relay_channels').fetchone()[0] == 0
        receipt = await registration_client.enroll_channel(
            client, fingerprint=fingerprint('client'),
        )
        assert receipt.device_id == device.device_id and client.state == 'enrolled'
        assert not relay._channels
        task = asyncio.create_task(client.run())
        await wait_connected(relay, device.device_id)
        grant = signed(key, {**claims, 'aud': 'https://relay.example/execute', 'azp': 'ai',
                             'scope': 'device:execute', 'device_id': device.device_id,
                             'grant_id': 'c' * 32, 'tools': ['files_write', 'operations_get']})
        target = tmp_path / 'first-connection.txt'
        result = await relay.exchange_authorized(verifier, owner, device.device_id, grant, Request(
            operation_id='d' * 32, tool='files_write',
            arguments={'path': str(target), 'text': '登録後の操作 ✅'},
        ))
        assert result.state == 'completed'
        assert target.read_text(encoding='utf-8') == '登録後の操作 ✅'
        recovered = await relay.exchange_authorized(verifier, owner, device.device_id, grant,
            Request(operation_id='e' * 32, tool='operations_get',
                    arguments={'operation_id': 'd' * 32}))
        assert recovered.data['state'] == 'completed'

        # Recreate the PC runtime using only its persisted state and TLS identity.
        previous_channel = relay._channels[device.device_id]
        previous_instance = engine.instance_id
        await client.stop()
        await asyncio.wait_for(task, 5)
        task = None
        await engine.close()
        engine = Engine(tmp_path / 'pc-engine')
        assert engine.instance_id != previous_instance
        pc = AuthorizedRelayAgent(RemoteAgent(engine, {}), verifier,
                                  account=owner, device_id=device.device_id)
        client = PCRelayClient(f'wss://localhost:{port}/pc', context('client', True), pc)
        task = asyncio.create_task(client.run())
        await wait_connected(relay, device.device_id, previous=previous_channel)
        assert registry.channel_device(fingerprint('client')) == (owner, device)
        recovered = await relay.exchange_authorized(verifier, owner, device.device_id, grant,
            Request(operation_id='f' * 32, tool='operations_get',
                    arguments={'operation_id': 'd' * 32}))
        assert recovered.data == result.model_dump(mode='json')

        # A changed file distinguishes ledger recovery from silently replaying a write.
        target.write_text('再起動後の別の編集 🔁', encoding='utf-8')
        duplicate = await relay.exchange_authorized(verifier, owner, device.device_id, grant,
            Request(operation_id='d' * 32, tool='files_write',
                    arguments={'path': str(target), 'text': '登録後の操作 ✅'}))
        assert duplicate == result
        assert target.read_text(encoding='utf-8') == '再起動後の別の編集 🔁'
    finally:
        await client.stop()
        if task is not None:
            await asyncio.wait_for(task, 5)
        await relay.close()
        await engine.close()
        registration_client.close()
