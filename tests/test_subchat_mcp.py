from test_subchat_lifecycle import BrowserFixture

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
        await server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        catalog = await call('tools/list', {})
        names = {tool['name'] for tool in catalog['result']['tools']}
        assert names == {'subchat_send', 'subchat_recover', 'subchat_status', 'subchat_wait',
                        'subchat_message', 'subchat_cancel', 'subchat_list'}
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
                        'subchat_message', 'subchat_cancel', 'subchat_list'}
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

    try:
        server = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                         observe_catalog=observe)
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
            assert reply.data['error_type'] == 'ValidationError'
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
