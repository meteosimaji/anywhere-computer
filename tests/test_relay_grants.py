import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_connection import agent as agent

from anywhere_computer.engine import Engine
from anywhere_computer.models import Request
from anywhere_computer.relay_grants import AuthorizedRelayAgent, ExecutionVerifier
from anywhere_computer.relay_registry import RelayAccount
from anywhere_computer.relay_tokens import SignedRelayToken
from anywhere_computer.remote_bridge import RemoteAgent


@pytest.fixture
async def execution(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    tokens = SignedRelayToken(issuer='https://issuer.example',
                             audience='https://relay.example/execute', client='ai',
                             public_keys={'test': public})
    current = {'active': True}
    engine = Engine(tmp_path / 'engine')
    verifier = ExecutionVerifier(tokens, is_current=lambda _: current['active'])
    pc = AuthorizedRelayAgent(RemoteAgent(engine, {}), verifier,
                              account=RelayAccount(issuer=tokens.issuer, subject='owner'),
                              device_id='a' * 32)
    now = int(time.time())
    claims = dict(iss=tokens.issuer, sub='owner', aud=tokens.audience, azp='ai', typ='Bearer',
                  iat=now, exp=now + 60, scope='device:execute', device_id='a' * 32,
                  grant_id='b' * 32, tools=['files_write', 'operations_get'])
    def sign(change=None):
        return jwt.encode({**claims, **(change or {})}, key, algorithm='RS256',
                          headers={'kid': 'test'})
    try:
        yield pc, sign, current, tmp_path, claims
    finally:
        await engine.close()


async def test_pc_verifies_write_refresh_recovery_and_revocation(execution):
    pc, sign, current, root, claims = execution
    target = root / '日本語.txt'
    write = Request(operation_id='1' * 32, tool='files_write',
                    arguments={'path': str(target), 'text': '日本語 🚀'})
    assert (await pc.dispatch(sign(), write)).state == 'completed'
    assert target.read_text(encoding='utf-8') == '日本語 🚀'
    lookup = Request(operation_id='2' * 32, tool='operations_get',
                     arguments={'operation_id': write.operation_id})
    result = await pc.dispatch(sign({'exp': claims['exp'] + 60}), lookup)
    assert result.state == 'completed' and result.data['state'] == 'completed'
    assert (await pc.dispatch(sign({'grant_id': 'c' * 32}), lookup)).state == 'failed'
    current['active'] = False
    assert (await pc.dispatch(sign(), lookup)).state == 'failed'
    # No bearer material is persisted in the engine ledger.
    for file in (root / 'engine').rglob('*'):
        if file.is_file():
            assert sign().encode() not in file.read_bytes()


@pytest.mark.parametrize('change', [
    {'scope': 'device:enroll'}, {'aud': 'https://relay.example/enrollment'},
    {'sub': 'other'}, {'iss': 'https://other.example'}, {'device_id': 'd' * 32},
    {'azp': 'desktop'}, {'tools': ['operations_get']}, {'exp': 1}, {'exp': True},
    {'iat': 9999999999}, {'grant_id': 'invalid'}, {'typ': 'ID'},
    {'tools': ['files_write', 'unknown_tool']}, {'tools': ['files_write', 'files_write']},
])
async def test_pc_rejects_before_file_mutation(execution, change):
    pc, sign, _, root, _ = execution
    target = root / 'must-not-exist.txt'
    request = Request(operation_id='3' * 32, tool='files_write',
                      arguments={'path': str(target), 'text': 'rejected'})
    token = sign(change)
    reply = await pc.dispatch(token, request)
    assert reply.state == 'failed'
    assert not target.exists()
    assert token not in reply.model_dump_json()
    assert pc.agent.grants == {}


@pytest.mark.parametrize('missing', ['device_id', 'grant_id', 'tools', 'exp', 'scope'])
async def test_missing_execution_claims_cannot_dispatch(execution, missing):
    pc, _, _, root, claims = execution
    # Use a valid signature on an incomplete payload, not an unsigned-token failure.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = other.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    pc.verifier.tokens = SignedRelayToken(issuer=claims['iss'], audience=claims['aud'], client='ai',
                                          public_keys={'test': public})
    incomplete = {k: v for k, v in claims.items() if k != missing}
    token = jwt.encode(incomplete, other, algorithm='RS256', headers={'kid': 'test'})
    target = root / 'absent.txt'
    reply = await pc.dispatch(token, Request(operation_id='4' * 32, tool='files_write',
                                            arguments={'path': str(target), 'text': 'no'}))
    assert reply.state == 'failed' and not target.exists()


async def test_catalog_is_limited_to_verified_tools(execution):
    pc, sign, _, _, _ = execution
    reply = await pc.dispatch(sign(), Request(operation_id='5' * 32, tool='__catalog'))
    assert reply.state == 'completed'
    assert {item['name'] for item in reply.data['tools']} == {'files_write', 'operations_get'}


async def test_signed_relay_uses_the_existing_local_engine(execution, agent, monkeypatch):
    import anywhere_computer.connection as connection

    original, sign, current, root, _ = execution
    directory, credential = agent
    monkeypatch.setattr(connection, 'local_credential', lambda _: credential)
    shared = AuthorizedRelayAgent(directory, original.verifier, account=original.account,
                                  device_id=original.device_id)
    local = await connection.exchange(directory, '__status', credential=credential)
    status = await shared.dispatch(sign({'tools': ['computer_status']}),
                                  Request(operation_id='8' * 32, tool='computer_status'))
    assert status.state == 'completed'
    assert status.data['instance_id'] == local.data['instance_id']
    target = root / 'shared-engine.txt'
    write = Request(operation_id='9' * 32, tool='files_write',
                    arguments={'path': str(target), 'text': '共有エンジン ✅'})
    result = await shared.dispatch(sign(), write)
    assert result.state == 'completed'
    lookup = await shared.dispatch(sign(), Request(operation_id='a' * 32, tool='operations_get',
                                  arguments={'operation_id': write.operation_id}))
    assert lookup.data == result.model_dump(mode='json')
    rejected = await shared.dispatch(sign({'tools': ['files_write', 'unknown_tool']}),
                                    write.model_copy(update={'operation_id': 'b' * 32}))
    assert rejected.state == 'failed' and 'before dispatch' in rejected.error
    current['active'] = False
    assert (await shared.dispatch(sign(), write)).state == 'failed'
    after = await connection.exchange(directory, '__status', credential=credential)
    assert after.data['instance_id'] == local.data['instance_id']
