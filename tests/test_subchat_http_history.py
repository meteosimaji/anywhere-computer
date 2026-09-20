"""Saved final answers must match the original input, not merely look complete."""
import json

import pytest

from anywhere_computer.subchat import (
    SubchatAccessError,
    SubchatBrowserClosed,
    SubchatInterrupted,
)
from anywhere_computer.subchat_browser.history import project_history, project_observation
from anywhere_computer.subchat_state import SubchatAccountMismatch, SubchatSubmission


def sample():
    submission = SubchatSubmission(operation_id='a' * 32, prompt='日本語 work', model='dynamic',
        effort='dynamic', state='submitted', conversation_id='00000000-0000-0000-0000-000000000001',
        user_message_id='user')
    binding = {'request_id': 'request', 'turn_exchange_id': 'exchange', 'working_turn_id': 'work'}
    payload = {'conversation_id': submission.conversation_id, 'messages': [
        {'id': 'user', 'author': {'role': 'user'},
         'content': {'content_type': 'text', 'parts': [submission.prompt]},
         'metadata': binding.copy(), 'status': 'finished_successfully'},
        {'id': 'answer', 'author': {'role': 'assistant'},
         'content': {'content_type': 'text', 'parts': ['日本語 result']},
         'metadata': {**binding, 'is_complete': True, 'finish_details': {'type': 'stop'}},
         'status': 'finished_successfully', 'channel': 'final', 'end_turn': True},
    ]}
    return submission, payload


@pytest.mark.parametrize('case', ['complete', 'interrupted', 'thinking', 'empty', 'other_request',
                                  'missing_binding', 'multiple_finals', 'missing_user',
                                  'wrong_prompt', 'wrong_conversation', 'unknown_finish',
                                  'shared_binding'])
def test_history_completion_and_identity(case):
    submission, payload = sample()
    user, answer = payload['messages']
    if case == 'interrupted':
        # A real stopped response also retained all three success-looking fields.
        answer['metadata']['finish_details'] = {'type': 'interrupted', 'reason': 'client_stopped'}
    elif case == 'thinking':
        answer['channel'] = 'analysis'
    elif case == 'empty':
        answer['content']['parts'] = ['']
    elif case == 'other_request':
        answer['metadata']['request_id'] = 'other'
    elif case == 'missing_binding':
        del user['metadata']['working_turn_id']
    elif case == 'multiple_finals':
        payload['messages'].append({**answer, 'id': 'another'})
    elif case == 'shared_binding':
        payload['messages'].append({**user, 'id': 'another-user'})
    elif case == 'missing_user':
        payload['messages'].remove(user)
    elif case == 'wrong_prompt':
        user['content']['parts'] = ['different']
    elif case == 'wrong_conversation':
        payload['conversation_id'] = 'other'
    elif case == 'unknown_finish':
        answer['metadata']['finish_details'] = {'type': 'future-value'}
    if case in {'interrupted', 'wrong_prompt', 'wrong_conversation'}:
        with pytest.raises(SubchatInterrupted if case == 'interrupted' else ValueError):
            project_history(json.dumps(payload).encode(), submission)
    else:
        result = project_history(json.dumps(payload).encode(), submission)
        if case == 'complete':
            assert result.text == '日本語 result' and result.answer_message_id == 'answer'
        else:
            assert result is None
            reasons = {
                'thinking': 'final_not_observed', 'empty': 'final_text_unavailable',
                'other_request': 'final_not_observed', 'missing_binding': 'correlation_unavailable',
                'multiple_finals': 'final_ambiguous', 'missing_user': 'input_not_observed',
                'unknown_finish': 'final_not_complete', 'shared_binding': 'correlation_ambiguous',
            }
            observation = project_observation(json.dumps(payload).encode(), submission)
            assert observation.reason == reasons[case]
            assert observation.operation_id == submission.operation_id
            assert set(observation.model_dump()) == {
                'operation_id', 'source', 'reason', 'observed_at'}


