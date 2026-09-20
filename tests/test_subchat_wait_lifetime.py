"""A short observation budget must not abort a queued send's preparation."""
import asyncio

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


async def test_session_close_joins_pending_preparation_without_send(tmp_path):
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
        await server.close()
        assert cancelled.is_set()
        assert not server.recoveries
        assert provider.sends == []
        result = await server.execute(Request(operation_id='d' * 32, tool='subchat_recover',
            arguments={'operation_id': child}))
        assert result.state == 'failed'
        assert provider.prepares == [child]
    finally:
        await server.close()
        ledger.close()
