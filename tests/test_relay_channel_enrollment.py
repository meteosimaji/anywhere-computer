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
