"""Queue is durable and target-bound; unsupported steer never becomes a send."""
import pytest

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAnswer, SubchatReceipt, Subchats
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


class Provider:
    def __init__(self):
        self.sends = []
        self.prepares = []
        self.finished = False
        self.lose_receipt = False

    async def prepare(self, submission):
        self.prepares.append(submission.operation_id)
        return ('parent-user',) if submission.after_operation_id else ()

    async def send(self, submission):
        self.sends.append(submission.operation_id)
        if self.lose_receipt:
            return None
        return SubchatReceipt(conversation_id='chat', user_message_id='parent-user',
                              prompt=submission.prompt)

    async def find_submission(self, submission):
        return None

    async def read_answer(self, submission):
        if not self.finished:
            return None
        return SubchatAnswer(conversation_id='chat', user_message_id='parent-user',
                             prompt=submission.prompt, answer_message_id='answer', text='42')


async def test_queue_restart_pending_completion_and_lost_receipt(tmp_path):
    parent, message = '1' * 32, '2' * 32
    provider = Provider()
    ledger = Ledger(tmp_path)
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    await service.send(parent, 'first', 'observed model', 'observed effort', owner='peer')
    queued = service.queue(message, parent, 'followup', owner='peer')
    assert queued.state == 'queued'
    assert queued.expected_last_user_message_id == 'parent-user'
    assert queued.after_operation_id == parent
    assert queued.conversation_id == 'chat'
    assert provider.sends == [parent]
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        service = Subchats(SubchatSubmissions(ledger.connection), provider)
        assert await service.recover(message, owner='peer') == queued
        assert provider.prepares == [parent]
        with pytest.raises(ValueError, match='Unknown'):
            await service.recover(message, owner='another-owner')
        with pytest.raises(ValueError, match='different arguments'):
            service.queue(message, parent, 'different', owner='peer')
        provider.finished = True
        provider.lose_receipt = True
        assert (await service.recover(message, owner='peer')).state == 'sending'
        assert service.queue(message, parent, 'followup', owner='peer').state == 'sending'
        assert (await service.recover(message, owner='peer')).state == 'sending'
        assert provider.sends == [parent, message]
        with pytest.raises(ValueError, match='without resending'):
            service.store.begin_send(message, owner='peer')
    finally:
        ledger.close()


async def test_mcp_steer_has_no_queue_fallback_and_queue_cannot_change_target(tmp_path):
    ledger = Ledger(tmp_path)
    provider = Provider()
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    parent, message = '3' * 32, '4' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    server = session(service)
    await server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'fixture', 'version': '1'}}})
    await server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    try:
        result = await server.execute(Request(operation_id=message, tool='subchat_message',
            arguments={'mode': 'steer', 'target_operation_id': parent, 'prompt': 'correction'}))
        assert result.state == 'failed'
        assert result.data == {'error_code': 'unsupported', 'mode': 'steer',
                               'dispatched': False, 'queued': False}
        with pytest.raises(ValueError, match='Unknown'):
            service.store.get(message, owner=None)
        assert provider.sends == [parent]
        assert provider.prepares == [parent]
        result = await server.execute(Request(operation_id=message, tool='subchat_message',
            arguments={'mode': 'queue', 'target_operation_id': parent, 'prompt': 'next'}))
        assert result.data['state'] == 'queued'
        other = '5' * 32
        await service.send(other, 'another', 'model', 'effort', owner=None)
        with pytest.raises(ValueError, match='different arguments'):
            service.queue(message, other, 'next', owner=None)
    finally:
        ledger.close()


