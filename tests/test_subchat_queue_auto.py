"""Durable opt-in queue delivery stays tied to one saved submission."""

import asyncio

import httpx
import pytest
from test_http_service import initialize

import anywhere_computer.subchat_mcp as subchat_mcp
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import (
    SubchatAccessError,
    SubchatAnswer,
    SubchatPreparedSend,
    SubchatReceipt,
    Subchats,
)
from anywhere_computer.subchat_gateway import (
    SUBCHAT_GATEWAY_TOOLS,
    SubchatGateway,
    subchat_ledger_owner,
)
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


class Provider:
    def __init__(self):
        self.sends: list[str] = []
        self.parent_ready = False
        self.child_ready = False
        self.lose_child_receipt = False

    async def prepare(self, submission):
        return ('parent-user',) if submission.after_operation_id else ()

    async def send(self, submission):
        self.sends.append(submission.operation_id)
        if submission.after_operation_id and self.lose_child_receipt:
            return None
        user_message_id = ('child-user' if submission.after_operation_id else
                           'another-parent-user' if submission.prompt == 'another parent' else
                           'parent-user')
        return SubchatReceipt(conversation_id='chat',
                              user_message_id=user_message_id, prompt=submission.prompt)

    async def find_submission(self, submission):
        return None

    async def read_answer(self, submission):
        is_child = submission.after_operation_id is not None
        if not (self.child_ready if is_child else self.parent_ready):
            return None
        return SubchatAnswer(conversation_id='chat',
                             user_message_id='child-user' if is_child else 'parent-user',
                             prompt=submission.prompt,
                             answer_message_id='child-answer' if is_child else 'parent-answer',
                             text='done')


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.01)


async def test_auto_queue_desktop_notice_only_for_opted_in_winning_epoch(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    original_platform = subchat_mcp.sys.platform
    calls = []

    class NoticeProcess:
        returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

    async def launch(*args, **kwargs):
        calls.append(args)
        return NoticeProcess()

    monkeypatch.setattr(subchat_mcp.asyncio, 'create_subprocess_exec', launch)
    ledger = Ledger(tmp_path)
    monkeypatch.setattr(subchat_mcp.sys, 'platform', 'darwin')
    try:
        store = SubchatSubmissions(ledger.connection)
        provider = Provider()
        service = Subchats(store, provider)
        parent, child = 'a' * 32, 'b' * 32
        await service.send(parent, 'first', 'model', 'effort', owner='alice')
        service.queue(child, parent, 'second', owner='alice')
        controller = session(service, owner='alice')
        try:
            armed = await controller.execute(Request(operation_id='c' * 32,
                tool='subchat_queue_auto', arguments={
                    'operation_id': child, 'notify_desktop': True}))
            assert armed.state == 'completed'
            assert armed.data['notify_desktop'] is True
            provider.parent_ready = True
            await _until(lambda: store.get(child, owner='alice').state == 'submitted')
            provider.child_ready = True
            await _until(lambda: store.auto_queue_status(child, owner='alice')['event']
                         == 'completed')
            await _until(lambda: len(calls) == 1)
            assert calls[0][0:2] == ('osascript', '-e')
            assert len(store.auto_queue_events(owner='alice')) == 1
            assert provider.sends == [parent, child]
            assert store.auto_queue_status(child, owner='alice')['notify_desktop'] is True
        finally:
            await controller.close()
    finally:
        ledger.close()
        monkeypatch.setattr(subchat_mcp.sys, 'platform', original_platform)
    reopened = Ledger(tmp_path)
    try:
        status = SubchatSubmissions(reopened.connection).auto_queue_status(
            child, owner='alice')
        assert status is not None and status['notify_desktop'] is True
    finally:
        reopened.close()


@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
@pytest.mark.parametrize('cleanup', ['exit', 'exit_race', 'stalled'])
async def test_desktop_notice_cleanup_preserves_saved_event_and_cancellation(
    tmp_path, monkeypatch, caplog, stop, cleanup,
):
    entered = asyncio.Event()
    exited = asyncio.Event()

    class NoticeProcess:
        returncode = None
        kill_calls = 0
        wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            entered.set()
            await exited.wait()
            return self.returncode

        def kill(self):
            self.kill_calls += 1
            if cleanup != 'stalled':
                self.returncode = -9
                exited.set()
            if cleanup == 'exit_race':
                raise ProcessLookupError('private subprocess detail')

    process = NoticeProcess()

    async def launch(*args, **kwargs):
        assert args[:2] == ('osascript', '-e')
        return process

    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .001)
    monkeypatch.setattr(subchat_mcp, '_DESKTOP_NOTICE_TIMEOUT', 1 if stop == 'cancel' else .01)
    monkeypatch.setattr(subchat_mcp, '_DESKTOP_NOTICE_CLEANUP_TIMEOUT', .01)
    monkeypatch.setattr(subchat_mcp.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(subchat_mcp.sys, 'platform', 'darwin')
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    service = Subchats(store, provider)
    controller = session(service, owner='alice')
    parent, child = 'a' * 32, 'b' * 32
    try:
        await service.send(parent, 'first', 'model', 'effort', owner='alice')
        service.queue(child, parent, 'second', owner='alice')
        armed = await controller.execute(Request(operation_id='c' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child, 'notify_desktop': True}))
        assert armed.state == 'completed'
        provider.parent_ready = True
        await _until(lambda: store.get(child, owner='alice').state == 'submitted')
        provider.child_ready = True
        await asyncio.wait_for(entered.wait(), 2)
        worker = controller.auto_queue_tasks[child]
        if stop == 'cancel':
            await asyncio.wait_for(controller.close(), 2)
            assert worker.cancelled()
        else:
            await asyncio.wait_for(asyncio.shield(worker), 2)
            assert not worker.cancelled()
        assert process.kill_calls == 1
        assert process.wait_calls == 2
        assert store.get(child, owner='alice').state == 'completed'
        assert store.auto_queue_status(child, owner='alice')['event'] == 'completed'
        assert len(store.auto_queue_events(owner='alice')) == 1
        assert provider.sends == [parent, child]
        assert 'private subprocess detail' not in caplog.text
        assert ('Subchat notification cleanup failed' in caplog.text) == (cleanup == 'stalled')
    finally:
        exited.set()
        await controller.close()
        ledger.close()


