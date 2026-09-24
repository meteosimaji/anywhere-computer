from test_subchat_lifecycle import BrowserFixture

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_mcp_submission_identity_pending_recovery_and_retry(tmp_path):
    ledger = Ledger(tmp_path)
    backend = BrowserFixture()
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend))
    serial = 0

    async def call(method, params):
        nonlocal serial
        serial += 1
        return await server.handle({'jsonrpc': '2.0', 'id': serial,
                                    'method': method, 'params': params})

    try:
        initialized = await call('initialize', {'protocolVersion': '2025-11-25',
                                               'capabilities': {},
                                               'clientInfo': {'name': 'test', 'version': '1'}})
        assert 'subchat_recover' in initialized['result']['instructions']
        assert 'generation_transport=browser_prepared_httpx' in (
            initialized['result']['instructions'])
        await server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        catalog = await call('tools/list', {})
        names = {tool['name'] for tool in catalog['result']['tools']}
        assert names == {'subchat_send', 'subchat_recover', 'subchat_status', 'subchat_wait',
                        'subchat_message', 'subchat_cancel', 'subchat_delete',
                        'subchat_list', 'subchat_queue_watch'}
        listed = await call('tools/call', {'name': 'subchat_list', 'arguments': {}})
        assert listed['result']['structuredContent']['data']['submissions'] == []
        op = '1' * 32
        arguments = {'request_id': op, 'prompt': '日本語\nprint(42)',
                     'model': 'observed model', 'effort': 'observed effort'}
        unknown = await call('tools/call', {'name': 'subchat_send', 'arguments': arguments})
        assert unknown['result']['isError'] is True
        assert unknown['result']['structuredContent']['operation_id'] == op
        assert unknown['result']['structuredContent']['state'] == 'unknown'
        duplicate = await call('tools/call', {'name': 'subchat_send', 'arguments': arguments})
        assert duplicate['result']['structuredContent']['data']['state'] == 'sending'
        target = {'operation_id': op}
        pending = await call('tools/call', {'name': 'subchat_recover', 'arguments': target})
        assert pending['result']['structuredContent']['data']['state'] == 'submitted'
        backend.thinking = False
        done = await call('tools/call', {'name': 'subchat_recover', 'arguments': target})
        assert done['result']['structuredContent']['data']['answer'] == '42'
        assert backend.sends == 1
        invalid = await call('tools/call', {'name': 'subchat_recover',
                                           'arguments': {**target, 'owner': 'injected'}})
        assert invalid['result']['isError'] is True
        assert 'injected' not in str(invalid)
        # A queued send is dispatched by a different recovery request. Losing
        # its receipt must identify the submission, not that observer request.
        queued_id, observer_id = '3' * 32, '4' * 32
        await call('tools/call', {'name': 'subchat_message', 'arguments': {
            'request_id': queued_id, 'mode': 'queue', 'target_operation_id': op,
            'prompt': 'follow-up'}})
        lost = await call('tools/call', {'name': 'subchat_recover', 'arguments': {
            'request_id': observer_id, 'operation_id': queued_id}})
        reply = lost['result']['structuredContent']
        assert reply['operation_id'] == observer_id
        assert reply['state'] == 'unknown'
        assert reply['data']['submission_operation_id'] == queued_id
        assert backend.sends == 2
        saved = await call('tools/call', {'name': 'subchat_status', 'arguments': {
            'operation_id': reply['data']['submission_operation_id']}})
        assert saved['result']['structuredContent']['data']['state'] == 'sending'
        assert backend.sends == 2
    finally:
        ledger.close()


