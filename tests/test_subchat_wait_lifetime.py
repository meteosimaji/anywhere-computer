"""A short observation budget must not abort a queued send's preparation."""
import asyncio

import pytest
from test_subchat_delivery import Provider

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_short_wait_does_not_cancel_or_repeat_queue_preparation(tmp_path):
    release = asyncio.Event()

    class SlowPreparation(Provider):
        cancelled = False

        async def prepare(self, submission):
            self.prepares.append(submission.operation_id)
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return ('parent-user',)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = SlowPreparation()
    provider.lose_receipt = True
    parent, child = '1' * 32, '2' * 32
    store.prepare(parent, 'first', 'model', 'effort', owner=None, conversation_id='chat')
    store.begin_send(parent, owner=None)
    store.submitted(parent, 'chat', 'parent-user', owner=None)
    store.complete(parent, 'answer', '42', owner=None)
    service = Subchats(store, provider)
    service.queue(child, parent, 'next', owner=None)
    server = session(service)
    try:
        for _ in range(2):
            result = await server.execute(Request(operation_id='3' * 32, tool='subchat_wait',
                arguments={'operation_id': child, 'wait_ms': 30}))
            assert result.data['state'] == 'queued'
            assert not provider.cancelled
            assert provider.prepares == [child]
        release.set()
        result = await server.execute(Request(operation_id='4' * 32, tool='subchat_recover',
                                               arguments={'operation_id': child}))
        assert result.data['state'] == 'sending'
        assert provider.sends == [child]
    finally:
        release.set()
        await server.close()
        ledger.close()


@pytest.mark.parametrize('finish', ['close', 'cancel'])
async def test_session_joins_pending_preparation_without_send(tmp_path, finish):
    cancelled = asyncio.Event()

    class PendingPreparation(Provider):
        async def prepare(self, submission):
            self.prepares.append(submission.operation_id)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    provider = PendingPreparation()
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'first', 'model', 'effort', owner=None, conversation_id='chat')
    store.begin_send(parent, owner=None)
    store.submitted(parent, 'chat', 'parent-user', owner=None)
    store.complete(parent, 'answer', '42', owner=None)
    service = Subchats(store, provider)
    service.queue(child, parent, 'next', owner=None)
    server = session(service)
    try:
        result = await server.execute(Request(operation_id='c' * 32, tool='subchat_wait',
            arguments={'operation_id': child, 'wait_ms': 30}))
        assert result.data['state'] == 'queued'
        assert provider.prepares == [child]
        assert not cancelled.is_set()
        if finish == 'close':
            await server.close()
        else:
            result = await server.execute(Request(operation_id='f' * 32, tool='subchat_cancel',
                                                  arguments={'operation_id': child}))
            assert result.data['state'] == 'cancelled'
        assert cancelled.is_set()
        assert not server.recoveries
        assert provider.sends == []
        result = await server.execute(Request(operation_id='d' * 32, tool='subchat_recover',
            arguments={'operation_id': child}))
        assert result.state == ('failed' if finish == 'close' else 'completed')
        if finish == 'cancel':
            assert result.data['state'] == 'cancelled'
        assert provider.prepares == [child]
    finally:
        await server.close()
        ledger.close()