async def test_https_gateway_auto_queue_keeps_worker_after_call_and_checks_scope(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        provider = Provider()
        service = Subchats(store, provider)
        parent, child = 'a' * 32, 'b' * 32
        await service.send(parent, 'first', 'model', 'effort', owner='owner')
        service.queue(child, parent, 'second', owner='owner')
        gateway = SubchatGateway(lambda owner: session(service, owner=owner),
                                 owner='owner')
        try:
            request = Request(operation_id='c' * 32, tool='subchat_queue_auto',
                              arguments={'operation_id': child})
            denied = await gateway.execute('owner', request, frozenset())
            assert denied.state == 'failed'
            assert store.auto_queue_status(child, owner='owner') is None
            armed = await gateway.execute('owner', request,
                                          frozenset({'subchat_queue_auto'}))
            assert armed.state == 'completed' and armed.data['state'] == 'armed'
            assert gateway.has_live_work()
            provider.parent_ready = True
            await _until(lambda: store.get(child, owner='owner').state == 'submitted')
            provider.child_ready = True
            await _until(lambda: store.auto_queue_status(child, owner='owner')['event']
                         == 'completed')
            assert provider.sends == [parent, child]
            assert not gateway.has_live_work()
        finally:
            await gateway.close()
    finally:
        ledger.close()


async def test_real_http_mcp_queue_auto_scope_and_detached_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    resource = 'https://computer.example/mcp'
    engine = Engine(tmp_path / 'engine')
    authority = AuthorizationStore(
        tmp_path / 'auth', resource=resource,
        known_tools=frozenset(engine.tools) | SUBCHAT_GATEWAY_TOOLS)
    redirect = 'https://client.example/callback'
    authority.register_client('client', frozenset({redirect}))
    authority.enroll_device('owner', 'device', frozenset({
        'subchat_queue_auto', 'subchat_queue_events', 'computer_status'}))

    def grant(tools):
        verifier = 'v' * 43
        code = authority.approve(
            owner='owner', device='device', client='client', redirect=redirect,
            resource=resource, tools=frozenset(tools), challenge=pkce_s256(verifier))
        token = authority.exchange_code(
            code=code, verifier=verifier, client='client', redirect=redirect,
            resource=resource).value
        identity = authority.verify(token, resource=resource)
        assert identity is not None
        return token, identity

    token, identity = grant({'subchat_queue_auto', 'subchat_queue_events'})
    other_token, _ = grant({'computer_status'})
    owner = subchat_ledger_owner(identity, 'account')
    ledger = Ledger(tmp_path / 'ledger')
    try:
        store = SubchatSubmissions(ledger.connection)
        provider = Provider()
        service = Subchats(store, provider)
        parent, child = 'a' * 32, 'b' * 32
        await service.send(parent, 'first', 'model', 'effort', owner=owner)
        service.queue(child, parent, 'second', owner=owner)
        def active_grant(grant_id: str) -> bool:
            current = authority.current_grant(grant_id)
            return (current is not None and 'subchat_queue_auto' in current.tools
                    and subchat_ledger_owner(current, 'account') == owner)

        gateway = SubchatGateway(
            lambda selected: session(service, owner=selected,
                                     auto_queue_grant_active=active_grant),
            owner='owner', account_id='account')
        backend = AuthorizedDeviceMCP(
            authority, engine, owner='owner', device='device', client='client',
            subchat_gateway=gateway)
        adapter = HTTPMCP(backend.authenticate, backend.session)
        port = await adapter.start()
        try:
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                         trust_env=False) as http:
                headers = await initialize(http, token)
                catalog = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                assert {tool['name'] for tool in catalog.json()['result']['tools']} == {
                    'subchat_queue_auto', 'subchat_queue_events'}
                armed = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                    'params': {'name': 'subchat_queue_auto', 'arguments': {
                        'request_id': 'c' * 32, 'operation_id': child}}})
                assert armed.status_code == 200
                assert armed.json()['result']['structuredContent']['data']['state'] == 'armed'
                other_headers = await initialize(http, other_token)
                other_catalog = await http.post('/mcp', headers=other_headers, json={
                    'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
                assert 'subchat_queue_auto' not in {
                    tool['name'] for tool in other_catalog.json()['result']['tools']}
            assert gateway.has_live_work()
            provider.parent_ready = True
            await _until(lambda: store.get(child, owner=owner).state == 'submitted')
            provider.child_ready = True
            await _until(lambda: store.auto_queue_status(child, owner=owner)['event']
                         == 'completed')
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                         trust_env=False) as http:
                headers = await initialize(http, token)
                events = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                    'params': {'name': 'subchat_queue_events', 'arguments': {
                        'request_id': 'd' * 32, 'after_id': 0}}})
                assert events.json()['result']['structuredContent']['data']['events'][0][
                    'event'] == 'completed'
            assert provider.sends == [parent, child]
            provider.parent_ready = False
            provider.child_ready = False
            next_parent, next_child = 'e' * 32, 'f' * 32
            await service.send(next_parent, 'another parent', 'model', 'effort',
                               owner=owner)
            service.queue(next_child, next_parent, 'must remain queued', owner=owner)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                         trust_env=False) as http:
                headers = await initialize(http, token)
                second_arm = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                    'params': {'name': 'subchat_queue_auto', 'arguments': {
                        'request_id': '1' * 32, 'operation_id': next_child}}})
                assert second_arm.json()['result']['structuredContent']['data'][
                    'state'] == 'armed'
            authority.revoke(owner='owner', grant=identity.grant_id)
            await _until(lambda: store.auto_queue_status(next_child, owner=owner)['event']
                         == 'authorization_lost')
            assert store.get(next_child, owner=owner).state == 'queued'
            assert provider.sends == [parent, child, next_parent]
            recovery_token, _ = grant({'subchat_queue_events'})
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                         trust_env=False) as http:
                headers = await initialize(http, recovery_token)
                revoked_events = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
                    'params': {'name': 'subchat_queue_events', 'arguments': {
                        'request_id': '2' * 32, 'after_id': 0,
                        'operation_id': next_child}}})
                assert [event['event'] for event in revoked_events.json()[
                    'result']['structuredContent']['data']['events']] == [
                        'authorization_lost']
        finally:
            await adapter.close()
            await gateway.close()
    finally:
        ledger.close()