@pytest.mark.parametrize('interrupted', [False, True])
async def test_backend_observes_history_without_send_or_original_tab_reload(interrupted):
    from playwright.async_api import Error, async_playwright

    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    submission, payload = sample()
    if interrupted:
        payload['messages'][1]['metadata']['finish_details'] = {'type': 'interrupted'}
    async with async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required')
            raise
        try:
            context = await browser.new_context()
            original = await context.new_page()
            calls = []
            path = '/backend-api/conversations/' + submission.conversation_id
            async def route(r):
                calls.append((r.request.method, r.request.url))
                if path in r.request.url:
                    await r.fulfill(content_type='application/json', body=json.dumps(payload))
                else:
                    await r.fulfill(content_type='text/html', body='<script>fetch(' +
                        json.dumps(path) +
                            ',{headers:{Authorization:"Bearer fixture"}})</script>')
            await context.route('https://chatgpt.com/**', route)
            backend = BrowserSubchatBackend(context, http_read=True)
            if interrupted:
                with pytest.raises(SubchatInterrupted):
                    await backend.read_answer(submission)
            else:
                assert (await backend.read_answer(submission)).text == '日本語 result'
            assert calls == [('GET', 'https://chatgpt.com/c/' + submission.conversation_id),
                             ('GET', 'https://chatgpt.com' + path)]
            assert context.pages == [original] and original.url == 'about:blank'
        finally:
            await browser.close()


async def test_interrupted_history_does_not_complete_or_release_queue(tmp_path):
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_state import SubchatList, SubchatSubmissions

    submission, payload = sample()
    payload['messages'][1]['metadata']['finish_details'] = {'type': 'interrupted'}

    class Reader:
        async def read_answer(self, saved):
            return project_history(json.dumps(payload).encode(), saved)

        async def prepare(self, saved):
            raise AssertionError('Interrupted target must not release a queued send')

    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        store.prepare(submission.operation_id, submission.prompt, submission.model,
                      submission.effort, owner=None)
        store.begin_send(submission.operation_id, owner=None)
        store.submitted(submission.operation_id, submission.conversation_id,
                        submission.user_message_id, owner=None)
        service = Subchats(store, Reader())
        service.queue('b' * 32, submission.operation_id, 'follow-up', owner=None)
        with pytest.raises(SubchatInterrupted):
            await service.recover('b' * 32, owner=None)
        assert store.get(submission.operation_id, owner=None).state == 'interrupted'
        assert store.get('b' * 32, owner=None).state == 'queued'
    finally:
        ledger.close()


    class UnavailableReader:
        async def read_answer(self, saved):
            raise AssertionError('Saved interruption must not reopen a browser or poll')

    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        service = Subchats(store, UnavailableReader())
        saved = store.get(submission.operation_id, owner=None)
        assert saved.state == 'interrupted' and saved.answer is None
        assert store.list(SubchatList(), owner=None).submissions[-1].state == 'interrupted'
        with pytest.raises(SubchatInterrupted):
            await service.recover(submission.operation_id, owner=None)
        with pytest.raises(SubchatInterrupted):
            await service.recover('b' * 32, owner=None)
        duplicate = await service.send(submission.operation_id, submission.prompt,
                                       submission.model, submission.effort, owner=None)
        assert duplicate == saved
        assert store.get('b' * 32, owner=None).state == 'queued'
    finally:
        ledger.close()


@pytest.mark.parametrize('failure, code', [(SubchatInterrupted, 'reply_interrupted'),
    (SubchatBrowserClosed, 'browser_closed'),
    (SubchatAccountMismatch, 'account_mismatch'),
    (lambda _: SubchatAccessError(401), 'authentication_required'),
    (lambda _: SubchatAccessError(403), 'access_denied')])
async def test_interruption_is_distinct_in_cli_and_mcp_without_provider_details(
        tmp_path, failure, code):
    from io import StringIO

    from anywhere_computer.models import Request
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_cli import process_lines
    from anywhere_computer.subchat_mcp import session
    from anywhere_computer.subchat_state import SubchatSubmissions

    class Reader:
        async def read_answer(self, saved):
            raise failure('private provider details')

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    op = 'e' * 32
    store.prepare(op, 'prompt', 'model', 'effort', owner=None)
    store.begin_send(op, owner=None)
    store.submitted(op, 'chat', 'user', owner=None)
    service = Subchats(store, Reader())
    server = session(service)
    try:
        output = StringIO()
        await process_lines(service, StringIO(json.dumps(
            {'action': 'recover', 'operation_id': op}) + '\n'), output)
        cli = json.loads(output.getvalue())
        assert cli['state'] == code and cli['automatic_retry'] is False
        result = await server.execute(Request(operation_id='f' * 32, tool='subchat_recover',
                                              arguments={'operation_id': op}))
        assert result.state == 'failed'
        assert result.data == {'error_code': code, 'automatic_retry': False}
        assert 'private provider details' not in output.getvalue() + result.model_dump_json()
        assert store.get(op, owner=None).state == (
            'interrupted' if code == 'reply_interrupted' else 'submitted')
        if code == 'reply_interrupted':
            waited = await server.execute(Request(operation_id='a' * 32, tool='subchat_wait',
                arguments={'operation_id': op, 'wait_ms': 1000}))
            assert waited.state == 'completed'
            assert waited.data['state'] == 'interrupted'
            assert waited.data['answer'] is None
    finally:
        await server.close()
        ledger.close()