async def test_mcp_session_owner_isolates_saved_operations_and_downloads(tmp_path):
    from types import SimpleNamespace

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    downloads = []

    class Backend(BrowserFixture):
        image_download_available = True

        async def download_sandbox_file(self, operation_id, sandbox_link, *, max_bytes):
            downloads.append(('file', operation_id))
            return SimpleNamespace(file_name='result.txt', mime_type='text/plain',
                                   file_size_bytes=2, content=b'ok')

        async def download_image(self, operation_id, *, max_bytes):
            downloads.append(('image', operation_id))
            return SimpleNamespace(file_size_bytes=2, content=b'ok', mime_type='image/png',
                                   submission_state='completed', width=1, height=1)

    backend = Backend()
    service = Subchats(store, backend)
    alice_id, bob_id = 'a' * 32, 'b' * 32
    for owner, operation_id, user_message in (
        ('alice', alice_id, 'alice-user'), ('bob', bob_id, 'bob-user')
    ):
        store.prepare(operation_id, owner, 'model', 'effort', owner=owner)
        store.begin_send(operation_id, owner=owner)
        store.submitted(operation_id, f'{owner}-chat', user_message, owner=owner)
        store.complete(operation_id, f'{owner}-answer', f'{owner}-reply', owner=owner)
    alice = session(service, owner='alice')
    bob = session(service, owner='bob')

    async def call(server, tool, target, **extra):
        return await server.execute(Request(operation_id='c' * 32, tool=tool,
                                            arguments={'operation_id': target, **extra}))

    try:
        own = await call(alice, 'subchat_status', alice_id)
        assert own.state == 'completed' and own.data['answer'] == 'alice-reply'
        for tool in ('subchat_status', 'subchat_recover', 'subchat_wait',
                     'subchat_cancel', 'subchat_queue_watch'):
            foreign = await call(alice, tool, bob_id)
            assert foreign.state == 'failed'
            assert foreign.data['error_code'] == 'unknown_operation'
        listing = await alice.execute(Request(operation_id='d' * 32,
                                              tool='subchat_list', arguments={}))
        assert [item['operation_id'] for item in listing.data['submissions']] == [alice_id]
        queue = await alice.execute(Request(operation_id='e' * 32, tool='subchat_message',
                                            arguments={'mode': 'queue',
                                                       'target_operation_id': bob_id,
                                                       'prompt': 'foreign follow-up'}))
        assert queue.data['error_code'] == 'unknown_operation'
        duplicate = await alice.execute(Request(operation_id=bob_id, tool='subchat_send',
                                                arguments={'prompt': 'different',
                                                           'model': 'model', 'effort': 'effort'}))
        assert duplicate.data['error_code'] == 'unknown_operation'
        assert backend.sends == 0

        for tool, extra in (
            ('subchat_download_file', {'sandbox_link': 'sandbox:/result.txt'}),
            ('subchat_download_image', {}),
        ):
            previous_downloads = list(downloads)
            foreign = await call(alice, tool, bob_id, **extra)
            assert foreign.data['error_code'] == 'unknown_operation'
            assert downloads == previous_downloads
            own_download = await call(alice, tool, alice_id, **extra)
            assert own_download.state == 'completed'
            assert own_download.data['content_base64'] == 'b2s='
        assert downloads == [('file', alice_id), ('image', alice_id)]
        own_send_id = '1' * 32
        sent = await alice.execute(Request(operation_id=own_send_id, tool='subchat_send',
                                           arguments={'prompt': 'alice input', 'model': 'model',
                                                      'effort': 'effort'}))
        assert sent.state == 'unknown'
        assert store.get(own_send_id, owner='alice').state == 'sending'
        assert (await call(bob, 'subchat_status', own_send_id)).data['error_code'] == (
            'unknown_operation')
        own_queue_id = '2' * 32
        queued = await alice.execute(Request(operation_id=own_queue_id,
                                             tool='subchat_message', arguments={
                                                 'mode': 'queue',
                                                 'target_operation_id': alice_id,
                                                 'prompt': 'alice follow-up'}))
        assert queued.state == 'completed'
        assert store.get(own_queue_id, owner='alice').state == 'queued'
        assert (await call(bob, 'subchat_status', own_queue_id)).data['error_code'] == (
            'unknown_operation')
        assert (await call(bob, 'subchat_status', bob_id)).data['answer'] == 'bob-reply'
        assert (await call(bob, 'subchat_status', alice_id)).data['error_code'] == (
            'unknown_operation')
    finally:
        await alice.close()
        await bob.close()
        ledger.close()


async def test_official_sdk_subchat_stdio_reuses_durable_submission(tmp_path):
    import asyncio
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
    async def prepare(self, submission): return ()
    async def send(self, submission):
        with (Path(sys.argv[1]) / 'sends').open('a') as output: output.write('sent\\n')
        return SubchatReceipt(conversation_id='fixture-chat', user_message_id='user',
                              prompt=submission.prompt)
    async def find_submission(self, submission): return None
    async def read_answer(self, submission):
        return SubchatAnswer(conversation_id='fixture-chat', user_message_id='user',
                              prompt=submission.prompt, answer_message_id='answer', text='42')
async def main():
    ledger = Ledger(Path(sys.argv[1]))
    try:
        service = Subchats(SubchatSubmissions(ledger.connection), Provider())
        await serve_stdio(session(service), sys.stdin.buffer, sys.stdout.buffer)
    finally: ledger.close()