async def test_auto_queue_survives_controller_restart_and_notifies_once(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    service = Subchats(store, provider)
    parent, child = '1' * 32, '2' * 32
    await service.send(parent, 'first', 'model', 'effort', owner='alice')
    service.queue(child, parent, 'second', owner='alice')
    first = session(service, owner='alice')
    try:
        armed = await first.execute(Request(operation_id='3' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert armed.state == 'completed'
        assert armed.data['state'] == 'armed'
        await first.close()
        # A new controller resumes only the original child operation. It can
        # recover the parent and dispatch after its final answer is observed.
        provider.parent_ready = True
        second = session(service, owner='alice')
        try:
            await _until(lambda: store.get(child, owner='alice').state == 'submitted')
            assert provider.sends == [parent, child]
            provider.child_ready = True
            await _until(lambda: store.get(child, owner='alice').state == 'completed')
            await _until(lambda: store.auto_queue_status(child, owner='alice')['event']
                         == 'completed')
            event = await second.execute(Request(operation_id='4' * 32,
                tool='subchat_queue_events', arguments={}))
            assert event.data['events'][0]['operation_id'] == child
            assert event.data['events'][0]['event'] == 'completed'
            cursor_page = await second.execute(Request(operation_id='9' * 32,
                tool='subchat_queue_events', arguments={'after_id': 0}))
            assert cursor_page.data['events'][0]['operation_id'] == child
            assert cursor_page.data['next_cursor'] > 0
            assert cursor_page.data['has_more'] is False
            assert second.notifications.empty()
            # The arming controller is gone. A replacement can recover the
            # durable event but must not impersonate its notification target.
            # No duplicate dispatch in a third controller.
        finally:
            await second.close()
        third = session(service, owner='alice')
        try:
            assert provider.sends == [parent, child]
            assert not third.auto_queue_tasks
        finally:
            await third.close()
    finally:
        ledger.close()


async def test_auto_queue_notifies_arming_controller_when_another_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockFirst(Provider):
        def __init__(self):
            super().__init__()
            self.parent_reads = 0

        async def read_answer(self, submission):
            if submission.after_operation_id is None:
                self.parent_reads += 1
                if self.parent_reads == 1:
                    entered.set()
                    await release.wait()
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = BlockFirst()
    service = Subchats(store, provider)
    parent, child = 'c' * 32, 'd' * 32
    await service.send(parent, 'first', 'model', 'effort', owner='alice')
    service.queue(child, parent, 'second', owner='alice')
    arming = session(service, owner='alice')
    try:
        armed = await arming.execute(Request(operation_id='e' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert armed.state == 'completed'
        provider.parent_ready = True
        await asyncio.wait_for(entered.wait(), 1)
        other = session(service, owner='alice')
        try:
            await _until(lambda: store.get(child, owner='alice').state == 'submitted')
            provider.child_ready = True
            await _until(lambda: store.auto_queue_status(child, owner='alice')['event']
                         == 'completed')
            await _until(lambda: not arming.notifications.empty())
            notice = arming.notifications.get_nowait()
            event = store.auto_queue_events_page(owner='alice', after_id=0)[0][0]
            assert notice['params']['data'] == {
                'operation_id': child, 'event': 'completed',
                'event_id': event['id'], 'recover_tool': 'subchat_status'}
            assert other.notifications.empty()
            assert provider.sends == [parent, child]
        finally:
            release.set()
            await other.close()
    finally:
        release.set()
        await arming.close()
        ledger.close()


async def test_rearm_does_not_notify_previous_arming_session(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    service = Subchats(store, Provider())
    parent, child = 'a' * 32, 'b' * 32
    await service.send(parent, 'first', 'model', 'effort', owner='alice')
    service.queue(child, parent, 'second', owner='alice')
    first = session(service, owner='alice')
    second = session(service, owner='alice')
    try:
        initial = await first.execute(Request(operation_id='c' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        revised = await second.execute(Request(operation_id='d' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert revised.data['epoch'] == initial.data['epoch'] + 1
        event_id = store.finish_auto_queue(child, owner='alice',
                                           event='observation_failed',
                                           expected_epoch=revised.data['epoch'])
        assert type(event_id) is int
        await _until(lambda: not second.notifications.empty())
        assert second.notifications.get_nowait()['params']['data']['event_id'] == event_id
        await asyncio.sleep(.03)
        assert first.notifications.empty()
    finally:
        await first.close()
        await second.close()
        ledger.close()


def test_auto_queue_event_cursor_recovers_more_than_recent_limit(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        with ledger.connection:
            ledger.connection.executemany(
                'INSERT INTO subchat_auto_queue_events '
                '(operation_id, owner, event, updated_at) VALUES (?,?,?,?)',
                [(f'{index:032x}', 'alice', 'completed', float(index))
                 for index in range(125)]
                + [('f' * 32, 'bob', 'completed', 200.0)])
        cursor = 0
        recovered = []
        while True:
            page, more = store.auto_queue_events_page(owner='alice', after_id=cursor,
                                                      limit=40)
            recovered.extend(page)
            cursor = page[-1]['id'] if page else cursor
            if not more:
                break
        assert len(recovered) == 125
        assert [item['operation_id'] for item in recovered] == [
            f'{index:032x}' for index in range(125)]
        assert store.auto_queue_events_page(owner='bob', after_id=0)[0][0][
            'operation_id'] == 'f' * 32
    finally:
        ledger.close()


async def test_auto_queue_owner_scope_disable(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    service = Subchats(store, provider)
    parent, child = '5' * 32, '6' * 32
    await service.send(parent, 'first', 'model', 'effort', owner='alice')
    service.queue(child, parent, 'second', owner='alice')
    alice = session(service, owner='alice')
    bob = session(service, owner='bob')
    try:
        denied = await bob.execute(Request(operation_id='7' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert denied.state == 'failed'
        assert store.auto_queue_events(owner='bob') == []
        armed = await alice.execute(Request(operation_id='8' * 32,
            tool='subchat_queue_auto', arguments={
                'operation_id': child, 'lease_seconds': 30}))
        assert armed.state == 'completed'
        disabled = await alice.execute(Request(operation_id='9' * 32,
            tool='subchat_queue_auto', arguments={
                'operation_id': child, 'enabled': False}))
        assert disabled.state == 'completed'
        assert store.active_auto_queues(owner='alice') == ()
        assert provider.sends == [parent]
    finally:
        await alice.close()
        await bob.close()
        ledger.close()


@pytest.mark.asyncio
async def test_auto_queue_events_survive_rearm_and_restart(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        service = Subchats(store, Provider())
        parent, child = 'a' * 32, 'b' * 32
        await service.send(parent, 'first', 'model', 'effort', owner='alice')
        service.queue(child, parent, 'second', owner='alice')
        store.arm_auto_queue(child, owner='alice', lease_seconds=30)
        first_id = store.finish_auto_queue(child, owner='alice',
                                           event='observation_failed')
        assert type(first_id) is int and first_id > 0
        store.arm_auto_queue(child, owner='alice', lease_seconds=30)
        assert store.disable_auto_queue(child, owner='alice')
        page, more = store.auto_queue_events_page(owner='alice', after_id=0)
        assert len(page) == 2 and page[0]['id'] == first_id
        assert page[1]['id'] > first_id
        assert not more
        assert [event['event'] for event in store.auto_queue_events(owner='alice')] == [
            'disabled', 'observation_failed']
        assert store.auto_queue_events(owner='bob') == []
    finally:
        ledger.close()
    reopened = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(reopened.connection)
        assert [event['event'] for event in store.auto_queue_events(owner='alice')] == [
            'disabled', 'observation_failed']
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_old_queue_epoch_cannot_finish_rearmed_queue(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        service = Subchats(store, Provider())
        parent, child = 'd' * 32, 'e' * 32
        await service.send(parent, 'first', 'model', 'effort', owner='alice')
        service.queue(child, parent, 'second', owner='alice')
        store.arm_auto_queue(child, owner='alice', lease_seconds=30)
        old_epoch = store.auto_queue_status(child, owner='alice')['epoch']
        store.arm_auto_queue(child, owner='alice', lease_seconds=30)
        new_epoch = store.auto_queue_status(child, owner='alice')['epoch']
        assert new_epoch == old_epoch + 1
        assert not store.finish_auto_queue(child, owner='alice',
                                           event='observation_failed',
                                           expected_epoch=old_epoch)
        assert store.auto_queue_status(child, owner='alice')['state'] == 'armed'
        assert store.auto_queue_events(owner='alice') == []
        assert store.finish_auto_queue(child, owner='alice', event='lease_expired',
                                       expected_epoch=new_epoch)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_rearm_while_old_worker_observes_keeps_new_queue_live(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    entered = asyncio.Event()
    release = asyncio.Event()

    class FailingFirstObservation(Provider):
        def __init__(self):
            super().__init__()
            self.observations = 0

        async def read_answer(self, submission):
            if submission.after_operation_id is None:
                self.observations += 1
                if self.observations == 1:
                    entered.set()
                    await release.wait()
                    raise SubchatAccessError(401)
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = FailingFirstObservation()
    service = Subchats(store, provider)
    parent, child = '7' * 32, '8' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    controller = session(service)
    try:
        first = await controller.execute(Request(operation_id='9' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert first.state == 'completed'
        await asyncio.wait_for(entered.wait(), 1)
        old_task = controller.auto_queue_tasks[child]
        second = await controller.execute(Request(operation_id='a' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert second.state == 'completed'
        release.set()
        await _until(lambda: controller.auto_queue_tasks[child] is not old_task)
        assert store.auto_queue_status(child, owner=None)['state'] == 'armed'
        assert store.auto_queue_events(owner=None) == []
        assert provider.sends == [parent]
    finally:
        release.set()
        await controller.close()
        ledger.close()


@pytest.mark.asyncio
async def test_auto_queue_capacity_rejection_does_not_arm_unowned_child(
        tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockParent(Provider):
        async def read_answer(self, submission):
            if submission.after_operation_id is None:
                entered.set()
                await release.wait()
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = BlockParent()
    service = Subchats(store, provider)
    parent = 'a' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    children = [f'{number:032x}' for number in range(1, 10)]
    for child in children:
        service.queue(child, parent, f'follow-up {child}', owner=None)
    controller = session(service)
    try:
        for number, child in enumerate(children[:8]):
            armed = await controller.execute(Request(
                operation_id=f'{number + 10:032x}', tool='subchat_queue_auto',
                arguments={'operation_id': child}))
            assert armed.state == 'completed'
        await asyncio.wait_for(entered.wait(), 1)
        for number, child in enumerate(children[:8]):
            disabled = await controller.execute(Request(
                operation_id=f'{number + 20:032x}', tool='subchat_queue_auto',
                arguments={'operation_id': child, 'enabled': False}))
            assert disabled.state == 'completed'
        assert len([task for task in controller.auto_queue_tasks.values()
                    if not task.done()]) == 8
        rejected = await controller.execute(Request(
            operation_id='f' * 32, tool='subchat_queue_auto',
            arguments={'operation_id': children[8]}))
        assert rejected.state == 'failed'
        assert store.auto_queue_status(children[8], owner=None) is None
        assert children[8] not in controller.auto_queue_tasks
        assert children[8] not in controller.auto_queue_notifications
        release.set()
        await _until(lambda: all(task.done() for task in
                                  controller.auto_queue_tasks.values()))
        assert store.get(children[8], owner=None).state == 'queued'
        assert provider.sends == [parent]
        accepted = await controller.execute(Request(
            operation_id='e' * 32, tool='subchat_queue_auto',
            arguments={'operation_id': children[8]}))
        assert accepted.state == 'completed'
        assert store.auto_queue_status(children[8], owner=None)['state'] == 'armed'
    finally:
        release.set()
        await controller.close()
        ledger.close()


def test_auto_queue_events_migrate_legacy_row_once(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        SubchatSubmissions(ledger.connection)
        with ledger.connection:
            ledger.connection.execute('DROP TABLE subchat_auto_queue_events')
            ledger.connection.execute('DROP TABLE subchat_auto_queue')
            ledger.connection.execute(
                'CREATE TABLE subchat_auto_queue ('
                'operation_id TEXT PRIMARY KEY, owner TEXT, state TEXT NOT NULL, '
                'expires_at REAL NOT NULL, event TEXT, updated_at REAL NOT NULL)')
            ledger.connection.execute(
                'INSERT INTO subchat_auto_queue VALUES (?,?,?,?,?,?)',
                ('c' * 32, 'alice', 'stopped', 123.0, 'lease_expired', 124.0))
        read_only = SubchatSubmissions(ledger.connection, initialize=False)
        assert [item['event'] for item in read_only.auto_queue_events(owner='alice')] == [
            'lease_expired']
        migrated = SubchatSubmissions(ledger.connection)
        assert [item['event'] for item in migrated.auto_queue_events(owner='alice')] == [
            'lease_expired']
        again = SubchatSubmissions(ledger.connection)
        assert [item['event'] for item in again.auto_queue_events(owner='alice')] == [
            'lease_expired']
    finally:
        ledger.close()


async def test_auto_queue_never_reposts_uncertain_child_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    provider.parent_ready = True
    provider.lose_child_receipt = True
    service = Subchats(store, provider)
    parent, child = 'a' * 32, 'b' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    first = session(service)
    try:
        armed = await first.execute(Request(operation_id='c' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child}))
        assert armed.state == 'completed'
        await _until(lambda: store.get(child, owner=None).state == 'sending')
        assert provider.sends == [parent, child]
    finally:
        await first.close()
    second = session(service)
    try:
        await asyncio.sleep(.05)
        assert store.get(child, owner=None).state == 'sending'
        assert provider.sends == [parent, child]
        assert store.auto_queue_status(child, owner=None)['state'] == 'armed'
    finally:
        await second.close()
        ledger.close()


async def test_expired_auto_queue_records_event_without_dispatch(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    service = Subchats(store, provider)
    parent, child = 'd' * 32, 'e' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    with ledger.connection:
        ledger.connection.execute('UPDATE subchat_auto_queue SET expires_at=0 '
                                  'WHERE operation_id=?', (child,))
    server = session(service)
    try:
        await _until(lambda: store.auto_queue_status(child, owner=None)['event']
                     == 'lease_expired')
        assert provider.sends == [parent]
        assert store.get(child, owner=None).state == 'queued'
    finally:
        await server.close()
        ledger.close()


async def test_two_auto_controllers_use_one_durable_send(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    both_preparing = asyncio.Event()

    class RacingProvider(Provider):
        def __init__(self):
            super().__init__()
            self.preparing = 0
            self.lose_child_receipt = True

        async def prepare(self, submission):
            if submission.after_operation_id is not None:
                self.preparing += 1
                if self.preparing == 2:
                    both_preparing.set()
                await both_preparing.wait()
            return await super().prepare(submission)

    first, second = Ledger(tmp_path), Ledger(tmp_path)
    provider = RacingProvider()
    first_store = SubchatSubmissions(first.connection)
    second_store = SubchatSubmissions(second.connection)
    parent, child = '1' * 32, '2' * 32
    first_store.prepare(parent, 'first', 'model', 'effort', owner='alice',
                        conversation_id='chat')
    first_store.begin_send(parent, owner='alice')
    first_store.submitted(parent, 'chat', 'parent-user', owner='alice')
    first_store.complete(parent, 'parent-answer', 'done', owner='alice')
    first_service = Subchats(first_store, provider)
    first_service.queue(child, parent, 'second', owner='alice')
    first_store.arm_auto_queue(child, owner='alice', lease_seconds=30)
    controllers = [session(first_service, owner='alice'),
                   session(Subchats(second_store, provider), owner='alice')]
    try:
        await _until(lambda: provider.preparing == 2)
        await _until(lambda: first_store.get(child, owner='alice').state == 'sending')
        assert provider.sends == [child]
        assert first_store.auto_queue_status(child, owner='alice')['state'] == 'armed'
        assert first_store.auto_queue_events(owner='alice') == []
    finally:
        await asyncio.gather(*(controller.close() for controller in controllers))
        first.close()
        second.close()


async def test_auto_queue_cancelled_child_never_dispatches(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    service = Subchats(store, provider)
    parent, child = '3' * 32, '4' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    controller = session(service)
    try:
        cancelled = await controller.execute(Request(operation_id='5' * 32,
            tool='subchat_cancel', arguments={'operation_id': child}))
        assert cancelled.state == 'completed'
        provider.parent_ready = True
        await _until(lambda: store.auto_queue_status(child, owner=None)['event']
                     == 'cancelled')
        assert store.get(child, owner=None).state == 'cancelled'
        assert provider.sends == [parent]
    finally:
        await controller.close()
        ledger.close()


async def test_auto_queue_parent_interruption_stops_child(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = Provider()
    service = Subchats(store, provider)
    parent, child = '6' * 32, '7' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    store.interrupt(parent, owner=None)
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    controller = session(service)
    try:
        await _until(lambda: store.auto_queue_status(child, owner=None)['event']
                     == 'predecessor_interrupted')
        assert store.get(child, owner=None).state == 'queued'
        assert provider.sends == [parent]
    finally:
        await controller.close()
        ledger.close()


async def test_auto_queue_parent_access_failure_stops_child(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)

    class RejectedParent(Provider):
        async def read_answer(self, submission):
            if submission.after_operation_id is None:
                raise SubchatAccessError(403)
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = RejectedParent()
    service = Subchats(store, provider)
    parent, child = '8' * 32, '9' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    controller = session(service)
    try:
        await _until(lambda: store.auto_queue_status(child, owner=None)['event']
                     == 'authorization_lost')
        assert store.get(child, owner=None).state == 'queued'
        assert provider.sends == [parent]
    finally:
        await controller.close()
        ledger.close()


async def test_checkpointed_sending_parent_can_queue_without_manual_recover(
        tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)

    class DelayedReceipt(Provider):
        def __init__(self):
            super().__init__()
            self.receipt_ready = False

        async def prepare(self, submission):
            if submission.after_operation_id is None:
                return SubchatPreparedSend((), 'parent-user', 'account')
            return await super().prepare(submission)

        async def send(self, submission):
            if submission.after_operation_id is None:
                self.sends.append(submission.operation_id)
                return None
            return await super().send(submission)

        async def find_submission(self, submission):
            if submission.after_operation_id is None and self.receipt_ready:
                return SubchatReceipt(conversation_id='chat',
                                      user_message_id='parent-user',
                                      prompt=submission.prompt)
            return None

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = DelayedReceipt()
    service = Subchats(store, provider)
    parent, child = 'a' * 32, 'c' * 32
    first = await service.send(parent, 'first', 'model', 'effort', owner=None,
                               conversation_id='chat')
    assert first.state == 'sending'
    queued = service.queue(child, parent, 'second', owner=None)
    assert queued.state == 'queued'
    assert queued.expected_last_user_message_id == 'parent-user'
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    controller = session(service)
    try:
        await asyncio.sleep(.05)
        assert store.get(child, owner=None).state == 'queued'
        assert provider.sends == [parent]
        provider.receipt_ready = provider.parent_ready = True
        await _until(lambda: store.get(child, owner=None).state == 'submitted')
        assert store.get(parent, owner=None).state == 'completed'
        assert provider.sends == [parent, child]
    finally:
        await controller.close()
        ledger.close()


@pytest.mark.parametrize('missing', ['user', 'account', 'conversation'])
def test_sending_parent_without_full_checkpoint_cannot_queue(tmp_path, missing):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = 'd' * 32, 'e' * 32
    store.prepare(parent, 'first', 'model', 'effort', owner=None,
                  conversation_id=None if missing == 'conversation' else 'chat')
    if missing == 'user':
        store.begin_send(parent, owner=None)
    elif missing == 'account':
        store.begin_send(parent, owner=None)
        store.observe_request(parent, 'parent-user', owner=None)
    else:
        store.begin_send(parent, owner=None, user_message_id='parent-user',
                         provider_account_id='account')
    try:
        with pytest.raises(ValueError, match='checkpointed'):
            Subchats(store, Provider()).queue(child, parent, 'second', owner=None)
        assert store.active_auto_queues(owner=None) == ()
    finally:
        ledger.close()


async def test_disable_while_parent_observation_blocks_prevents_child_send(
        tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowParent(Provider):
        async def read_answer(self, submission):
            if submission.after_operation_id is None:
                entered.set()
                await release.wait()
                self.parent_ready = True
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = SlowParent()
    service = Subchats(store, provider)
    parent, child = 'f' * 32, '0' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    controller = session(service)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        disabled = await controller.execute(Request(operation_id='1' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child,
                                                  'enabled': False}))
        assert disabled.state == 'completed'
        assert disabled.data['state'] == 'disabled'
        release.set()
        await _until(lambda: store.get(parent, owner=None).state == 'completed')
        await _until(lambda: controller.auto_queue_tasks[child].done())
        assert store.get(child, owner=None).state == 'queued'
        assert provider.sends == [parent]
        assert store.auto_queue_status(child, owner=None)['event'] == 'disabled'
    finally:
        release.set()
        await controller.close()
        ledger.close()


async def test_disable_after_child_send_reservation_does_not_undo_dispatch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_mcp, 'QUEUE_WATCH_INTERVAL', .01)
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowChild(Provider):
        async def send(self, submission):
            if submission.after_operation_id is not None:
                entered.set()
                await release.wait()
            return await super().send(submission)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = SlowChild()
    provider.parent_ready = True
    service = Subchats(store, provider)
    parent, child = '2' * 32, '3' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'second', owner=None)
    store.arm_auto_queue(child, owner=None, lease_seconds=30)
    controller = session(service)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert store.get(child, owner=None).state == 'sending'
        disabled = await controller.execute(Request(operation_id='4' * 32,
            tool='subchat_queue_auto', arguments={'operation_id': child,
                                                  'enabled': False}))
        assert disabled.state == 'completed'
        release.set()
        await _until(lambda: store.get(child, owner=None).state == 'submitted')
        assert provider.sends == [parent, child]
        assert store.auto_queue_status(child, owner=None)['event'] == 'disabled'
    finally:
        release.set()
        await controller.close()
        ledger.close()