@pytest.mark.parametrize('independent', [False, True])
@pytest.mark.parametrize('resource', ['history', 'catalog'])
@pytest.mark.parametrize('expired_status', [401, 403])
async def test_repeated_http_reads_auth_expiry_and_redirects(
        monkeypatch, expired_status, resource, independent):
    """Real APIRequestContext transport against a controlled local HTTP server."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from playwright.async_api import Error, async_playwright

    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    submission, payload = sample()
    status, calls, tab_gets = [200], [], []
    path = '/backend-api/conversations/' + submission.conversation_id
    if resource == 'catalog':
        from test_subchat_http_catalog import catalog

        payload = catalog()
        path = '/backend-api/models?observed=fixture'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization') == 'Bearer fixture'
                          and self.headers.get('oai-language') == 'ja'
                          and self.headers.get('chatgpt-account-id') == 'fixture-account'
                          and self.headers.get('unrelated-header') is None))
            if status[0] == 0:
                self.connection.close()
                return
            self.send_response(status[0])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Location', '/must-not-follow')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        async with async_playwright() as driver:
            try:
                browser = await driver.chromium.launch(channel='chrome', headless=True)
            except Error as error:
                if 'not found' in str(error) or "doesn't exist" in str(error):
                    pytest.skip('Chrome required')
                raise
            try:
                context = await browser.new_context()
                original = await context.new_page()

                async def route(r):
                    tab_gets.append(r.request.method)
                    if path in r.request.url:
                        await r.fulfill(content_type='application/json', body=json.dumps(payload))
                    else:
                        await r.fulfill(content_type='text/html', body='<script>fetch(' +
                            json.dumps(path) +
                            ',{headers:{Authorization:"Bearer fixture","oai-language":"ja",' +
                            '"chatgpt-account-id":"fixture-account",' +
                            '"unrelated-header":"do-not-copy"}})</script>')

                await context.route('https://chatgpt.com/**', route)
                request = (await driver.request.new_context() if independent else context.request)
                async def request_factory():
                    return request

                if independent:
                    async def forbidden_browser_request(*args, **kwargs):
                        raise AssertionError('Standalone read used browser request context')
                    monkeypatch.setattr(context.request, 'get', forbidden_browser_request)
                real_get = request.get

                async def local_get(url, **kwargs):
                    assert url in ('https://chatgpt.com' + path,
                                   'https://chatgpt.com/backend-api/conversations/other')
                    assert kwargs['max_redirects'] == kwargs['max_retries'] == 0
                    return await real_get(f'http://127.0.0.1:{server.server_port}' +
                                          url.removeprefix('https://chatgpt.com'), **kwargs)

                monkeypatch.setattr(request, 'get', local_get)
                backend = BrowserSubchatBackend(context, http_read=True,
                    http_request_factory=request_factory if independent else None)
                async def read():
                    return (await backend.http_catalog() if resource == 'catalog'
                            else await backend.read_answer(submission))

                first = await read()
                legacy_submission = submission
                if resource == 'history':
                    submission = submission.model_copy(update={
                        'provider_account_id': 'fixture-account'})
                    mismatch = submission.model_copy(update={'provider_account_id': 'other'})
                    with pytest.raises(ValueError, match='different Chat account'):
                        await backend.read_answer(mismatch)
                    assert calls == []  # Reject before any conversation HTTP request.
                for _ in range(2):
                    assert await read() == first
                assert tab_gets == ['GET', 'GET']  # Only the initial bootstrap navigated.
                assert calls == [(path, True)] * 2
                for failure in (302, 429, 500, 0):
                    status[0] = failure
                    before = len(calls)
                    with pytest.raises(Error if failure == 0 else ConnectionError):
                        await read()
                    assert len(calls) == before + 1  # No redirects or automatic retries.
                    assert len(tab_gets) == 2
                status[0] = 200
                assert await read() == first
                assert len(tab_gets) == 2  # Transport recovery stays HTTP-only.
                status[0] = expired_status
                with pytest.raises(SubchatAccessError) as rejected:
                    await read()
                assert rejected.value.code == ('authentication_required'
                    if expired_status == 401 else 'access_denied')
                assert len(tab_gets) == 2  # No automatic reauthentication/retry.
                before = len(calls)
                status[0] = 200
                for _ in range(3):
                    with pytest.raises(SubchatAccessError) as repeated:
                        await read()
                    assert repeated.value.code == rejected.value.code
                assert len(tab_gets) == 2 and len(calls) == before
                # A resource denial is not evidence that another resource is denied.
                async def forbidden_observation(page):
                    raise AssertionError('Cross-resource read opened a browser tab')

                other_url = 'https://chatgpt.com/backend-api/conversations/other'
                if expired_status == 403:
                    assert json.loads(await backend._http_reader._read(
                        context, other_url, forbidden_observation)) == payload
                    assert len(calls) == before + 1
                    with pytest.raises(SubchatAccessError):
                        await read()  # The denied resource remains latched.
                    assert len(calls) == before + 1
                else:
                    with pytest.raises(SubchatAccessError):
                        await backend._http_reader._read(
                            context, other_url, forbidden_observation)
                    assert len(calls) == before  # Authentication rejection is global.
                before = len(calls)
                # An explicitly restarted adapter can observe the repaired login.
                submission = legacy_submission
                backend = BrowserSubchatBackend(context, http_read=True,
                    http_request_factory=request_factory if independent else None)
                assert await read() == first
                assert len(tab_gets) == 4 and len(calls) == before
                assert await read() == first
                assert len(tab_gets) == 4 and len(calls) == before + 1
                assert all(ok and called_path in (path, '/backend-api/conversations/other')
                           for called_path, ok in calls)
                assert context.pages == [original] and original.url == 'about:blank'
                if independent:
                    await browser.close()
                    assert await read() == first
                    if resource == 'history':
                        receipt = await backend.find_submission(submission)
                        assert receipt.user_message_id == submission.user_message_id
                    status[0] = expired_status
                    with pytest.raises(SubchatAccessError):
                        await read()
                    before = len(calls)
                    status[0] = 200
                    with pytest.raises(SubchatAccessError):
                        await read()
                    assert len(calls) == before and len(tab_gets) == 4
            finally:
                if independent and 'request' in locals():
                    await request.dispose()
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('case', ['cancelled', 'other_request', 'not_terminal', 'unknown',
                                  'wrong_type', 'missing_binding'])
def test_cancelled_thinking_without_final_or_local_stop_receipt(case):
    submission, payload = sample()
    user, recap = payload['messages']
    recap['channel'] = None
    recap['content'] = {'content_type': 'reasoning_recap'}
    recap['metadata'].pop('finish_details')
    recap['metadata'].pop('is_complete')
    recap['metadata']['reasoning_status'] = 'reasoning_cancelled'
    if case == 'other_request':
        recap['metadata']['request_id'] = 'other'
    elif case == 'not_terminal':
        recap['end_turn'] = False
    elif case == 'unknown':
        recap['metadata']['reasoning_status'] = 'future_status'
    elif case == 'wrong_type':
        recap['content']['content_type'] = 'thoughts'
    elif case == 'missing_binding':
        del user['metadata']['request_id']
    if case == 'cancelled':
        with pytest.raises(SubchatInterrupted):
            project_history(json.dumps(payload).encode(), submission)
    else:
        assert project_history(json.dumps(payload).encode(), submission) is None


@pytest.mark.parametrize('close_browser', [False, True])
@pytest.mark.parametrize('confirmed', [False, True])
async def test_user_close_preserves_submission_without_relaunch(tmp_path, close_browser, confirmed):
    from playwright.async_api import async_playwright

    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmissions

    saved, _ = sample()
    ledger = Ledger(tmp_path)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        context = await browser.new_context()
        launches = 0

        async def factory():
            nonlocal launches
            launches += 1
            return context

        backend = BrowserSubchatBackend(factory, http_read=True)
        store = SubchatSubmissions(ledger.connection)
        store.prepare(saved.operation_id, saved.prompt, saved.model, saved.effort,
                      owner=None, conversation_id=saved.conversation_id if confirmed else None)
        store.begin_send(saved.operation_id, owner=None)
        if confirmed:
            store.submitted(saved.operation_id, saved.conversation_id, saved.user_message_id,
                            owner=None)
        before = store.get(saved.operation_id, owner=None)
        try:
            await backend._browser()
            if close_browser:
                await browser.close()
            else:
                await context.close()
            service = Subchats(store, backend)
            for _ in range(2):
                with pytest.raises(SubchatBrowserClosed):
                    await service.recover(saved.operation_id, owner=None)
            assert launches == 1
            assert store.get(saved.operation_id, owner=None) == before
        finally:
            await browser.close()
            ledger.close()


async def test_unknown_conversation_recovery_does_not_launch_browser():
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    saved, _ = sample()
    unknown = saved.model_copy(update={
        'state': 'sending', 'conversation_id': None, 'user_message_id': None})

    async def forbidden_factory():
        raise AssertionError('Unknown conversation recovery must not launch Chrome')

    backend = BrowserSubchatBackend(forbidden_factory, http_read=True)
    assert await backend.find_submission(unknown) is None


@pytest.mark.parametrize('status', [401, 403])
async def test_bootstrap_access_rejection_does_not_reopen_tabs(status):
    from anywhere_computer.subchat_browser.http_reader import ChatHTTPReader

    class Page:
        closed = False

        async def close(self):
            self.closed = True

    class Context:
        def __init__(self):
            self.pages = []

        async def new_page(self):
            page = Page()
            self.pages.append(page)
            return page

    observations = []

    async def observe(page):
        observations.append(page)
        raise SubchatAccessError(status)

    reader, context = ChatHTTPReader(), Context()
    for _ in range(3):
        with pytest.raises(SubchatAccessError) as rejected:
            await reader._read(context, None, observe)
        assert rejected.value.status == status
    assert len(observations) == len(context.pages) == 1
    assert context.pages[0].closed
    # Explicitly replacing the connection permits a fresh login observation.
    replacement = Context()
    with pytest.raises(SubchatAccessError):
        await reader._read(replacement, None, observe)
    assert len(observations) == 2 and replacement.pages[0].closed


@pytest.mark.parametrize("new_chat", [False, True])
async def test_checkpointed_request_recovers_without_rendered_history(tmp_path, new_chat):
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_browser.history import project_receipt
    from anywhere_computer.subchat_state import SubchatSubmissions

    submission, payload = sample()
    payload["messages"][1]["metadata"].update(
        model_slug="future-model", thinking_effort="future-effort")
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    op = submission.operation_id
    store.prepare(op, submission.prompt, submission.model, submission.effort, owner=None,
                  conversation_id=None if new_chat else submission.conversation_id)
    store.begin_send(op, owner=None)
    assert store.observe_request(op, 'user', owner=None).state == 'sending'
    with pytest.raises(ValueError, match='identity changed'):
        store.observe_request(op, 'different', owner=None)
    ledger.close()
    ledger = Ledger(tmp_path)

    class Context:
        browser = None

        def on(self, *_):
            pass

        async def new_page(self):
            raise AssertionError('Receipt recovery must not create a page')

    class Reader:
        def can_read_without_browser(self, context, *, catalog=False):
            return False

        async def receipt(self, context, saved):
            assert saved.user_message_id == 'user'
            return project_receipt(json.dumps(payload).encode(), saved)

        async def history(self, context, saved):
            return project_history(json.dumps(payload).encode(), saved)

    try:
        backend = BrowserSubchatBackend(Context(), http_read=True)
        backend._http_reader = Reader()
        if new_chat:
            class Page:
                url = 'https://chatgpt.com/c/' + submission.conversation_id

                def is_closed(self):
                    return False

                async def evaluate(self, *_):
                    raise AssertionError('Checkpointed input must not use rendered history')

            backend.pages[op] = Page()
        service = Subchats(SubchatSubmissions(ledger.connection), backend)
        result = await service.recover(op, owner=None)
        assert result.state == 'completed' and result.answer == '日本語 result'
        assert result.reported_settings.model_slug == 'future-model'
        assert result.model == submission.model and result.effort == submission.effort
        ledger.close()
        ledger = Ledger(tmp_path)
        restored = SubchatSubmissions(ledger.connection)
        assert restored.get(op, owner=None).reported_settings == result.reported_settings
        with pytest.raises(ValueError, match='cannot be replaced'):
            restored.complete(op, result.answer_message_id, result.answer, owner=None)
    finally:
        ledger.close()

@pytest.mark.parametrize('metadata, expected', [
    ({'model_slug': 'future-model', 'thinking_effort': 'future-effort'},
     {'model_slug': 'future-model', 'thinking_effort': 'future-effort'}),
    ({'model_slug': 'future-model', 'thinking_effort': {'unexpected': True}},
     {'model_slug': 'future-model', 'thinking_effort': None}),
    ({'model_slug': 'x' * 257, 'thinking_effort': ' '}, None),
    ({'authorization': 'must-not-be-exported'}, None),
])
def test_reported_settings_are_optional_bounded_and_not_ui_mapping(metadata, expected):
    submission, payload = sample()
    payload['messages'][1]['metadata'].update(metadata)
    answer = project_history(json.dumps(payload).encode(), submission)
    assert answer is not None
    assert (answer.reported_settings.model_dump() if answer.reported_settings else None) == expected
    assert 'must-not-be-exported' not in answer.model_dump_json()


@pytest.mark.parametrize(('saved', 'accepted'), [
    ('Review [https://example.com/pr/1](https://example.com/pr/1)', True),
    ('Review https://example.com/pr/1', True),
    ('Review [https://example.com/pr/1](https://other.example/pr/1)', False),
    ('Review [different](https://example.com/pr/1)', False),
    ('Review [https://example.com/pr/1](https://example.com/pr/1) extra', False),
])
def test_history_url_decoration_preserves_prompt_identity(saved, accepted):
    from anywhere_computer.subchat_browser.history import project_receipt

    submission, payload = sample()
    submission = submission.model_copy(update={'prompt': 'Review https://example.com/pr/1'})
    payload['messages'][0]['content']['parts'] = [saved]
    encoded = json.dumps(payload).encode()
    if accepted:
        assert project_receipt(encoded, submission).user_message_id == submission.user_message_id
        assert project_history(encoded, submission).text == '日本語 result'
    else:
        for project in (project_receipt, project_history):
            with pytest.raises(ValueError, match='Saved prompt does not match'):
                project(encoded, submission)


def test_request_account_binding_is_atomic_immutable_and_survives_restart(tmp_path):
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat_state import SubchatSubmissions

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    op = 'd' * 32
    store.prepare(op, 'bound input', 'model', 'effort', owner='owner')
    store.begin_send(op, owner='owner')
    ledger.connection.execute(
        "CREATE TRIGGER refuse_binding BEFORE INSERT ON subchat_account_bindings "
        "BEGIN SELECT RAISE(ABORT, 'fixture binding failure'); END")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match='fixture binding failure'):
        store.observe_request(op, 'input', owner='owner', provider_account_id='account-a')
    assert store.get(op, owner='owner').user_message_id is None
    ledger.connection.execute('DROP TRIGGER refuse_binding')
    bound = store.observe_request(op, 'input', owner='owner', provider_account_id='account-a')
    assert bound.provider_account_id == 'account-a'
    for account in ('account-b', None):
        with pytest.raises(ValueError, match='identity changed'):
            store.observe_request(op, 'input', owner='owner', provider_account_id=account)
    with pytest.raises(ValueError, match='Unknown'):
        store.observe_request(op, 'input', owner='other', provider_account_id='account-a')
    body = ledger.connection.execute(
        'SELECT body FROM subchat_submissions WHERE operation_id=?', (op,)).fetchone()[0]
    assert 'provider_account_id' not in body  # Legacy JSON shape remains readable.
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        assert store.get(op, owner='owner') == bound
        assert store.observe_request(op, 'input', owner='owner',
                                     provider_account_id='account-a') == bound
    finally:
        ledger.close()


@pytest.mark.parametrize('bootstrap', [False, True])
async def test_bound_account_mismatch_does_not_request_conversation(monkeypatch, bootstrap):
    from anywhere_computer.subchat_browser.http_reader import ChatHTTPReader

    saved, _ = sample()
    saved = saved.model_copy(update={'provider_account_id': 'account-a'})
    context = object()

    async def forbidden_client():
        raise AssertionError('Mismatched account reached HTTP transport')

    reader = ChatHTTPReader(forbidden_client)
    observations = []

    async def catalog(selected_context):
        assert selected_context is context
        observations.append('catalog')
        reader._context = context
        reader._headers = {'authorization': 'fixture', 'chatgpt-account-id': 'account-b'}
        return {}

    monkeypatch.setattr(reader, 'catalog', catalog)
    if not bootstrap:
        await catalog(context)
        observations.clear()
    with pytest.raises(ValueError, match='different Chat account'):
        await reader.history(context, saved)
    assert observations == (['catalog'] if bootstrap else [])


def test_queued_request_cannot_change_parent_account_after_restart(tmp_path):
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat_state import SubchatSubmissions

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = 'e' * 32, 'f' * 32
    store.prepare(parent, 'parent', 'model', 'effort', owner=None)
    store.begin_send(parent, owner=None)
    store.observe_request(parent, 'parent-input', owner=None, provider_account_id='account-a')
    store.submitted(parent, 'conversation', 'parent-input', owner=None)
    store.complete(parent, 'parent-answer', 'done', owner=None)
    store.prepare(child, 'child', 'model', 'effort', owner=None,
                  conversation_id='conversation', after_operation_id=parent)
    store.begin_send(child, owner=None)
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        for account in ('account-b', None):
            with pytest.raises(ValueError, match='account'):
                store.observe_request(child, 'child-input', owner=None,
                                      provider_account_id=account)
        assert store.get(child, owner=None).user_message_id is None
        assert store.observe_request(child, 'child-input', owner=None,
            provider_account_id='account-a').provider_account_id == 'account-a'
    finally:
        ledger.close()


@pytest.mark.parametrize('entry', ['cli', 'mcp', 'wait'])
@pytest.mark.parametrize('queued', [False, True])
async def test_recovery_reports_request_scoped_http_observation(tmp_path, entry, queued):
    """A successful history read with no matching input must not look like Thinking."""
    from anywhere_computer.models import Request
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_browser.http_reader import ChatHTTPReader
    from anywhere_computer.subchat_cli import Command, dispatch
    from anywhere_computer.subchat_mcp import session
    from anywhere_computer.subchat_state import SubchatSubmissions

    submission, payload = sample()
    payload['messages'] = []
    reader = ChatHTTPReader()
    class Backend:
        async def read_answer(self, saved):
            async def history_payload(context, target):
                assert target.operation_id == submission.operation_id
                return json.dumps(payload).encode()
            reader._history_payload = history_payload
            return await reader.history(None, saved)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent = submission.operation_id
    store.prepare(parent, submission.prompt, submission.model, submission.effort, owner=None)
    store.begin_send(parent, owner=None)
    store.submitted(parent, submission.conversation_id, submission.user_message_id, owner=None)
    service = Subchats(store, Backend())
    operation = 'b' * 32 if queued else parent
    if queued:
        service.queue(operation, parent, 'follow-up', owner=None)
    try:
        if entry == 'cli':
            result = json.loads(await dispatch(service, Command(
                action='recover', operation_id=operation)))
        else:
            server = session(service)
            try:
                reply = await server.execute(Request(operation_id='c' * 32,
                    tool='subchat_wait' if entry == 'wait' else 'subchat_recover',
                    arguments={'operation_id': operation, **({'wait_ms': 50}
                        if entry == 'wait' else {})}))
                assert reply.state == 'completed'
                result = reply.data
            finally:
                await server.close()
        assert result['state'] == ('queued' if queued else 'submitted')
        assert result['observation']['operation_id'] == parent
        assert result['observation']['reason'] == 'input_not_observed'
        assert result['observation']['source'] == 'http_history'
        assert result['observation']['observed_at']
        assert 'observation' not in store.get(operation, owner=None).model_dump()
        assert 'observation' not in ledger.connection.execute(
            'SELECT body FROM subchat_submissions WHERE operation_id=?', (operation,)).fetchone()[0]
    finally:
        ledger.close()
