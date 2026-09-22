"""Explicit background queue delivery without caller polling or implicit browser startup."""
import asyncio
import time

import pytest
from test_subchat_delivery import Provider

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAccessError, SubchatAnswer, SubchatReceipt, Subchats
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


@pytest.fixture
async def watched(tmp_path, monkeypatch):
    monkeypatch.setattr('anywhere_computer.subchat_mcp.QUEUE_WATCH_INTERVAL', .01)

    class ReadyProvider(Provider):
        ready = True
        reads = 0
        failure = False

        def queue_watch_ready(self, submission):
            return self.ready

        async def read_answer(self, submission):
            self.reads += 1
            if self.failure:
                raise SubchatAccessError(401)
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    provider = ReadyProvider()
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    parent, child = '1' * 32, '2' * 32
    await service.send(parent, 'first', 'model', 'effort', owner=None)
    service.queue(child, parent, 'next', owner=None)
    provider.lose_receipt = True
    server = session(service)

    async def watch(enabled=True, operation_id=child):
        return await server.execute(Request(operation_id='a' * 32, tool='subchat_queue_watch',
            arguments={'operation_id': operation_id, 'enabled': enabled}))

    try:
        yield service, provider, server, parent, child, watch
    finally:
        await server.close()
        ledger.close()


async def test_watch_dispatches_after_parent_without_client_recovery_and_never_replays(watched):
    service, provider, server, parent, child, watch = watched
    assert (await watch()).data['state'] == 'watching'
    task = server.queue_watches[child]
    await asyncio.sleep(.03)
    assert provider.reads > 0 and provider.sends == [parent]
    assert (await watch()).data['state'] == 'watching'
    assert server.queue_watches[child] is task
    provider.finished = True
    async with asyncio.timeout(2):
        while service.store.get(child, owner=None).state != 'sending':
            await asyncio.sleep(.01)
    assert service.store.get(child, owner=None).state == 'sending'
    assert provider.sends == [parent, child]
    assert not task.done()
    assert (await watch()).data['state'] == 'watching'
    assert (await watch(False)).data['state'] == 'disabled'
    assert (await watch()).state == 'failed'  # uncertain send cannot be armed again
    assert provider.sends == [parent, child]


@pytest.mark.parametrize('stop', ['access', 'browser', 'cancel', 'close'])
async def test_watch_stops_on_loss_cancel_or_controller_close(watched, stop):
    service, provider, server, parent, child, watch = watched
    await watch()
    task = server.queue_watches[child]
    if stop == 'access':
        provider.failure = True
    elif stop == 'browser':
        provider.ready = False
    elif stop == 'cancel':
        await server.execute(Request(operation_id='b' * 32, tool='subchat_cancel',
                                     arguments={'operation_id': child}))
    else:
        await server.close()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    reads = provider.reads
    await asyncio.sleep(.03)
    assert provider.reads == reads and provider.sends == [parent]
    assert service.store.get(child, owner=None).state == (
        'cancelled' if stop == 'cancel' else 'queued')
    if stop != 'close':
        reply = await server.execute(Request(operation_id='c' * 32, tool='subchat_status',
                                            arguments={'operation_id': child}))
        assert reply.data['queue_watch']['state'] == 'stopped'
        if stop == 'access':
            assert reply.data['queue_watch']['reason'] == 'authorization_lost'
        assert (await watch()).data['state'] == 'stopped'  # no implicit retry


async def test_watch_requires_live_backend_and_is_not_restored_on_restart(watched):
    service, provider, server, parent, child, watch = watched
    provider.ready = False
    assert (await watch()).state == 'failed'
    assert not server.queue_watches and not provider.reads
    provider.ready = True
    await watch()
    await server.close()
    restarted = session(service)
    try:
        await asyncio.sleep(.03)
        assert not restarted.queue_watches and provider.sends == [parent]
        assert service.store.get(child, owner=None).state == 'queued'
    finally:
        await restarted.close()


async def test_watch_slots_are_bounded_and_released_explicitly(watched):
    service, provider, server, parent, child, watch = watched
    for index in range(8):
        identity = f'{index + 10:032x}'
        service.queue(identity, parent, f'next {index}', owner=None)
        assert (await watch(operation_id=identity)).state == 'completed'
    assert (await watch()).state == 'failed'
    await watch(False, operation_id=f'{10:032x}')
    assert (await watch()).state == 'completed'
    assert len(server.queue_watches) == 8 and provider.sends == [parent]


async def test_watch_lease_expires_without_dispatch_or_implicit_restart(watched):
    service, provider, server, parent, child, watch = watched
    armed = await server.execute(Request(operation_id='c' * 32, tool='subchat_queue_watch',
        arguments={'operation_id': child, 'lease_seconds': 30}))
    assert armed.data['state'] == 'watching'
    server.queue_watch_deadlines[child] = time.monotonic() - 1
    await asyncio.wait_for(server.queue_watches[child], 2)
    assert server.queue_watch_states[child] == {'state': 'stopped', 'reason': 'lease_expired'}
    assert provider.sends == [parent]
    assert service.store.get(child, owner=None).state == 'queued'
    assert (await watch()).data['state'] == 'watching'


