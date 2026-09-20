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
