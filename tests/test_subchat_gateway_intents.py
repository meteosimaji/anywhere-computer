"""Real gateway/core/SQLite intent recovery; only provider I/O is synthetic."""

import pytest

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatReceipt, Subchats
from anywhere_computer.subchat_gateway import SubchatGateway
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatRequestConflict, SubchatSubmissions

SCOPES = frozenset({'subchat_send', 'subchat_recover', 'subchat_status'})
CANONICAL = 'a' * 32
INTENT = 'b' * 32
ARGS = {'prompt': 'synthetic completed work', 'model': 'model', 'effort': 'effort',
        'intent_key': INTENT}


class Provider:
    def __init__(self):
        self.sent = []

    async def prepare(self, submission):
        return ()

    async def send(self, submission):
        self.sent.append(submission.operation_id)
        return SubchatReceipt(conversation_id='chat-' + submission.operation_id,
                              user_message_id='user-' + submission.operation_id,
                              prompt=submission.prompt)


@pytest.fixture
async def fixture(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    store.prepare(CANONICAL, ARGS['prompt'], 'model', 'effort', owner='owner-a',
                  intent_key=INTENT)
    store.begin_send(CANONICAL, owner='owner-a', user_message_id='user',
                     provider_account_id='account-a')
    store.submitted(CANONICAL, 'chat', 'user', owner='owner-a')
    store.complete(CANONICAL, 'answer', 'synthetic answer', owner='owner-a')
    gateways = []

    def make_gateway():
        gateway = SubchatGateway(lambda owner: session(
            Subchats(store, provider), owner=owner, require_send_intent=True),
            owner='owner', account_id='account-a')
        gateways.append(gateway)
        return gateway

    try:
        yield ledger, store, provider, make_gateway
    finally:
        for gateway in gateways:
            await gateway.close()
        ledger.close()


async def test_completed_intent_aliases_do_not_exhaust_gateway(fixture):
    ledger, store, provider, make_gateway = fixture
    gateway = make_gateway()
    for number in range(1, 131):
        reply = await gateway.execute('owner-a', Request(operation_id=f'{number:032x}',
            tool='subchat_send', arguments=ARGS), SCOPES)
        assert reply.state == 'completed' and reply.data['operation_id'] == CANONICAL
    assert provider.sent == []
    assert ledger.connection.execute('SELECT count(*) FROM subchat_submissions').fetchone() == (1,)
    recovered = await gateway.execute('owner-a', Request(operation_id='c' * 32,
        tool='subchat_recover', arguments={'operation_id': CANONICAL}), SCOPES)
    assert recovered.state == 'completed' and recovered.data['state'] == 'completed'
    fresh = await gateway.execute('owner-a', Request(operation_id='d' * 32,
        tool='subchat_send', arguments={**ARGS, 'intent_key': 'e' * 32,
                                      'prompt': 'synthetic unrelated work'}), SCOPES)
    assert fresh.state == 'completed' and fresh.data['operation_id'] == 'd' * 32
    assert provider.sent == ['d' * 32]
    assert store.get(CANONICAL, owner='owner-a').state == 'completed'
    assert ('owner-a', f'{1:032x}') not in gateway.pending
    rebound = await gateway.execute('owner-a', Request(operation_id=f'{1:032x}',
        tool='subchat_send', arguments={**ARGS, 'intent_key': 'f' * 32}), SCOPES)
    assert rebound.state == 'failed' and rebound.data['error_code'] == 'request_conflict'
    assert provider.sent == ['d' * 32]


@pytest.mark.parametrize('change', [
    {'prompt': 'changed'}, {'model': 'other'}, {'effort': 'more'},
    {'intent_key': 'c' * 32}, {'resources': {'attachments': [
        {'id': 'file_fixture', 'name': 'note.txt', 'mime_type': 'text/plain', 'size': 4}]}},
])
async def test_alias_input_binding_survives_gateway_restart(fixture, change):
    ledger, store, provider, make_gateway = fixture
    gateway = make_gateway()
    alias = '1' * 32
    first = await gateway.execute('owner-a', Request(operation_id=alias,
        tool='subchat_send', arguments=ARGS), SCOPES)
    assert first.state == 'completed' and first.data['operation_id'] == CANONICAL
    await gateway.close()
    restored = make_gateway()
    changed = await restored.execute('owner-a', Request(operation_id=alias,
        tool='subchat_send', arguments={**ARGS, **change}), SCOPES)
    assert changed.state == 'failed' and changed.data['error_code'] == 'request_conflict'
    assert provider.sent == []
    assert ledger.connection.execute('SELECT count(*) FROM subchat_submissions').fetchone() == (1,)
    assert store.get(CANONICAL, owner='owner-a').state == 'completed'


async def test_alias_cannot_be_rebound_to_another_tool_after_restart(fixture):
    _, _, provider, make_gateway = fixture
    gateway = make_gateway()
    alias = '1' * 32
    await gateway.execute('owner-a', Request(operation_id=alias,
        tool='subchat_send', arguments=ARGS), SCOPES)
    await gateway.close()
    changed = await make_gateway().execute('owner-a', Request(operation_id=alias,
        tool='subchat_status', arguments={'operation_id': CANONICAL}), SCOPES)
    assert changed.state == 'failed' and changed.data['error_code'] == 'request_conflict'
    assert provider.sent == []


async def test_uncertain_aliases_keep_exact_owned_recovery_and_do_not_leak_other_owner(fixture):
    _, store, provider, make_gateway = fixture
    unknown = 'f' * 32
    args = {**ARGS, 'intent_key': '0' * 32, 'prompt': 'synthetic uncertain work'}
    store.prepare(unknown, args['prompt'], 'model', 'effort', owner='owner-a',
                  intent_key=args['intent_key'])
    store.begin_send(unknown, owner='owner-a', user_message_id='uncertain-user',
                     provider_account_id='account-a')
    gateway = make_gateway()
    for number in range(1000, 1128):
        reply = await gateway.execute('owner-a', Request(operation_id=f'{number:032x}',
            tool='subchat_send', arguments=args), SCOPES)
        assert reply.state == 'completed' and reply.data['state'] == 'sending'
    fresh_request = Request(operation_id='c' * 32, tool='subchat_send',
                            arguments={**ARGS, 'intent_key': 'd' * 32})
    blocked = await gateway.execute('owner-a', fresh_request, SCOPES)
    assert blocked.state == 'failed' and blocked.data['dispatched'] is False
    assert blocked.data['retained_requests'] == 128
    assert blocked.data['recoverable_submission_ids'] == [unknown]
    assert len(gateway.pending) == 128 and provider.sent == []
    outsider = await gateway.execute('owner-b', fresh_request, SCOPES)
    assert outsider.state == 'failed' and outsider.data['recoverable_submission_ids'] == []
    assert unknown not in outsider.model_dump_json()
    assert len(gateway.pending) == 128 and provider.sent == []

    store.submitted(unknown, 'uncertain-chat', 'uncertain-user', owner='owner-a')
    store.complete(unknown, 'uncertain-answer', 'synthetic final', owner='owner-a')
    recovered = await gateway.execute('owner-a', Request(operation_id='e' * 32,
        tool='subchat_recover', arguments={'operation_id': unknown}), SCOPES)
    assert recovered.data['operation_id'] == unknown and recovered.data['state'] == 'completed'
    accepted = await gateway.execute('owner-a', fresh_request, SCOPES)
    assert accepted.state == 'completed' and provider.sent == ['c' * 32]


async def test_alias_binding_survives_ledger_reopen(fixture, tmp_path):
    ledger, _, provider, make_gateway = fixture
    gateway = make_gateway()
    alias = '1' * 32
    await gateway.execute('owner-a', Request(operation_id=alias,
        tool='subchat_send', arguments=ARGS), SCOPES)
    await gateway.close()
    ledger.close()
    reopened = Ledger(tmp_path)
    restored = session(Subchats(SubchatSubmissions(reopened.connection), provider),
                       owner='owner-a', require_send_intent=True)
    try:
        reply = await restored.execute(Request(operation_id=alias, tool='subchat_send',
            arguments={**ARGS, 'intent_key': 'c' * 32}))
        assert reply.state == 'failed' and reply.data['error_code'] == 'request_conflict'
        assert provider.sent == []
    finally:
        await restored.close()
        reopened.close()


async def test_completed_receipts_from_another_account_do_not_justify_alias_cleanup(fixture):
    _, _, provider, make_gateway = fixture
    gateway = make_gateway()
    for number in range(1, 129):
        await gateway.execute('owner-a', Request(operation_id=f'{number:032x}',
            tool='subchat_send', arguments=ARGS), SCOPES)
    gateway.account_id = 'account-b'
    blocked = await gateway.execute('owner-a', Request(operation_id='c' * 32,
        tool='subchat_send', arguments={**ARGS, 'intent_key': 'd' * 32}), SCOPES)
    assert blocked.state == 'failed' and blocked.data['recoverable_submission_ids'] == []
    assert len(gateway.pending) == 128 and provider.sent == []


async def test_transport_alias_cannot_become_new_submission_through_direct_service(fixture):
    _, store, provider, make_gateway = fixture
    gateway = make_gateway()
    alias = '1' * 32
    await gateway.execute('owner-a', Request(operation_id=alias,
        tool='subchat_send', arguments=ARGS), SCOPES)
    with pytest.raises(SubchatRequestConflict):
        await Subchats(store, provider).send(alias, 'changed', 'model', 'effort', owner='owner-a')
    assert provider.sent == []


async def test_persisted_alias_cannot_be_rebound_to_another_owner(fixture):
    _, store, provider, make_gateway = fixture
    gateway = make_gateway()
    alias = '1' * 32
    await gateway.execute('owner-a', Request(operation_id=alias,
        tool='subchat_send', arguments=ARGS), SCOPES)
    await gateway.close()
    foreign = '2' * 32
    store.prepare(foreign, ARGS['prompt'], 'model', 'effort', owner='owner-b', intent_key=INTENT)
    store.begin_send(foreign, owner='owner-b', user_message_id='foreign-user',
                     provider_account_id='account-b')
    store.submitted(foreign, 'foreign-chat', 'foreign-user', owner='owner-b')
    store.complete(foreign, 'foreign-answer', 'foreign final', owner='owner-b')
    changed = await make_gateway().execute('owner-b', Request(operation_id=alias,
        tool='subchat_send', arguments=ARGS), SCOPES)
    assert changed.state == 'failed' and changed.data['error_code'] == 'request_conflict'
    assert CANONICAL not in changed.model_dump_json() and provider.sent == []
