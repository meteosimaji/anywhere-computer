"""Browser-free controller acceptance with synthetic sessions, never live generation."""
import json
from contextlib import asynccontextmanager
from io import BytesIO, StringIO

import httpx
import pytest
from test_subchat_http_catalog import catalog
from test_subchat_http_history import sample

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAccessError, SubchatInterrupted, Subchats
from anywhere_computer.subchat_cli import process_lines
from anywhere_computer.subchat_state import SubchatAccountMismatch, SubchatSubmissions

CATALOG_URL = 'https://chatgpt.com/backend-api/models?language=ja'
SECRET = 'Bearer fixture-secret-not-a-live-token'


@pytest.mark.parametrize('status', [401, 403])
async def test_chrome_login_rejection_keeps_capabilities_but_blocks_http(status):
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    async def no_request():
        raise AssertionError('Rejected startup must not make another HTTP request')

    backend = HTTPOnlySubchatBackend(
        no_request, chrome_login=True, startup_access_status=status)
    capabilities = backend.capabilities()
    assert capabilities['authentication_state'] == (
        'authentication_required' if status == 401 else 'access_denied')
    assert capabilities['authenticated_account_id'] is None
    assert capabilities['generation_transport'] == 'unavailable'
    with pytest.raises(SubchatAccessError) as rejected:
        await backend.http_catalog()
    assert rejected.value.status == status


async def test_pinned_account_mismatch_keeps_tools_and_reports_specific_state():
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    async def no_request():
        raise AssertionError('Mismatched account must not make a catalog request')

    backend = HTTPOnlySubchatBackend(
        no_request, chrome_login=True, startup_access_status=401,
        startup_account_mismatch=True)
    assert backend.capabilities()['authentication_state'] == 'account_mismatch'
    with pytest.raises(SubchatAccountMismatch):
        await backend.http_catalog()


def session_payload():
    return {'authorization': SECRET, 'account_id': 'fixture-account',
            'catalog_url': CATALOG_URL, 'language': 'ja'}


def credentials():
    from anywhere_computer.subchat_http_session import read_http_session

    return read_http_session(BytesIO(json.dumps(session_payload()).encode() + b'\n'))


