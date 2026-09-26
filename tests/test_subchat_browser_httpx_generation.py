"""HTTPX transport for one browser-prepared generation request."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from anywhere_computer.subchat_browser.httpx_generation import (
    HTTPXGenerationPreflightError,
    post_browser_prepared_once,
)
from anywhere_computer.subchat_state import SubchatSubmission


async def test_pinned_account_mismatch_stops_before_browser_generation(
    monkeypatch,
):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatAccountMismatch, SubchatHTTPSelection

    class Client:
        async def aclose(self):
            pass

    class Page:
        def is_closed(self):
            return False

        async def route(self, *_args):
            pytest.fail('A mismatched account must not install a send route')

    class Context:
        browser = None

        def on(self, _event, _callback):
            pass

    async def browser():
        return Context()

    async def verify(_context, _client, *, expected_account_id, page_factory):
        assert expected_account_id == 'account-a'
        raise SubchatAccountMismatch('Different Chat account')

    backend = BrowserSubchatBackend(
        browser, http_read=True, httpx_generation=True,
        expected_account_id='account-a')
    backend.pages['e' * 32] = Page()
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', lambda **_kwargs: Client())
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', verify)
    submission = SubchatSubmission(
        operation_id='e' * 32, prompt='fixture', model='fixture', effort='fixture',
        state='sending',
        http_selection=SubchatHTTPSelection(
            version_id='fixture', preset_id=1, model_slug='fixture',
            thinking_effort=None))
    with pytest.raises(SubchatAccountMismatch, match='Different Chat account'):
        await backend.send(submission)


class RequestFixture:
    method = 'POST'
    url = 'https://chatgpt.com/backend-api/f/conversation'
    post_data = '{}'

    async def header_value(self, name):
        return None

    async def all_headers(self):
        return {'authorization': 'Bearer fixture', 'cookie': 'session=fixture',
                'origin': 'https://chatgpt.com', 'content-type': 'application/json',
                'content-length': '999', 'host': 'chatgpt.com',
                'chatgpt-account-id': None,
                'openai-sentinel-proof-token': 'proof-fixture'}


async def test_browser_prepared_generation_uses_httpx_once():
    observed = []

    def respond(request):
        observed.append(request)
        assert request.extensions['timeout']['read'] is None
        assert request.extensions['timeout']['connect'] == 10.0
        assert request.content == b'{"action":"next"}'
        assert request.headers['authorization'] == 'Bearer fixture'
        assert request.headers['cookie'] == 'session=fixture'
        assert request.headers['openai-sentinel-proof-token'] == 'proof-fixture'
        assert 'chatgpt-account-id' not in request.headers
        assert request.headers['content-length'] == str(len(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content=b'data: [DONE]\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await post_browser_prepared_once(RequestFixture(), client,
            authorization='Bearer fixture', content=b'{"action":"next"}')
    assert len(observed) == 1
    assert result.status == 200 and result.body == b'data: [DONE]\n\n'


async def test_httpx_reports_headers_before_a_silent_sse_body():
    release_body = asyncio.Event()
    headers_seen = asyncio.Event()
    chunks = []

    class DelayedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release_body.wait()
            yield b'data: [DONE]\n\n'

    def respond(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=DelayedBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(post_browser_prepared_once(
            RequestFixture(), client, authorization='Bearer fixture', content=b'{}',
            on_headers=lambda metadata: headers_seen.set(), on_chunk=chunks.append))
        await asyncio.wait_for(headers_seen.wait(), 1)
        assert not task.done() and chunks == []
        release_body.set()
        result = await task
    assert result.body == b'data: [DONE]\n\n' and chunks == [result.body]


async def test_httpx_times_out_waiting_for_headers_without_reposting():
    requests = []
    waiting = asyncio.Event()

    class StalledHeaders(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append(request)
            await waiting.wait()
            raise AssertionError('Headers should time out first')

    async with httpx.AsyncClient(transport=StalledHeaders()) as client:
        with pytest.raises(TimeoutError):
            await post_browser_prepared_once(
                RequestFixture(), client, authorization='Bearer fixture', content=b'{}',
                header_timeout=0.01)
    assert len(requests) == 1


async def test_browser_prepared_generation_rejects_changed_account_without_post():
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPXGenerationPreflightError, match='account or origin changed'):
            await post_browser_prepared_once(RequestFixture(), client,
                authorization='Bearer other', content=b'{}')
    assert not sent


@pytest.mark.parametrize('preflight', [True, False])
async def test_httpx_errors_keep_preflight_distinct_from_unknown_post(monkeypatch, preflight):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatHTTPSelection

    class Client:
        async def aclose(self):
            pass

    class Page:
        routed = None

        def is_closed(self):
            return False

        async def route(self, pattern, callback):
            self.routed = callback

        async def unroute(self, pattern, callback):
            pass

    class Route:
        request = RequestFixture()
        aborted = False

        async def abort(self):
            self.aborted = True

    async def browser():
        return object()

    async def session(*args, **kwargs):
        return SimpleNamespace(account_id='account-a',
                               authorization=SimpleNamespace(
                                   get_secret_value=lambda: 'Bearer fixture'))

    async def reject_before_post(*args, **kwargs):
        if preflight:
            raise HTTPXGenerationPreflightError('Chat generation account or origin changed')
        raise ValueError('Response exceeded memory limit after POST')

    page = Page()
    route = Route()
    failures = []
    backend = BrowserSubchatBackend(
        browser, http_read=True, httpx_generation=True,
        record_preflight_failure=failures.append)
    backend.pages['e' * 32] = page

    async def send(submission):
        await page.routed(route)
        return None

    monkeypatch.setattr(backend, '_send', send)
    monkeypatch.setattr(backend, '_browser', browser)
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', lambda **kwargs: Client())
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    monkeypatch.setattr(backend_module, 'generation_input', lambda *args: {
        'messages': [{'id': 'message-a'}]})
    monkeypatch.setattr(
        __import__('anywhere_computer.subchat_browser.httpx_generation', fromlist=['x']),
        'post_browser_prepared_once', reject_before_post)
    submission = SubchatSubmission(
        operation_id='e' * 32, prompt='fixture', model='fixture', effort='fixture',
        state='sending',
        http_selection=SubchatHTTPSelection(
            version_id='fixture', preset_id=1, model_slug='fixture',
            thinking_effort=None))
    with pytest.raises(ValueError, match='not confirmed'):
        await backend.send(submission)
    assert route.aborted
    assert failures == ([submission.operation_id] if preflight else [])
    assert (page in backend._unreusable_pages) is preflight


async def test_browser_prepared_generation_discards_rejection_body():
    def respond(request):
        return httpx.Response(403, headers={'content-type': 'text/html',
                                            'cf-mitigated': 'challenge'},
                              content=b'private-response-marker')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await post_browser_prepared_once(RequestFixture(), client,
            authorization='Bearer fixture', content=b'{}')
    assert result.status == 403 and result.body == b''
    assert result.cf_mitigated is True


def test_buffered_sse_candidate_requires_root_identity():
    from anywhere_computer.subchat_browser.backend import _stream_conversation_id

    body = (b'data: {"text":{"conversation_id":'
            b'"22222222-2222-3333-4444-555555555555"}}\n\n'
            b'data: {"p":"","o":"add","v":{"conversation_id":'
            b'"11111111-2222-3333-4444-555555555555"}}\n\n')
    assert _stream_conversation_id(body) == '11111111-2222-3333-4444-555555555555'


async def test_slow_httpx_stream_keeps_client_and_route_until_delivery(monkeypatch):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_browser.httpx_generation import HTTPXGenerationResponse

    class Client:
        def __init__(self, **kwargs):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class Page:
        def __init__(self):
            self.routed = None
            self.fulfilled = False
            self.unrouted = False

        def is_closed(self):
            return False

        async def route(self, pattern, callback):
            self.routed = callback

        async def unroute(self, pattern, callback):
            assert self.fulfilled
            self.unrouted = True

        async def close(self):
            raise AssertionError('Started HTTPX stream must not close its page')

        async def expose_binding(self, *args):
            pass

        async def evaluate(self, *args):
            pass

    class Route:
        request = RequestFixture()

        async def fulfill(self, **kwargs):
            page.fulfilled = True

        async def abort(self):
            raise AssertionError('Slow valid generation must not be aborted')

    page = Page()
    context = object()
    clients = []
    candidates = []

    def make_client(**kwargs):
        client = Client(**kwargs)
        clients.append(client)
        return client

    async def session(*args, **kwargs):
        return SimpleNamespace(account_id='account-a',
                               authorization=SimpleNamespace(
                                   get_secret_value=lambda: 'Bearer fixture'))

    async def slow_post(request, client, **kwargs):
        await asyncio.sleep(10.1)
        assert not client.closed
        return HTTPXGenerationResponse(200, 'text/event-stream',
                                       b'data: {"conversation_id":'
                                       b'"11111111-2222-3333-4444-555555555555"}\n\n',
                                       'HTTP/1.1', False)

    async def context_factory():
        return context

    backend = BrowserSubchatBackend(context_factory, http_read=True,
                                    httpx_generation=True,
                                    record_request=lambda *args: None,
                                    record_conversation=lambda *args: candidates.append(args))
    backend.pages['b' * 32] = page

    async def browser():
        return context

    async def send(submission):
        assert page.routed is not None
        async def late_request():
            await asyncio.sleep(10.1)
            assert not page.unrouted and not clients[0].closed
            await page.routed(Route())

        task = asyncio.create_task(late_request())
        task.add_done_callback(lambda finished: finished.result())
        return None

    monkeypatch.setattr(backend, '_browser', browser)
    monkeypatch.setattr(backend, '_send', send)
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', make_client)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    monkeypatch.setattr(backend_module, 'generation_input', lambda *args: {
        'messages': [{'id': 'message-a'}]})
    monkeypatch.setattr(
        __import__('anywhere_computer.subchat_browser.httpx_generation', fromlist=['x']),
        'post_browser_prepared_once', slow_post)
    submission = SubchatSubmission(
        operation_id='b' * 32, prompt='fixture', model='fixture', effort='fixture',
        state='sending')
    assert await backend.send(submission) is None
    while backend.has_live_generation():
        await asyncio.sleep(0)
    assert page.fulfilled and page.unrouted and len(clients) == 1 and clients[0].closed
    assert candidates == [(submission.operation_id, 'message-a',
                           '11111111-2222-3333-4444-555555555555', 'account-a')]


async def test_httpx_headers_release_send_while_stream_and_identity_continue(monkeypatch):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_browser.httpx_generation import HTTPXGenerationResponse

    finish = asyncio.Event()
    frame_seen = asyncio.Event()
    callbacks = []
    candidates = []
    status_events = []

    class Store:
        def record_http_event(self, operation_id, stage, *, owner, status):
            status_events.append((operation_id, stage, owner, status))

    class Client:
        closed = False

        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            self.closed = True

    client = Client()

    class Page:
        callback = None
        unrouted = False
        fulfilled = False

        def is_closed(self):
            return False

        async def route(self, pattern, callback):
            self.callback = callback

        async def unroute(self, pattern, callback):
            self.unrouted = True

        async def expose_binding(self, *args):
            pass

        async def evaluate(self, *args):
            pass

    class Route:
        request = RequestFixture()

        async def fulfill(self, **kwargs):
            page.fulfilled = True

        async def abort(self):
            raise AssertionError('Valid stream must not abort')

    page = Page()

    async def browser():
        return object()

    async def session(*args, **kwargs):
        return SimpleNamespace(account_id='account-a',
                               authorization=SimpleNamespace(
                                   get_secret_value=lambda: 'Bearer fixture'))

    async def stream_post(request, supplied_client, **kwargs):
        assert supplied_client is client
        kwargs['on_headers'](HTTPXGenerationResponse(
            200, 'text/event-stream', b'', 'HTTP/1.1', False))
        kwargs['on_chunk'](b'data: {"p":"","o":"add","v":{"conversation_id":'
                           b'"11111111-2222-3333-4444-555555555555"}}\n\n')
        frame_seen.set()
        await finish.wait()
        return HTTPXGenerationResponse(200, 'text/event-stream', b'data: [DONE]\n\n',
                                       'HTTP/1.1', False)

    async def send(submission):
        callbacks.append(asyncio.create_task(page.callback(Route())))
        return None

    backend = BrowserSubchatBackend(browser, http_read=True, httpx_generation=True,
                                    record_request=lambda *args: None,
                                    record_conversation=lambda *args: candidates.append(args),
                                    store=Store(), owner='owner-a')
    backend.pages['d' * 32] = page
    monkeypatch.setattr(backend, '_browser', browser)
    monkeypatch.setattr(backend, '_send', send)
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    monkeypatch.setattr(backend_module, 'generation_input', lambda *args: {
        'messages': [{'id': 'message-a'}]})
    monkeypatch.setattr(
        __import__('anywhere_computer.subchat_browser.httpx_generation', fromlist=['x']),
        'post_browser_prepared_once', stream_post)
    submission = SubchatSubmission(operation_id='d' * 32, prompt='fixture', model='fixture',
                                   effort='fixture', state='sending')
    await asyncio.wait_for(backend.send(submission), 1)
    await frame_seen.wait()
    assert backend.has_live_generation() and not client.closed and not page.unrouted
    assert candidates == [(submission.operation_id, 'message-a',
                           '11111111-2222-3333-4444-555555555555', 'account-a')]
    assert status_events == [(submission.operation_id, 'generation_response', 'owner-a', 200)]
    finish.set()
    await asyncio.gather(*callbacks)
    while backend.has_live_generation():
        await asyncio.sleep(0)
    assert page.fulfilled and page.unrouted and client.closed
    assert not backend.has_live_generation()


async def test_completed_stream_aborts_route_and_retains_queue_without_reusing_tab(monkeypatch):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_browser.httpx_generation import HTTPXGenerationResponse

    first_half = asyncio.Event()
    continue_chunks = asyncio.Event()
    candidate_seen = asyncio.Event()
    never_finished = asyncio.Event()
    callbacks = []
    posts = []

    class Store:
        state = 'sending'

        def get(self, operation_id, *, owner):
            return SimpleNamespace(state=self.state)

    store = Store()

    class Client:
        closed = False

        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            self.closed = True

    client = Client()

    class Page:
        url = 'https://chatgpt.com/c/11111111-2222-3333-4444-555555555555'
        routed = None
        unrouted = False

        def is_closed(self):
            return False

        async def route(self, pattern, callback):
            self.routed = callback

        async def unroute(self, pattern, callback):
            self.unrouted = True

        async def expose_binding(self, *args):
            pass

        async def evaluate(self, *args):
            pass

    class Route:
        request = RequestFixture()
        aborted = False
        fulfilled = False

        async def abort(self):
            self.aborted = True

        async def fulfill(self, **kwargs):
            self.fulfilled = True

    page = Page()
    route = Route()

    async def browser():
        return object()

    async def session(*args, **kwargs):
        return SimpleNamespace(account_id='account-a',
                               authorization=SimpleNamespace(
                                   get_secret_value=lambda: 'Bearer fixture'))

    def record_conversation(*args):
        candidate_seen.set()
        if store.state == 'completed':
            raise ValueError('Completed submission does not accept candidates')

    async def stream_post(request, supplied_client, **kwargs):
        posts.append(request)
        kwargs['on_headers'](HTTPXGenerationResponse(
            200, 'text/event-stream', b'', 'HTTP/1.1', False))
        kwargs['on_chunk'](b'data: {"p":"","o":"add","v":{"conversation_id":"11111111-')
        first_half.set()
        await continue_chunks.wait()
        kwargs['on_chunk'](b'2222-3333-4444-555555555555"}}\n\n')
        await candidate_seen.wait()
        await never_finished.wait()
        raise AssertionError('Completed stream must be cancelled')

    async def send(submission):
        callbacks.append(asyncio.create_task(page.routed(route)))
        return None

    backend = BrowserSubchatBackend(browser, http_read=True, httpx_generation=True,
                                    record_request=lambda *args: None,
                                    record_conversation=record_conversation,
                                    store=store, owner='owner-a')
    operation_id = 'a' * 32
    backend.pages[operation_id] = page
    monkeypatch.setattr(backend, '_browser', browser)
    monkeypatch.setattr(backend, '_send', send)
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    monkeypatch.setattr(backend_module, 'generation_input', lambda *args: {
        'messages': [{'id': 'message-a'}]})
    monkeypatch.setattr(
        __import__('anywhere_computer.subchat_browser.httpx_generation', fromlist=['x']),
        'post_browser_prepared_once', stream_post)
    submission = SubchatSubmission(operation_id=operation_id, prompt='fixture', model='fixture',
                                   effort='fixture', state='sending')
    await asyncio.wait_for(backend.send(submission), 1)
    await first_half.wait()
    store.state = 'completed'
    # A candidate split across chunks arrives after final history committed.
    # The store rejects it, but that must not abort an otherwise accepted stream.
    continue_chunks.set()
    await asyncio.wait_for(candidate_seen.wait(), 1)
    assert not route.aborted and not callbacks[0].done()
    completed = submission.model_copy(update={
        'state': 'completed', 'conversation_id': '11111111-2222-3333-4444-555555555555'})
    await backend.release_completed(completed, keep_for_queue=True)
    await asyncio.gather(*callbacks, return_exceptions=True)
    assert route.aborted and not route.fulfilled
    assert page in backend._unreusable_pages and client.closed and page.unrouted
    backend._context = SimpleNamespace(browser=None)
    queued = submission.model_copy(update={
        'operation_id': 'b' * 32,
        'requested_conversation_id': completed.conversation_id})
    assert backend.queue_watch_ready(queued)
    assert len(posts) == 1 and not backend.has_live_generation()


@pytest.mark.parametrize('failure_stage', ['account_binding', 'observer_setup'])
async def test_httpx_client_closes_when_setup_fails(monkeypatch, failure_stage):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    class Client:
        closed = False

        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            self.closed = True

    class Page:
        def is_closed(self):
            return False

        async def expose_binding(self, *args):
            raise RuntimeError('observer setup failed')

    client = Client()
    context = object()

    async def browser():
        return context

    async def session(*args, **kwargs):
        return SimpleNamespace(account_id='account-a',
                               authorization=SimpleNamespace(
                                   get_secret_value=lambda: 'Bearer fixture'))

    backend = BrowserSubchatBackend(browser, http_read=True, httpx_generation=True,
                                    record_request=lambda *args: None,
                                    record_conversation=(lambda *args: None)
                                    if failure_stage == 'observer_setup' else None)
    backend.pages['c' * 32] = Page()
    monkeypatch.setattr(backend, '_browser', browser)
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    if failure_stage == 'account_binding':
        monkeypatch.setattr(backend._http_reader, 'bind_verified_account',
                            lambda *args: (_ for _ in ()).throw(RuntimeError('account failed')))
    submission = SubchatSubmission(
        operation_id='c' * 32, prompt='fixture', model='fixture', effort='fixture',
        state='sending')
    with pytest.raises(RuntimeError):
        await backend.send(submission)
    assert client.closed


async def test_invalid_browser_generation_marks_preflight_without_http_post(monkeypatch):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatHTTPSelection

    class Client:
        async def aclose(self):
            pass

    class Page:
        routed = None

        def is_closed(self):
            return False

        async def route(self, pattern, callback):
            self.routed = callback

        async def unroute(self, pattern, callback):
            pass

    class Route:
        request = RequestFixture()
        aborted = False

        async def abort(self):
            self.aborted = True

    async def session(*args, **kwargs):
        return SimpleNamespace(account_id='account-a',
                               authorization=SimpleNamespace(
                                   get_secret_value=lambda: 'Bearer fixture'))

    page = Page()
    route = Route()
    failures = []
    backend = BrowserSubchatBackend(
        lambda: None, http_read=True, httpx_generation=True,
        record_preflight_failure=failures.append)
    backend.pages['d' * 32] = page

    async def browser():
        return object()

    async def send(submission):
        assert page.routed is not None
        await page.routed(route)
        return None

    monkeypatch.setattr(backend, '_browser', browser)
    monkeypatch.setattr(backend, '_send', send)
    monkeypatch.setattr(backend_module.httpx, 'AsyncClient', lambda **kwargs: Client())
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    submission = SubchatSubmission(
        operation_id='d' * 32, prompt='fixture', model='fixture', effort='fixture',
        state='sending',
        http_selection=SubchatHTTPSelection(
            version_id='fixture', preset_id=1, model_slug='fixture',
            thinking_effort=None))
    with pytest.raises(ValueError, match='not confirmed'):
        await backend.send(submission)
    assert route.aborted and failures == [submission.operation_id]
    assert page in backend._unreusable_pages