async def test_long_read_in_one_conversation_does_not_block_another(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    first, second = '3' * 32, '4' * 32

    class SeparateReads(Provider):
        async def send(self, submission):
            self.sends.append(submission.operation_id)
            return SubchatReceipt(conversation_id='chat-' + submission.operation_id,
                                  user_message_id='user-' + submission.operation_id,
                                  prompt=submission.prompt)

        async def read_answer(self, submission):
            if submission.operation_id == first:
                entered.set()
                await release.wait()
                return None
            return SubchatAnswer(conversation_id=submission.conversation_id,
                                 user_message_id=submission.user_message_id,
                                 prompt=submission.prompt, answer_message_id='answer-second',
                                 text='done')

    ledger = Ledger(tmp_path)
    provider = SeparateReads()
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    server = session(service)
    try:
        await service.send(first, 'first', 'model', 'effort', owner=None)
        await service.send(second, 'second', 'model', 'effort', owner=None)
        pending = asyncio.create_task(server.execute(Request(operation_id='5' * 32,
            tool='subchat_wait', arguments={'operation_id': first, 'wait_ms': 100})))
        await asyncio.wait_for(entered.wait(), 1)
        other = await asyncio.wait_for(server.execute(Request(operation_id='6' * 32,
            tool='subchat_recover', arguments={'operation_id': second})), 1)
        assert other.data['state'] == 'completed'
        assert (await pending).data['state'] == 'submitted'
    finally:
        release.set()
        await server.close()
        ledger.close()


async def test_real_browser_backend_readiness_does_not_create_context():
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmission

    async def forbidden():
        pytest.fail('watch readiness must not launch Chrome')

    backend = BrowserSubchatBackend(forbidden)
    submission = SubchatSubmission(operation_id='f' * 32, prompt='next', model='m', effort='e',
                                   requested_conversation_id='chat', state='queued')
    assert backend.queue_watch_ready(submission) is False


async def test_queue_watch_through_real_stdio_sdk(tmp_path):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    program = '''
import asyncio, sys
from pathlib import Path
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats, SubchatReceipt, SubchatAnswer
from anywhere_computer.subchat_state import SubchatSubmissions
from anywhere_computer.subchat_mcp import session
from anywhere_computer.mcp_server import serve_stdio
class Provider:
    def queue_watch_ready(self, submission): return True
    async def prepare(self, submission): return ()
    async def send(self, submission):
        with (Path(sys.argv[1]) / 'sends').open('a') as output:
            output.write(submission.operation_id + '\\n')
        return SubchatReceipt(conversation_id='fixture-chat',
            user_message_id=submission.operation_id, prompt=submission.prompt)
    async def find_submission(self, submission): return None
    async def read_answer(self, submission):
        return SubchatAnswer(conversation_id='fixture-chat',
            user_message_id=submission.operation_id, prompt=submission.prompt,
            answer_message_id='answer-' + submission.operation_id, text='42')
async def main():
    ledger = Ledger(Path(sys.argv[1]) / 'state')
    server = session(Subchats(SubchatSubmissions(ledger.connection), Provider()))
    try: await serve_stdio(server, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await server.close()
        ledger.close()
asyncio.run(main())
'''
    parent, child = '7' * 32, '8' * 32
    params = StdioServerParameters(command=sys.executable, args=['-c', program, str(tmp_path)])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            assert not (await client.call_tool('subchat_send', {
                'request_id': parent, 'prompt': 'first', 'model': 'fixture',
                'effort': 'fixture'})).isError
            assert not (await client.call_tool('subchat_message', {
                'request_id': child, 'mode': 'queue', 'target_operation_id': parent,
                'prompt': 'next'})).isError
            assert not (await client.call_tool('subchat_queue_watch', {
                'operation_id': child})).isError
            async with asyncio.timeout(3):
                while True:
                    result = await client.call_tool('subchat_status', {'operation_id': child})
                    if result.structuredContent['data']['state'] == 'submitted':
                        break
                    await asyncio.sleep(.01)
            assert (tmp_path / 'sends').read_text().splitlines() == [parent, child]


async def test_disarmed_watch_releases_finished_pending_recovery(watched):
    service, provider, server, parent, child, watch = watched
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.read_answer

    async def delayed(submission):
        entered.set()
        await release.wait()
        return await original(submission)

    provider.read_answer = delayed
    await watch()
    await asyncio.wait_for(entered.wait(), 1)
    recovery = server.recoveries[child]
    try:
        assert (await watch(False)).data['state'] == 'disabled'
        assert not recovery.done()  # Disabling must not cancel in-flight preparation.
        release.set()
        await asyncio.wait_for(asyncio.shield(recovery), 1)
        await asyncio.sleep(0)
        assert service.store.get(child, owner=None).state == 'queued'
        assert provider.sends == [parent]
        assert child not in server.recoveries
    finally:
        release.set()
