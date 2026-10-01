from contextlib import asynccontextmanager

import pytest

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAccessError
from anywhere_computer.subchat_gateway import LazySubchatGateway, SubchatGatewayConfig
from anywhere_computer.subchat_state import SubchatSubmissions


@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('tool', ['subchat_catalog', 'subchat_send', 'subchat_recover'])
async def test_startup_access_rejection_preserves_typed_reason_and_unknown_send(
    monkeypatch, tmp_path, status, tool,
):
    import anywhere_computer.subchat_gateway as gateway_module

    attempts = 0

    @asynccontextmanager
    async def rejected_gateway(config, *, owner):
        nonlocal attempts
        attempts += 1
        error = SubchatAccessError(status)
        error.args = ('private provider URL and credential fixture',)
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(gateway_module, 'open_subchat_gateway', rejected_gateway)
    selected = SubchatGatewayConfig(
        profile=str(tmp_path / 'Default'), ledger=str(tmp_path / 'ledger'),
        account_id='account', consent='ordinary-chat-browser-control-approved')
    ledger = Ledger(tmp_path / 'ledger')
    store = SubchatSubmissions(ledger.connection)
    original_id, intent_key = 'c' * 32, 'd' * 32
    store.prepare(original_id, 'already sent', 'model', 'effort', owner='grant',
                  intent_key=intent_key)
    store.begin_send(original_id, owner='grant', user_message_id='sent-user',
                     provider_account_id='account')
    gateway = LazySubchatGateway(selected, owner='owner')
    try:
        allowed = frozenset({tool, 'subchat_send', 'subchat_status'})
        reply = await gateway.execute('grant', Request(
            operation_id='a' * 32, tool=tool,
            arguments={'prompt': 'new input'} if tool == 'subchat_send' else {}), allowed)
        assert reply.operation_id == 'a' * 32 and reply.state == 'failed'
        assert reply.data['error_code'] == (
            'authentication_required' if status == 401 else 'access_denied')
        assert reply.data['automatic_retry'] is False
        if tool in {'subchat_send', 'subchat_catalog'}:
            assert reply.data['dispatched'] is False
        else:
            assert 'dispatched' not in reply.data
        assert 'saved' in reply.error.lower() and 'recover' in reply.error.lower()
        assert 'private provider' not in str(reply)

        # Startup refusal cannot relabel an existing, unconfirmed send as unsent.
        existing = await gateway.execute('grant', Request(
            operation_id=original_id, tool='subchat_send',
            arguments={'prompt': 'already sent'}), allowed)
        rebound = await gateway.execute('grant', Request(
            operation_id='e' * 32, tool='subchat_send',
            arguments={'prompt': 'already sent', 'intent_key': intent_key}), allowed)
        for saved in (existing, rebound):
            assert saved.data['operation_id'] == original_id
            assert saved.data['state'] == 'sending'
            assert saved.data['provider_receipt'] == 'unconfirmed'
            assert saved.data.get('dispatched') is not False
        assert attempts == 1  # Existing cooldown remains; no auth/generation replay.
    finally:
        await gateway.close()
        ledger.close()