@pytest.mark.parametrize('finish', ['close', 'cancel'])
async def test_session_joins_direct_send(tmp_path, finish):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class PendingPreparation(Provider):
        async def prepare(self, submission):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    ledger = Ledger(tmp_path)
    provider = PendingPreparation()
    server = session(Subchats(SubchatSubmissions(ledger.connection), provider))
    request = Request(operation_id='e' * 32, tool='subchat_send',
                      arguments={'prompt': 'next', 'model': 'model', 'effort': 'effort'})
    sending = asyncio.create_task(server.execute(request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if finish == 'close':
            await server.close()
        else:
            result = await server.execute(Request(operation_id='f' * 32, tool='subchat_cancel',
                                                  arguments={'operation_id': request.operation_id}))
            assert result.data['state'] == 'cancelled'
        await asyncio.wait_for(asyncio.gather(sending, return_exceptions=True), 2)
        assert cancelled.is_set()
        assert sending.cancelled() == (finish == 'close')
        if finish == 'cancel':
            assert sending.result().data['state'] == 'cancelled'
        assert not provider.sends
        assert not server.calls
        result = await server.execute(request)
        assert result.state == ('failed' if finish == 'close' else 'completed')
        if finish == 'cancel':
            assert result.data['state'] == 'cancelled'
        assert not provider.sends
    finally:
        await server.close()
        ledger.close()


@pytest.mark.parametrize('outcome', ['answer', 'access_error', 'close'])
@pytest.mark.parametrize('serialize_recovery', [True, False])
async def test_short_wait_preserves_slow_observation_and_its_result(
        tmp_path, outcome, serialize_recovery):
    from anywhere_computer.subchat import SubchatAccessError

    release = asyncio.Event()

    class SlowReader(Provider):
        reads = 0
        cancellations = 0

        async def read_answer(self, submission):
            self.reads += 1
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                raise
            if outcome == 'access_error':
                raise SubchatAccessError(401)
            self.finished = True
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    provider = SlowReader()
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    server = session(service, serialize_recovery=serialize_recovery)
    operation = '6' * 32
    try:
        await service.send(operation, 'prompt', 'model', 'effort', owner=None)
        for _ in range(3):
            reply = await server.execute(Request(operation_id='7' * 32, tool='subchat_wait',
                arguments={'operation_id': operation, 'wait_ms': 20}))
            assert reply.data['state'] == 'submitted'
            assert provider.reads == 1
            assert provider.cancellations == 0
        if outcome == 'close':
            await server.close()
            assert provider.cancellations == 1
            assert not server.recoveries
        else:
            task = server.recoveries[operation]
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            reply = await server.execute(Request(operation_id='8' * 32, tool='subchat_recover',
                arguments={'operation_id': operation}))
            if outcome == 'answer':
                assert reply.data['answer'] == '42'
                assert reply.data['state'] == 'completed'
            else:
                assert reply.data['error_code'] == 'authentication_required'
                assert service.store.get(operation, owner=None).state == 'submitted'
            assert provider.reads == 1
            assert not server.recoveries
        assert provider.sends == [operation]
    finally:
        await server.close()
        ledger.close()


@pytest.mark.parametrize('state', ['sending', 'submitted'])
@pytest.mark.parametrize('serialize_recovery', [True, False])
async def test_finished_pending_observations_release_recovery_capacity(
        tmp_path, state, serialize_recovery):
    class PendingReader(Provider):
        gate = asyncio.Event()
        entered = asyncio.Event()
        reads = 0

        async def pending(self):
            self.reads += 1
            self.entered.set()
            await self.gate.wait()
            return None

        async def find_submission(self, submission):
            return await self.pending()

        async def read_answer(self, submission):
            return await self.pending()

    ledger = Ledger(tmp_path)
    provider = PendingReader()
    store = SubchatSubmissions(ledger.connection)
    server = session(Subchats(store, provider), serialize_recovery=serialize_recovery)
    try:
        for index in range(9):
            operation = f'{index + 1:032x}'
            store.prepare(operation, 'prompt', 'model', 'effort', owner=None)
            store.begin_send(operation, owner=None)
            if state == 'submitted':
                store.submitted(operation, 'chat', f'user-{index}', owner=None)
            provider.gate = asyncio.Event()
            provider.entered = asyncio.Event()
            waiting = asyncio.create_task(server.execute(Request(
                operation_id=f'{index + 100:032x}', tool='subchat_wait',
                arguments={'operation_id': operation, 'wait_ms': 20})))
            await asyncio.wait_for(provider.entered.wait(), 2)
            assert (await waiting).data['state'] == state
            recovery = server.recoveries[operation]
            provider.gate.set()
            await asyncio.wait_for(asyncio.shield(recovery), 2)
            await asyncio.sleep(0)
            assert operation not in server.recoveries
        assert provider.reads == 9
        assert provider.sends == []
    finally:
        provider.gate.set()
        await server.close()
        ledger.close()
