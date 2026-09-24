"""HTTPX transport for one browser-prepared generation request."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from anywhere_computer.subchat_browser.httpx_generation import post_browser_prepared_once
from anywhere_computer.subchat_state import SubchatSubmission


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


async def test_browser_prepared_generation_rejects_changed_account_without_post():
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match='account or origin changed'):
            await post_browser_prepared_once(RequestFixture(), client,
                authorization='Bearer other', content=b'{}')
    assert not sent


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

    class Route:
        request = RequestFixture()

        async def fulfill(self, **kwargs):
            page.fulfilled = True

        async def abort(self):
            raise AssertionError('Slow valid generation must not be aborted')

    page = Page()
    context = object()
    clients = []

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
        return HTTPXGenerationResponse(200, 'text/event-stream', b'data: [DONE]\n\n',
                                       'HTTP/1.1', False)

    async def context_factory():
        return context

    backend = BrowserSubchatBackend(context_factory, http_read=True,
                                    httpx_generation=True,
                                    record_request=lambda *args: None)
    backend.pages['b' * 32] = page

    async def browser():
        return context

    async def send(submission):
        assert page.routed is not None
        task = asyncio.create_task(page.routed(Route()))
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
    assert page.fulfilled and page.unrouted and len(clients) == 1 and clients[0].closed


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