class Client:
    """Records exact GET contracts; deliberately has no post or browser method."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.status = {}
        self.disposed = 0

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        assert method == 'GET'
        self.calls.append((url, kwargs))
        assert kwargs == {'headers': {'authorization': SECRET,
                                     'chatgpt-account-id': 'fixture-account',
                                     'oai-language': 'ja'},
                          'timeout': 15.0, 'follow_redirects': False}

        payload_value = self.payload
        response_status = self.status.get(url, 200)
        owner = self

        class Response:
            status_code = response_status
            headers = {'content-type': 'application/json'}

            async def aiter_bytes(self, *, chunk_size):
                assert self.status_code == 200, 'Never read a refusal body'
                body = json.dumps(catalog() if url == CATALOG_URL else payload_value).encode()
                for offset in range(0, len(body), chunk_size):
                    yield body[offset:offset + chunk_size]

            async def aclose(inner_self):
                owner.disposed += 1

        response = Response()
        try:
            yield response
        finally:
            await response.aclose()


def backend(client, *, authenticated=True):
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    async def factory():
        return client

    return HTTPOnlySubchatBackend(factory, credentials() if authenticated else None)


async def test_chrome_read_refreshes_once_after_401_without_resending():
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    _, payload = sample()
    old, fresh = Client(payload), Client(payload)
    old.status[CATALOG_URL] = 401
    refreshes = []

    async def factory():
        return old

    async def fresh_factory():
        return fresh

    async def refresh(expected_account):
        refreshes.append(expected_account)
        return credentials(), fresh_factory

    adapter = HTTPOnlySubchatBackend(factory, credentials(), chrome_login=True,
                                     refresh_session=refresh)
    result = await adapter.http_catalog()
    assert result['state'] == 'http_catalog_observed'
    assert refreshes == ['fixture-account']
    assert len(old.calls) == len(fresh.calls) == 1
    assert old.disposed == fresh.disposed == 1
    assert adapter.capabilities()['credential_refresh'] is True
    assert adapter.capabilities()['authentication_state'] == 'authenticated'


async def test_chrome_read_refresh_is_single_flight_for_concurrent_401():
    import asyncio

    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    _, payload = sample()
    old, fresh = Client(payload), Client(payload)
    old.status[CATALOG_URL] = 401
    refreshes = 0

    async def factory():
        return old

    async def fresh_factory():
        return fresh

    async def refresh(_expected_account):
        nonlocal refreshes
        refreshes += 1
        await asyncio.sleep(0)
        return credentials(), fresh_factory

    adapter = HTTPOnlySubchatBackend(factory, credentials(), chrome_login=True,
                                     refresh_session=refresh)
    first, second = await asyncio.gather(adapter.http_catalog(), adapter.http_catalog())
    assert first['state'] == second['state'] == 'http_catalog_observed'
    assert refreshes == 1
    assert len(fresh.calls) == 2


async def test_chrome_refresh_rejects_other_account_and_403_does_not_refresh():
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
    from anywhere_computer.subchat_http_session import ObservedHTTPSession

    _, payload = sample()
    client = Client(payload)
    client.status[CATALOG_URL] = 401
    refreshes = 0

    async def factory():
        return client

    async def changed_account(_expected_account):
        nonlocal refreshes
        refreshes += 1
        return (ObservedHTTPSession.model_validate({
            **session_payload(), 'account_id': 'another-account'}), factory)

    adapter = HTTPOnlySubchatBackend(factory, credentials(), chrome_login=True,
                                     refresh_session=changed_account)
    with pytest.raises(SubchatAccountMismatch):
        await adapter.http_catalog()
    assert adapter.capabilities()['authenticated_account_id'] == 'fixture-account'
    assert adapter.capabilities()['authentication_state'] == 'authentication_required'
    with pytest.raises(SubchatAccessError):
        await adapter.http_catalog()
    assert refreshes == 1  # Cooldown prevents a snapshot loop.

    client.status[CATALOG_URL] = 403
    other = HTTPOnlySubchatBackend(factory, credentials(), chrome_login=True,
                                   refresh_session=changed_account)
    with pytest.raises(SubchatAccessError) as denied:
        await other.http_catalog()
    assert denied.value.status == 403
    assert refreshes == 1


@pytest.mark.parametrize(('resource', 'limit', 'error'), [
    ('catalog', 1_048_576, 'Model catalog is too large'),
    ('history', 4_194_304, 'Conversation response is too large'),
])
async def test_http_reader_stops_stream_at_resource_limit(resource, limit, error):
    from anywhere_computer.subchat_browser.http_reader import ChatHTTPReader

    class Stream(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            for _ in range(limit // 65_536 + 10):
                self.chunks += 1
                yield b'x' * 65_536

        async def aclose(self):
            self.closed = True

    stream = Stream()

    def serve(_request):
        return httpx.Response(200, stream=stream,
                              headers={'content-type': 'application/json'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        async def factory():
            return client

        reader = ChatHTTPReader(factory, browser_free=True, session=credentials())
        url = CATALOG_URL if resource == 'catalog' else (
            'https://chatgpt.com/backend-api/conversations/fixture')

        async def no_browser(_page):
            raise AssertionError('Browser was opened')

        with pytest.raises(ValueError, match=error):
            await reader._read(None, url, no_browser)
    assert stream.chunks == limit // 65_536 + 1
    assert stream.closed


def seed(store, *, receipt=True, account='fixture-account'):
    submission, payload = sample()
    store.prepare(submission.operation_id, submission.prompt, submission.model,
                  submission.effort, owner=None)
    store.begin_send(submission.operation_id, owner=None)
    store.observe_request(submission.operation_id, submission.user_message_id,
                          owner=None, provider_account_id=account)
    store.observe_conversation(submission.operation_id, submission.user_message_id,
                               submission.conversation_id, owner=None, provider_account_id=account)
    if receipt:
        store.submitted(submission.operation_id, submission.conversation_id,
                        submission.user_message_id, owner=None)
    return submission, payload


async def test_http_only_catalog_receipt_answer_and_saved_restart(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = seed(store, receipt=False)
        client = Client(payload)
        adapter = backend(client)
        observed = await adapter.http_catalog()
        assert observed['source'] == 'preauthenticated_http'
        assert observed['generation_transport'] == 'unavailable'
        assert observed['http_selection_send_supported'] is False
        assert observed['versions'][0]['choices'][0]['model_slug'] == 'future-chat'
        service = Subchats(store, adapter)
        done = await service.recover(submission.operation_id, owner=None)
        assert done.state == 'completed' and done.answer == '日本語 result'
        assert done.answer_message_id == 'answer'
        assert len(client.calls) == client.disposed == 3
    finally:
        ledger.close()
    # New ledger and adapter: terminal recovery needs neither a session nor a request.
    ledger = Ledger(tmp_path)
    try:
        adapter = backend(client, authenticated=False)
        service = Subchats(SubchatSubmissions(ledger.connection), adapter)
        recovered = await service.recover(submission.operation_id, owner=None)
        duplicate = await service.send(submission.operation_id, submission.prompt,
                                       submission.model, submission.effort, owner=None)
        assert duplicate == recovered == done
        assert len(client.calls) == 3
    finally:
        ledger.close()
    for path in tmp_path.iterdir():
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()


@pytest.mark.parametrize('status', [401, 403, 429, 500, 302])
async def test_http_only_denial_scope_disposal_and_no_implicit_retries(status):
    submission, payload = sample()
    submission = submission.model_copy(update={'provider_account_id': 'fixture-account'})
    client = Client(payload)
    adapter = backend(client)
    url = 'https://chatgpt.com/backend-api/conversations/' + submission.conversation_id
    client.status[url] = status
    expected = SubchatAccessError if status in (401, 403) else ConnectionError
    with pytest.raises(expected):
        await adapter.read_answer(submission)
    assert len(client.calls) == client.disposed == 1
    if status in (401, 403):
        with pytest.raises(SubchatAccessError) as caught:
            await adapter.read_answer(submission)
        assert caught.value.status == status
        assert len(client.calls) == 1
        if status == 401:
            with pytest.raises(SubchatAccessError):
                await adapter.http_catalog()
            assert len(client.calls) == 1
        else:
            assert (await adapter.http_catalog())['state'] == 'http_catalog_observed'
            assert len(client.calls) == 2


@pytest.mark.parametrize('case', ['account', 'unknown_identity', 'path_injection', 'wrong_prompt',
                                  'wrong_answer', 'interrupted'])
async def test_http_only_identity_and_final_gates(case):
    submission, payload = sample()
    submission = submission.model_copy(update={'provider_account_id': 'fixture-account'})
    if case == 'account':
        submission = submission.model_copy(update={'provider_account_id': 'other-account'})
    elif case == 'unknown_identity':
        submission = submission.model_copy(update={'conversation_id': None})
    elif case == 'path_injection':
        submission = submission.model_copy(update={'conversation_id': '../models?secret=oops'})
    elif case == 'wrong_prompt':
        payload['messages'][0]['content']['parts'] = ['not the requested input']
    elif case == 'wrong_answer':
        payload['messages'][1]['metadata']['request_id'] = 'unrelated-request'
    else:
        payload['messages'][1]['metadata']['finish_details'] = {'type': 'interrupted'}
    client = Client(payload)
    adapter = backend(client)
    if case == 'unknown_identity':
        assert await adapter.find_submission(submission) is None
        assert await adapter.read_answer(submission) is None
    elif case == 'wrong_answer':
        assert (await adapter.read_answer(submission)).reason == 'final_not_observed'
    else:
        exception = (SubchatAccountMismatch if case == 'account' else
                     SubchatInterrupted if case == 'interrupted' else ValueError)
        with pytest.raises(exception):
            await adapter.read_answer(submission)
    no_request = case in {'account', 'unknown_identity', 'path_injection'}
    assert len(client.calls) == (0 if no_request else 1)


async def test_http_only_send_queue_and_queued_recovery_never_dispatch(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = seed(store)
        store.complete(submission.operation_id, 'answer', 'saved answer', owner=None)
        client = Client(payload)
        service = Subchats(store, backend(client))
        # Existing queued proposals must stay queued even when their parent is complete.
        store.prepare('c' * 32, 'existing follow-up', submission.model, submission.effort,
                      owner=None, conversation_id=submission.conversation_id,
                      after_operation_id=submission.operation_id)
        commands = [
            {'action': 'send', 'operation_id': 'b' * 32, 'prompt': 'new work',
             'model': 'model', 'effort': 'effort'},
            {'action': 'queue', 'operation_id': 'd' * 32,
             'target_operation_id': submission.operation_id, 'prompt': 'new queue'},
            {'action': 'recover', 'operation_id': 'c' * 32},
        ]
        output = StringIO()
        await process_lines(service, StringIO(''.join(json.dumps(x) + '\n' for x in commands)),
                            output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        assert [reply['state'] for reply in replies] == ['http_generation_unavailable'] * 3
        assert all(reply['automatic_retry'] is False for reply in replies)
        assert all(reply['dispatched'] is False for reply in replies)
        assert store.get('b' * 32, owner=None).state == 'prepared'
        assert store.get('c' * 32, owner=None).state == 'queued'
        with pytest.raises(ValueError, match='Unknown'):
            store.get('d' * 32, owner=None)
        assert client.calls == []
    finally:
        ledger.close()


async def test_http_only_no_session_is_explicit_and_does_not_request(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = seed(store)
        client = Client(payload)
        service = Subchats(store, backend(client, authenticated=False))
        output = StringIO()
        commands = [{'action': 'capabilities'}, {'action': 'catalog'},
                    {'action': 'recover', 'operation_id': submission.operation_id}]
        await process_lines(service, StringIO(''.join(json.dumps(x) + '\n' for x in commands)),
                            output)
        capabilities, catalog_result, recovery = map(json.loads, output.getvalue().splitlines())
        assert capabilities['generation_transport'] == 'unavailable'
        assert capabilities['browser_required'] is False
        assert capabilities['credential_refresh'] is False
        assert catalog_result['state'] == recovery['state'] == 'http_session_required'
        assert client.calls == []
    finally:
        ledger.close()


@pytest.mark.parametrize('change', ['cookie_newline', 'cookie_del', 'protection_token',
                                  'foreign_origin', 'userinfo',
                                  'port', 'fragment', 'other_path', 'header_newline', 'oversized',
                                  'duplicate', 'bad_json', 'missing_account'])
def test_session_input_rejects_unsafe_envelopes_without_secret_diagnostics(change):
    from anywhere_computer.subchat_http_session import read_http_session

    data = session_payload()
    if change == 'cookie_newline':
        data['cookie'] = 'session=fixture\r\nInjected: bad'
    elif change == 'cookie_del':
        data['cookie'] = 'session=fixture\x7f'
    elif change == 'protection_token':
        data['openai-sentinel-proof-token'] = 'fixture'
    elif change in {'foreign_origin', 'userinfo', 'port', 'fragment', 'other_path'}:
        data['catalog_url'] = {
            'foreign_origin': 'https://example.com/backend-api/models',
            'userinfo': 'https://user@chatgpt.com/backend-api/models',
            'port': 'https://chatgpt.com:443/backend-api/models',
            'fragment': CATALOG_URL + '#fragment',
            'other_path': 'https://chatgpt.com/backend-api/f/conversation',
        }[change]
    elif change == 'header_newline':
        data['authorization'] = SECRET + '\r\nCookie: fixture'
    elif change == 'missing_account':
        del data['account_id']
    raw = json.dumps(data).encode() + b'\n'
    if change == 'oversized':
        raw = b' ' * 32769
    elif change == 'duplicate':
        raw = raw.rstrip()[:-1] + b',"account_id":"another"}\n'
    elif change == 'bad_json':
        raw = SECRET.encode() + b'\n'
    with pytest.raises(ValueError) as caught:
        read_http_session(BytesIO(raw))
    assert SECRET not in str(caught.value)
    assert 'fixture' not in str(caught.value)


def test_session_consumes_one_line_and_masks_representation():
    from anywhere_computer.subchat_http_session import read_http_session

    stream = BytesIO(json.dumps(session_payload()).encode() + b'\n{"action":"catalog"}\n')
    observed = read_http_session(stream)
    assert SECRET not in repr(observed)
    assert SECRET not in observed.model_dump_json()
    assert stream.readline() == b'{"action":"catalog"}\n'


async def test_http_only_unknown_403_survives_reopen_without_resend(tmp_path):
    ledger = Ledger(tmp_path)
    submission, payload = sample()
    try:
        store = SubchatSubmissions(ledger.connection)
        store.prepare(submission.operation_id, submission.prompt, submission.model,
                      submission.effort, owner=None)
        store.begin_send(submission.operation_id, owner=None)
        store.observe_request(submission.operation_id, submission.user_message_id,
                              owner=None, provider_account_id='fixture-account')
        store.observe_rejection(submission.operation_id, submission.user_message_id, 403,
                                owner=None, provider_account_id='fixture-account')
    finally:
        ledger.close()
    client = Client(payload)
    ledger = Ledger(tmp_path)
    try:
        service = Subchats(SubchatSubmissions(ledger.connection),
                           backend(client, authenticated=False))
        recovered = await service.recover(submission.operation_id, owner=None)
        duplicate = await service.send(submission.operation_id, submission.prompt,
                                       submission.model, submission.effort, owner=None)
        assert recovered == duplicate
        assert recovered.state == 'sending' and recovered.generation_http_status == 403
        assert recovered.conversation_id is None and recovered.answer is None
        assert client.calls == []
    finally:
        ledger.close()


async def test_http_only_legacy_receipt_does_not_infer_account_binding(tmp_path):
    submission, payload = sample()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        store.prepare(submission.operation_id, submission.prompt, submission.model,
                      submission.effort, owner=None)
        store.begin_send(submission.operation_id, owner=None)
        store.submitted(submission.operation_id, submission.conversation_id,
                        submission.user_message_id, owner=None)
        service = Subchats(store, backend(Client(payload)))
        done = await service.recover(submission.operation_id, owner=None)
        assert done.state == 'completed' and done.provider_account_id is None
        assert store.connection.execute('SELECT count(*) FROM subchat_account_bindings').fetchone()[
            0] == 0
    finally:
        ledger.close()


@pytest.mark.parametrize('transport', ['browser', 'http'])
async def test_capabilities_describe_dispatch_without_starting_transport(transport):
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    async def forbidden():
        raise AssertionError('Capability inspection must not start a transport')

    provider = (BrowserSubchatBackend(forbidden) if transport == 'browser'
                else HTTPOnlySubchatBackend(forbidden))
    caps = provider.capabilities()
    assert caps['queue_dispatch'] == (
        'recover_or_wait' if transport == 'browser' else 'unavailable')
    assert caps['native_steer'] is False
    assert caps['provider_stop'] is False
    assert caps['cancel_scope'] == 'local_queued_or_prepared'
    assert caps['background_dispatcher'] is False


async def test_independent_http_recovery_does_not_wait_for_slow_peer(tmp_path):
    import asyncio

    from anywhere_computer.models import Request
    from anywhere_computer.subchat import SubchatAnswer
    from anywhere_computer.subchat_mcp import session

    entered, release = asyncio.Event(), asyncio.Event()

    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    class PendingHTTP(HTTPOnlySubchatBackend):
        async def read_answer(self, submission):
            if submission.operation_id == 'a' * 32:
                entered.set()
                await release.wait()
            return SubchatAnswer(conversation_id=submission.conversation_id,
                user_message_id=submission.user_message_id, prompt=submission.prompt,
                answer_message_id='answer-' + submission.operation_id, text=submission.prompt)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    for key in ('a', 'b'):
        store.prepare(key * 32, key, 'model', 'effort', owner=None)
        store.begin_send(key * 32, owner=None)
        store.submitted(key * 32, 'chat-' + key, 'user-' + key, owner=None)
    async def unused():
        raise AssertionError('No network used in scheduling fixture')
    server = session(Subchats(store, PendingHTTP(unused)), serialize_recovery=False)
    async def recover(key):
        return await server.execute(Request(operation_id=key * 32, tool='subchat_recover',
            arguments={'operation_id':key * 32}))
    slow = asyncio.create_task(recover('a'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        fast = await asyncio.wait_for(recover('b'), 1)
        assert fast.data['answer'] == 'b'
        assert not slow.done()
        release.set()
        assert (await slow).data['answer'] == 'a'
    finally:
        release.set()
        await server.close()
        await asyncio.gather(slow, return_exceptions=True)
        ledger.close()


@pytest.mark.parametrize('status', [401, 403])
async def test_queued_http_read_rechecks_access_after_client_initialization(status):
    import asyncio

    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    submission, payload = sample()
    client = Client(payload)
    client.status['https://chatgpt.com/backend-api/conversations/' +
                  submission.conversation_id] = status
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return client

    adapter = HTTPOnlySubchatBackend(factory, credentials())
    pending = asyncio.create_task(adapter.read_answer(submission))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(SubchatAccessError):
            await adapter.read_answer(submission)
        release.set()
        with pytest.raises(SubchatAccessError) as error:
            await pending
        assert error.value.status == status
        assert len(client.calls) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