@pytest.mark.parametrize("dispatcher_count", [2, 32])
async def test_two_dispatchers_do_not_send_same_queued_message_twice(tmp_path, dispatcher_count):
    import asyncio

    ready = asyncio.Event()

    class ConcurrentProvider(Provider):
        async def prepare(self, submission):
            self.prepares.append(submission.operation_id)
            if len(self.prepares) == dispatcher_count:
                ready.set()
            await ready.wait()
            return ('parent-user',)

    first = Ledger(tmp_path)
    others = [Ledger(tmp_path) for _ in range(dispatcher_count - 1)]
    provider = ConcurrentProvider()
    provider.lose_receipt = True
    store = SubchatSubmissions(first.connection)
    parent, message = '6' * 32, '7' * 32
    store.prepare(parent, 'first', 'model', 'effort', owner='peer', conversation_id='chat')
    store.begin_send(parent, owner='peer')
    store.submitted(parent, 'chat', 'parent-user', owner='peer')
    store.complete(parent, 'answer', '42', owner='peer')
    service = Subchats(store, provider)
    service.queue(message, parent, 'next', owner='peer')
    services = [service, *[Subchats(SubchatSubmissions(item.connection), provider)
                           for item in others]]
    try:
        results = await asyncio.wait_for(asyncio.gather(
            *(item.recover(message, owner='peer') for item in services),
            return_exceptions=True), timeout=5)
        assert sum(isinstance(result, ValueError) for result in results) == dispatcher_count - 1
        assert store.get(message, owner='peer').state == 'sending'
        assert provider.sends == [message]
    finally:
        first.close()
        for item in others:
            item.close()


async def test_cancel_during_preparation_prevents_dispatch_and_survives_restart(tmp_path):
    import asyncio

    preparing, release = asyncio.Event(), asyncio.Event()

    class PreparingProvider(Provider):
        async def prepare(self, submission):
            preparing.set()
            await release.wait()
            return ()

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = PreparingProvider()
    service = Subchats(store, provider)
    parent, message = '8' * 32, '9' * 32
    store.prepare(parent, 'first', 'model', 'effort', owner='peer', conversation_id='chat')
    store.begin_send(parent, owner='peer')
    store.submitted(parent, 'chat', 'parent-user', owner='peer')
    store.complete(parent, 'answer', '42', owner='peer')
    service.queue(message, parent, 'next', owner='peer')
    task = asyncio.create_task(service.recover(message, owner='peer'))
    try:
        await asyncio.wait_for(preparing.wait(), timeout=5)
        with pytest.raises(ValueError, match='Unknown'):
            store.cancel(message, owner='other')
        assert store.cancel(message, owner='peer').state == 'cancelled'
        release.set()
        with pytest.raises(ValueError, match='without resending'):
            await task
        assert provider.sends == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        service = Subchats(store, provider)
        assert store.cancel(message, owner='peer').state == 'cancelled'
        assert (await service.recover(message, owner='peer')).state == 'cancelled'
        assert service.queue(message, parent, 'next', owner='peer').state == 'cancelled'
        assert provider.sends == []
        with pytest.raises(ValueError, match='dispatched'):
            store.cancel(parent, owner='peer')
    finally:
        ledger.close()


async def test_cancelled_input_is_terminal_for_mcp_wait_and_cli(tmp_path):
    from anywhere_computer.subchat_cli import Command, dispatch

    ledger = Ledger(tmp_path)
    provider = Provider()
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    operation = 'a' * 32
    service.store.prepare(operation, 'pending', 'model', 'effort', owner=None)
    server = session(service)
    try:
        cancelled = await server.execute(Request(operation_id='b' * 32, tool='subchat_cancel',
                                                 arguments={'operation_id': operation}))
        assert cancelled.data['state'] == 'cancelled'
        waited = await server.execute(Request(operation_id='c' * 32, tool='subchat_wait',
                                               arguments={'operation_id': operation}))
        assert waited.data['state'] == 'cancelled'
        result = await dispatch(service, Command(action='cancel', operation_id=operation))
        assert '"state":"cancelled"' in result
        assert provider.prepares == []
        assert provider.sends == []
        with pytest.raises(ValueError, match='only an operation identity'):
            await dispatch(service, Command(action='cancel', operation_id=operation, prompt='edit'))
    finally:
        ledger.close()


async def test_cancel_while_parent_is_observed_does_not_prepare_child(tmp_path):
    import asyncio

    observing, release = asyncio.Event(), asyncio.Event()

    class SlowObservation(Provider):
        async def read_answer(self, submission):
            observing.set()
            await release.wait()
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    provider = SlowObservation()
    provider.finished = True
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    parent, message = 'd' * 32, 'e' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(message, parent, 'next', owner=None)
    task = asyncio.create_task(service.recover(message, owner=None))
    try:
        await asyncio.wait_for(observing.wait(), timeout=5)
        service.store.cancel(message, owner=None)
        release.set()
        assert (await task).state == 'cancelled'
        assert provider.prepares == [parent]
        assert provider.sends == [parent]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ledger.close()
