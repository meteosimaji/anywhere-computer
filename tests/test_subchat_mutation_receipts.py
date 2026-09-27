"""Committed queue mutations survive gateway cache eviction and controller restart."""

import asyncio

import pytest
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer.models import Reply, Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_content import SubchatResources
from anywhere_computer.subchat_gateway import (
    LazySubchatGateway,
    SubchatGateway,
    SubchatGatewayConfig,
)
from anywhere_computer.subchat_mcp import _mutation_digest, session
from anywhere_computer.subchat_state import SubchatRequestConflict, SubchatSubmissions


def queued(store, owner='owner'):
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'parent', 'old', 'normal', owner=owner,
                  conversation_id='conversation')
    store.begin_send(parent, owner=owner, user_message_id='user',
                     provider_account_id='account')
    store.submitted(parent, 'conversation', 'user', owner=owner)
    store.complete(parent, 'answer', 'done', owner=owner)
    store.prepare(child, 'child', 'old', 'normal', owner=owner,
                  conversation_id='conversation', after_operation_id=parent)
    return child


def test_queue_mutations_have_atomic_durable_receipts(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        child = queued(store)
        model_request = Request(operation_id='1' * 32, tool='subchat_queue_model_change',
                                arguments={'operation_id': child, 'expected_revision': 0,
                                           'model': 'new', 'effort': 'high'})
        model_digest = _mutation_digest(model_request)
        first = store.change_queued_model(
            child, owner='owner', expected_revision=0, model='new', effort='high',
            http_selection=None, request_id=model_request.operation_id, digest=model_digest)
        assert first[1] == 1
        refs = SubchatResources.model_validate({'attachments': [{
            'id': 'file_uploaded', 'name': 'note.txt', 'mime_type': 'text/plain', 'size': 4}]})
        resource_request = Request(operation_id='2' * 32,
                                   tool='subchat_queue_resources_change',
                                   arguments={'operation_id': child, 'expected_revision': 1,
                                              'resources': refs.model_dump(mode='json')})
        resource_digest = _mutation_digest(resource_request)
        changed = store.change_queued_resources(
            child, owner='owner', expected_revision=1, resources=refs,
            request_id=resource_request.operation_id, digest=resource_digest)
        assert changed[1] == 2
        assert store.change_queued_model(
            child, owner='owner', expected_revision=0, model='new', effort='high',
            http_selection=None, request_id=model_request.operation_id,
            digest=model_digest) == first
        assert store.change_queued_resources(
            child, owner='owner', expected_revision=1, resources=refs,
            request_id=resource_request.operation_id, digest=resource_digest) == changed
        assert store.queue_revision(child, owner='owner') == 2
        with pytest.raises(SubchatRequestConflict):
            store.mutation_receipt(model_request.operation_id, owner='owner',
                                   tool=model_request.tool, digest='different')
        with pytest.raises(SubchatRequestConflict):
            store.mutation_receipt(model_request.operation_id, owner='another-owner',
                                   tool=model_request.tool, digest=model_digest)

        original_save = store._save_mutation_receipt

        def fail_before_commit(*args, **kwargs):
            raise RuntimeError('receipt write failed')

        monkeypatch.setattr(store, '_save_mutation_receipt', fail_before_commit)
        with pytest.raises(RuntimeError, match='receipt write failed'):
            store.change_queued_resources(
                child, owner='owner', expected_revision=2, resources=SubchatResources(),
                request_id='3' * 32, digest='rollback')
        monkeypatch.setattr(store, '_save_mutation_receipt', original_save)
        assert store.queue_revision(child, owner='owner') == 2
        assert store.get(child, owner='owner').resources == refs
    finally:
        ledger.close()
    reopened = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(reopened.connection)
        assert store.mutation_receipt(model_request.operation_id, owner='owner',
                                      tool=model_request.tool, digest=model_digest) is not None
        assert store.change_queued_resources(
            child, owner='owner', expected_revision=1, resources=refs,
            request_id=resource_request.operation_id, digest=resource_digest) == changed
        assert store.queue_revision(child, owner='owner') == 2
    finally:
        reopened.close()


def test_cancel_request_id_cannot_be_reused_for_another_submission(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        first = queued(store)
        second = 'c' * 32
        store.prepare(second, 'another queue', 'old', 'normal', owner='owner',
                      conversation_id='conversation', after_operation_id='a' * 32)
        request_id = 'd' * 32
        original = store.cancel(first, owner='owner', request_id=request_id,
                                digest='first-request')
        assert original.state == 'cancelled'
        assert store.cancel(first, owner='owner', request_id=request_id,
                            digest='first-request') == original
        with pytest.raises(SubchatRequestConflict):
            store.cancel(second, owner='owner', request_id=request_id,
                         digest='second-request')
        assert store.get(second, owner='owner').state == 'queued'
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_lazy_gateway_cancel_replay_checks_durable_receipt(tmp_path):
    ledger_path = tmp_path / 'ledger'
    ledger = Ledger(ledger_path)
    store = SubchatSubmissions(ledger.connection)
    first = queued(store)
    second = 'c' * 32
    store.prepare(second, 'another queue', 'old', 'normal', owner='owner',
                  conversation_id='conversation', after_operation_id='a' * 32)
    gateway = LazySubchatGateway(SubchatGatewayConfig(
        profile=str(tmp_path / 'profile'), ledger=str(ledger_path),
        account_id='account', consent='ordinary-chat-browser-control-approved'),
        owner='owner')
    request_id = 'd' * 32
    tools = frozenset({'subchat_cancel'})
    try:
        request = Request(operation_id=request_id, tool='subchat_cancel',
                          arguments={'operation_id': first})
        first_result = await gateway.execute('owner', request, tools)
        assert first_result.state == 'completed'
        assert (await gateway.execute('owner', request, tools)).data == first_result.data
        conflict = await gateway.execute('owner', request.model_copy(update={
            'arguments': {'operation_id': second}}), tools)
        assert conflict.data['error_code'] == 'request_conflict'
        assert store.get(second, owner='owner').state == 'queued'
    finally:
        await gateway.close()
        ledger.close()


@pytest.mark.asyncio
async def test_new_oauth_grant_replay_reports_prior_mutation_as_uncertain(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'parent', 'old', 'normal', owner='owner',
                  conversation_id='conversation')
    store.begin_send(parent, owner='owner', user_message_id='user',
                     provider_account_id='account')
    store.submitted(parent, 'conversation', 'user', owner='owner')
    store.prepare(child, 'child', 'old', 'normal', owner='owner',
                  conversation_id='conversation', after_operation_id=parent)
    request = Request(operation_id='c' * 32, tool='subchat_queue_auto',
                      arguments={'operation_id': child})
    store.arm_auto_queue(child, owner='owner', lease_seconds=900,
                         authorization_grant_id='old-grant',
                         request_id=request.operation_id,
                         digest=_mutation_digest(request, 'old-grant'))
    server = session(Subchats(store, BrowserFixture()), owner='owner',
                     auto_queue_grant_active=lambda _: True)
    try:
        result = await server.execute_with_queue_grant(request, 'new-grant')
        assert result.state == 'failed'
        assert result.data['error_code'] == 'request_conflict'
        assert result.data['dispatched'] is None
        assert result.data['automatic_retry'] is False
        assert store.auto_queue_status(child, owner='owner')['state'] == 'armed'
    finally:
        await server.close()
        ledger.close()


@pytest.mark.asyncio
async def test_legacy_unbound_queue_can_be_disabled_over_https(tmp_path, monkeypatch):
    ledger_path = tmp_path / 'ledger'
    ledger = Ledger(ledger_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'parent', 'old', 'normal', owner='owner',
                  conversation_id='conversation')
    store.begin_send(parent, owner='owner')
    store.submitted(parent, 'conversation', 'user', owner='owner')
    store.prepare(child, 'child', 'old', 'normal', owner='owner',
                  conversation_id='conversation', after_operation_id=parent)
    store.arm_auto_queue(child, owner='owner', lease_seconds=900)
    direct = SubchatGateway(
        lambda owner: session(Subchats(store, BrowserFixture()), owner=owner),
        owner='owner', account_id='account')
    lazy = LazySubchatGateway(SubchatGatewayConfig(
        profile=str(tmp_path / 'profile'), ledger=str(ledger_path),
        account_id='account', consent='ordinary-chat-browser-control-approved'),
        owner='owner')

    async def acquire():
        return direct

    async def release():
        return None

    monkeypatch.setattr(lazy, '_acquire', acquire)
    monkeypatch.setattr(lazy, '_release', release)
    request = Request(operation_id='d' * 32, tool='subchat_queue_auto',
                      arguments={'operation_id': child, 'enabled': False})
    try:
        result = await lazy.execute('owner', request, frozenset({'subchat_queue_auto'}))
        assert result.state == 'completed'
        assert store.auto_queue_status(child, owner='owner')['state'] == 'disabled'
    finally:
        await direct.close()
        await lazy.close()
        ledger.close()


@pytest.mark.asyncio
async def test_old_auto_enable_cannot_rearm_after_disable_eviction_or_restart(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    child = queued(store)

    class Core:
        async def close(self):
            pass

        async def execute_with_queue_grant(self, request, grant_id):
            return await self.run(request, grant_id)

        async def execute(self, request):
            return await self.run(request, None)

        async def run(self, request, grant_id):
            if request.tool == 'subchat_activity':
                return Reply(operation_id=request.operation_id, state='completed')
            digest = _mutation_digest(request, grant_id)
            if request.arguments['enabled']:
                status = store.arm_auto_queue(
                    child, owner='owner', lease_seconds=900,
                    authorization_grant_id=grant_id, request_id=request.operation_id,
                    digest=digest)
            else:
                store.disable_auto_queue(child, owner='owner',
                                         request_id=request.operation_id, digest=digest)
                status = store.auto_queue_status(child, owner='owner')
            return Reply(operation_id=request.operation_id, state='completed', data=status)

    core = Core()
    gateway = SubchatGateway(lambda _: core, owner='owner')
    tools = frozenset({'subchat_queue_auto', 'subchat_activity'})
    release = asyncio.Event()

    async def call(index, enabled, *, grant='oauth-grant'):
        request = Request(operation_id=f'{index:032x}', tool='subchat_queue_auto',
                          arguments={'operation_id': child, 'enabled': enabled})
        return await gateway.execute('owner', request, tools,
                                     authorization_grant_id=grant)

    try:
        enabled = await call(1, True)
        assert enabled.data['state'] == 'armed'
        assert (await call(2, False)).data['state'] == 'disabled'
        for index in range(3, 130):
            await gateway.execute('owner', Request(
                operation_id=f'{index:032x}', tool='subchat_activity'), tools)
        assert ('owner', f'{1:032x}') not in gateway.pending
        replay = await call(1, True)
        assert replay.data == enabled.data
        assert store.auto_queue_status(child, owner='owner')['state'] == 'disabled'
        changed_grant = await call(1, True, grant='different-grant')
        assert changed_grant.state == 'failed'
        assert changed_grant.data['dispatched'] is None
        assert store.auto_queue_status(child, owner='owner')['state'] == 'disabled'

        # The full-cache direct fallback also passes through the SQLite guard.
        gateway.pending.clear()

        async def unresolved():
            await release.wait()
            return Reply(operation_id='0' * 32, state='running')

        for index in range(128):
            gateway.pending[('owner', f'{index + 1000:032x}')] = (
                'subchat_send', '{}', asyncio.create_task(unresolved()))
        assert (await call(200, True)).state == 'completed'
        assert (await call(201, False)).state == 'completed'
        assert (await call(200, True)).state == 'completed'
        assert store.auto_queue_status(child, owner='owner')['state'] == 'disabled'
        release.set()
    finally:
        release.set()
        await gateway.close()
        ledger.close()

    reopened = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(reopened.connection)
        request = Request(operation_id=f'{1:032x}', tool='subchat_queue_auto',
                          arguments={'operation_id': child, 'enabled': True})
        assert store.arm_auto_queue(
            child, owner='owner', lease_seconds=900,
            authorization_grant_id='oauth-grant', request_id=request.operation_id,
            digest=_mutation_digest(request, 'oauth-grant')) == enabled.data
        assert store.auto_queue_status(child, owner='owner')['state'] == 'disabled'
        with pytest.raises(SubchatRequestConflict):
            store.mutation_receipt(request.operation_id, owner='owner',
                                   tool=request.tool,
                                   digest=_mutation_digest(request, 'different-grant'))
    finally:
        reopened.close()