asyncio.run(main())
'''
    parameters = StdioServerParameters(command=sys.executable,
                                       args=['-u', '-c', program, str(tmp_path)])
    arguments = {'request_id': '2' * 32, 'prompt': '日本語\n42',
                 'model': 'dynamic model', 'effort': 'dynamic effort'}
    async with asyncio.timeout(20):
        # Closing the first SDK connection terminates its subprocess. Reopen the
        # same SQLite store in a fresh process, not just a fresh in-memory object.
        for _ in range(2):
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        'subchat_send', 'subchat_recover', 'subchat_status', 'subchat_wait',
                        'subchat_message', 'subchat_cancel', 'subchat_delete',
                        'subchat_list', 'subchat_queue_watch'}
                    sent = await client.call_tool('subchat_send', arguments)
                    assert not sent.isError
                    answer = await client.call_tool('subchat_recover',
                                                    {'operation_id': '2' * 32})
                    assert answer.structuredContent['data']['answer'] == '42'
    assert (tmp_path / 'sends').read_text().splitlines() == ['sent']


async def test_catalog_preserves_partial_observation_and_cannot_send(tmp_path):
    from anywhere_computer.models import Request

    ledger = Ledger(tmp_path)
    backend = BrowserFixture()
    expected = {'state': 'catalog_partial', 'models': [{'label': 'dynamic model'}],
                'efforts_for_selected_model': {'state': 'efforts_unconfirmed'},
                'submitted': False}

    async def observe(model):
        assert model in {None, 'dynamic model'}
        return expected

    async def observe_http():
        return {'state': 'http_catalog_observed', 'versions': [], 'submitted': False}

    try:
        server = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                         observe_catalog=observe, observe_http_catalog=observe_http)
        tools = await server.catalog()
        assert 'subchat_catalog' in [item['name'] for item in tools]
        reply = await server.execute(Request(operation_id='3' * 32, tool='subchat_catalog'))
        assert reply.state == 'completed'
        assert reply.data == expected
        selected = await server.execute(Request(operation_id='5' * 32, tool='subchat_catalog',
                                                 arguments={'model': 'dynamic model'}))
        assert selected.data == expected
        invalid = await server.execute(Request(operation_id='4' * 32, tool='subchat_catalog',
                                                arguments={'prompt': 'do not send'}))
        assert invalid.state == 'failed'
        http = await server.execute(Request(operation_id='6' * 32, tool='subchat_catalog',
                                             arguments={'source': 'http'}))
        assert http.data['state'] == 'http_catalog_observed'
        invalid_http = await server.execute(Request(operation_id='7' * 32, tool='subchat_catalog',
            arguments={'source': 'http', 'model': 'dynamic model'}))
        assert invalid_http.state == 'failed'
        assert backend.sends == 0
    finally:
        ledger.close()


async def test_wait_preserves_thinking_and_releases_browser_between_observations(tmp_path):
    import asyncio

    from anywhere_computer.models import Request

    observed = asyncio.Event()

    class ThinkingBrowser(BrowserFixture):
        async def read_answer(self, submission):
            observed.set()
            return await super().read_answer(submission)

    ledger = Ledger(tmp_path)
    backend = ThinkingBrowser()

    async def catalog(model):
        return {'state': 'catalog_partial', 'submitted': False}

    server = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                     observe_catalog=catalog)
    pending = None
    try:
        sent = await server.execute(Request(operation_id='a' * 32, tool='subchat_send',
            arguments={'prompt': 'work', 'model': 'model', 'effort': 'effort'}))
        assert sent.state == 'unknown'
        pending = asyncio.create_task(server.execute(Request(
            operation_id='b' * 32, tool='subchat_wait',
            arguments={'operation_id': 'a' * 32, 'wait_ms': 1000})))
        await asyncio.wait_for(observed.wait(), 2)
        other = await asyncio.wait_for(server.execute(Request(
            operation_id='c' * 32, tool='subchat_catalog')), .3)
        assert other.state == 'completed'
        assert not pending.done()
        timed_out = await pending
        assert timed_out.state == 'completed'
        assert timed_out.data['state'] == 'submitted'
        assert backend.thinking
        assert backend.sends == 1
        backend.thinking = False
        recovered = await server.execute(Request(operation_id='d' * 32, tool='subchat_wait',
            arguments={'operation_id': 'a' * 32, 'wait_ms': 1000}))
        assert recovered.data['answer'] == '42'
        assert backend.sends == 1
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        ledger.close()


async def test_wait_does_not_disguise_provider_timeout_as_normal_thinking(tmp_path):
    from anywhere_computer.models import Request

    class BrokenBrowser(BrowserFixture):
        async def read_answer(self, submission):
            raise TimeoutError('private provider details')

    ledger = Ledger(tmp_path)
    backend = BrokenBrowser()
    service = Subchats(SubchatSubmissions(ledger.connection), backend)
    server = session(service)
    try:
        await server.execute(Request(operation_id='a' * 32, tool='subchat_send',
            arguments={'prompt': 'work', 'model': 'model', 'effort': 'effort'}))
        failed = await server.execute(Request(operation_id='b' * 32, tool='subchat_wait',
            arguments={'operation_id': 'a' * 32, 'wait_ms': 1000}))
        assert failed.state == 'failed'
        assert failed.data['error_type'] == 'TimeoutError'
        assert 'private provider details' not in failed.model_dump_json()
        assert service.store.get('a' * 32, owner=None).state == 'submitted'
        assert backend.sends == 1
    finally:
        ledger.close()


async def test_wait_rejects_transport_exceeding_budget_before_observation(tmp_path):
    from anywhere_computer.models import Request

    class NoObservation(BrowserFixture):
        async def find_submission(self, submission):
            raise AssertionError('invalid wait reached browser')

    ledger = Ledger(tmp_path)
    backend = NoObservation()
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend))
    try:
        tools = await server.catalog()
        schema = next(tool for tool in tools if tool['name'] == 'subchat_wait')['inputSchema']
        assert schema['properties']['wait_ms']['maximum'] == 10_000
        assert schema['properties']['wait_ms']['default'] == 1000
        for duration in (10_001, 30_000, 60_000):
            reply = await server.execute(Request(operation_id='e' * 32, tool='subchat_wait',
                arguments={'operation_id': 'a' * 32, 'wait_ms': duration}))
            assert reply.state == 'failed'
            assert reply.data['error_code'] == 'invalid_parameter'
            assert any('wait_ms' in item['path'] for item in reply.data['invalid_params'])
            assert reply.data['dispatched'] is False
        assert backend.sends == 0
    finally:
        ledger.close()


async def test_preparation_failure_is_unsent_and_same_request_can_retry(tmp_path):
    from anywhere_computer.models import Request

    class UnreadyBrowser(BrowserFixture):
        ready = False

        async def prepare(self, submission):
            if not self.ready:
                raise ValueError('private draft and account details')
            return await super().prepare(submission)

    ledger = Ledger(tmp_path)
    backend = UnreadyBrowser()
    service = Subchats(SubchatSubmissions(ledger.connection), backend)
    server = session(service)
    request = Request(operation_id='f' * 32, tool='subchat_send',
                      arguments={'prompt': 'work', 'model': 'model', 'effort': 'effort'})
    try:
        failed = await server.execute(request)
        assert failed.state == 'failed'
        assert failed.data == {'error_code': 'preparation_failed', 'dispatched': False}
        assert 'private' not in failed.model_dump_json()
        assert service.store.get(request.operation_id, owner=None).state == 'prepared'
        assert backend.sends == 0
        # Once preparation is corrected, reuse the original identity. This
        # fixture loses the send receipt, so neither error path claims success.
        backend.ready = True
        unconfirmed = await server.execute(request)
        assert unconfirmed.state == 'unknown'
        assert backend.sends == 1
        duplicate = await server.execute(request)
        assert duplicate.data['state'] == 'sending'
        assert backend.sends == 1
    finally:
        await server.close()
        ledger.close()


async def test_preparation_failure_reports_only_a_known_local_reason(tmp_path):
    from anywhere_computer.models import Request

    class DraftBrowser(BrowserFixture):
        async def prepare(self, submission):
            raise ValueError('Ordinary Chat composer contains a draft')

    ledger = Ledger(tmp_path)
    backend = DraftBrowser()
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend))
    try:
        request = Request(operation_id='e' * 32, tool='subchat_send', arguments={
            'prompt': 'work', 'model': 'model', 'effort': 'effort'})
        failed = await server.execute(request)
        assert failed.state == 'failed'
        assert failed.data == {'error_code': 'preparation_failed',
                               'dispatched': False, 'reason': 'composer_has_draft'}
        assert backend.sends == 0
    finally:
        await server.close()
        ledger.close()


async def test_wait_drops_parent_observation_after_parent_completes(tmp_path):
    import asyncio

    from anywhere_computer.models import Request
    from anywhere_computer.subchat import SubchatPendingObservation

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'input', 'model', 'effort', owner=None)
    store.begin_send(parent, owner=None)
    store.submitted(parent, 'chat', 'input', owner=None)
    observed = asyncio.Event()

    class Backend(BrowserFixture):
        async def read_answer(self, submission):
            observed.set()
            return SubchatPendingObservation(operation_id=parent, reason='final_not_observed')

    service = Subchats(store, Backend())
    service.queue(child, parent, 'follow-up', owner=None)
    server = session(service)

    async def finish_parent():
        await observed.wait()
        store.complete(parent, 'answer', 'done', owner=None)

    finishing = asyncio.create_task(finish_parent())
    try:
        reply = await server.execute(Request(operation_id='c' * 32, tool='subchat_wait',
            arguments={'operation_id': child, 'wait_ms': 100}))
        await finishing
        assert store.get(parent, owner=None).state == 'completed'
        assert reply.data['state'] == 'queued'
        assert 'observation' not in reply.data
    finally:
        finishing.cancel()
        await asyncio.gather(finishing, return_exceptions=True)
        await server.close()
        ledger.close()


async def test_session_owner_scopes_ledger_tools_and_download_authorization(tmp_path):
    from anywhere_computer.models import Request

    class DownloadBackend(BrowserFixture):
        file_reads = 0
        image_reads = 0

        async def download_sandbox_file(self, operation_id, sandbox_link, *, max_bytes):
            self.file_reads += 1
            raise AssertionError('Cross-owner file download reached backend')

        async def download_image(self, operation_id, *, max_bytes):
            self.image_reads += 1
            raise AssertionError('Cross-owner image download reached backend')

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    backend = DownloadBackend()
    service = Subchats(store, backend)
    alice_id, bob_id = 'a' * 32, 'b' * 32
    store.prepare(alice_id, 'alice prompt', 'model', 'effort', owner='alice')
    store.prepare(bob_id, 'bob prompt', 'model', 'effort', owner='bob')
    alice = session(service, owner='alice')
    bob = session(service, owner='bob')
    anonymous = session(service)

    async def run(server, tool, operation_id, arguments):
        return await server.execute(Request(operation_id=operation_id, tool=tool,
                                            arguments=arguments))

    try:
        for server, own_id, foreign_id in ((alice, alice_id, bob_id),
                                           (bob, bob_id, alice_id)):
            listed = await run(server, 'subchat_list', '1' * 32, {})
            assert [item['operation_id'] for item in listed.data['submissions']] == [own_id]
            own = await run(server, 'subchat_status', '2' * 32,
                            {'operation_id': own_id})
            assert own.data['operation_id'] == own_id
            for tool in ('subchat_status', 'subchat_recover', 'subchat_wait',
                         'subchat_queue_watch', 'subchat_cancel'):
                rejected = await run(server, tool, '3' * 32,
                                     {'operation_id': foreign_id})
                assert rejected.data['error_code'] == 'unknown_operation'
            queued = await run(server, 'subchat_message', '4' * 32,
                               {'mode': 'queue', 'target_operation_id': foreign_id,
                                'prompt': 'foreign queue'})
            assert queued.data['error_code'] == 'unknown_operation'
            deleted = await run(server, 'subchat_delete', '5' * 32,
                                {'operation_id': foreign_id,
                                 'conversation_id': '00000000-0000-0000-0000-000000000001'})
            assert deleted.data['error_code'] == 'unknown_operation'
            sent = await run(server, 'subchat_send', foreign_id,
                             {'prompt': 'foreign prompt', 'model': 'model', 'effort': 'effort'})
            assert sent.data['error_code'] == 'unknown_operation'
            file = await run(server, 'subchat_download_file', '6' * 32,
                             {'operation_id': foreign_id, 'sandbox_link': 'sandbox:/foreign'})
            assert file.data['error_code'] == 'unknown_operation'
            image = await run(server, 'subchat_download_image', '7' * 32,
                              {'operation_id': foreign_id})
            assert image.data['error_code'] == 'unknown_operation'

        assert backend.sends == backend.file_reads == backend.image_reads == 0
        assert store.get(alice_id, owner='alice').state == 'prepared'
        assert store.get(bob_id, owner='bob').state == 'prepared'
        empty = await run(anonymous, 'subchat_list', '8' * 32, {})
        assert empty.data['submissions'] == []
        cancelled = await run(alice, 'subchat_cancel', '9' * 32,
                              {'operation_id': alice_id})
        assert cancelled.data['state'] == 'cancelled'
        assert store.get(bob_id, owner='bob').state == 'prepared'
    finally:
        await alice.close()
        await bob.close()
        await anonymous.close()
        ledger.close()
