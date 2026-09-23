"""Chrome login bootstraps HTTPX without persisting bearer or Cookie values."""
import logging

import httpx
import pytest
from test_subchat_http_catalog import catalog
from test_subchat_http_generation import handoff_data

from anywhere_computer.subchat import SubchatAccessError
from anywhere_computer.subchat_chrome_login import (
    CATALOG_URL,
    _import_chrome_cookies,
    chrome_generation_cookie,
    chrome_http_session,
)
from anywhere_computer.subchat_http_generation import ObservedHTTPGeneration
from anywhere_computer.subchat_state import SubchatAccountMismatch


class Page:
    async def evaluate(self, _expression):
        return 'Chrome test user agent'

    async def close(self):
        pass


class Context:
    async def cookies(self):
        return [{'domain': '.chatgpt.com', 'path': '/', 'expires': -1,
                 'name': 'session', 'value': 'private-cookie'},
                {'domain': 'other.example', 'name': 'other', 'value': 'excluded'}]

    async def new_page(self):
        return Page()


async def test_chrome_login_gets_token_then_validates_catalog(caplog):
    seen = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers['cookie'] == 'session=private-cookie'
        assert request.headers['user-agent'] == 'Chrome test user agent'
        if request.url.path == '/api/auth/session':
            assert request.headers['sec-fetch-site'] == 'same-origin'
            return httpx.Response(200, json={'accessToken': 'private-token',
                                             'account': {'id': 'account'},
                                             'user': {'email': 'owner@example.com'}})
        assert str(request.url) == CATALOG_URL
        assert request.headers['authorization'] == 'Bearer private-token'
        assert request.headers['chatgpt-account-id'] == 'account'
        return httpx.Response(200, json=catalog())

    with caplog.at_level(logging.INFO, logger='anywhere_computer.subchat_chrome_login'):
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            session = await chrome_http_session(Context(), client, expected_account_id='account')
    assert len(seen) == 2
    assert session.catalog_url == CATALOG_URL
    assert 'cookie' not in session.headers()
    assert session.user_email == 'owner@example.com'
    assert 'private-token' not in repr(session)
    assert 'private-cookie' not in repr(session)
    assert 'Subchat HTTP auth GET status=200 elapsed_ms=' in caplog.text
    assert 'Subchat HTTP catalog GET status=200 elapsed_ms=' in caplog.text
    for private in ('private-token', 'private-cookie', 'owner@example.com', 'account'):
        assert private not in caplog.text


async def test_chrome_cookie_jar_rotates_from_auth_response():
    seen = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get('cookie'))
        if request.url.path == '/api/auth/session':
            return httpx.Response(200, headers={
                'set-cookie': 'session=rotated; Domain=.chatgpt.com; Path=/; Secure'},
                json={'accessToken': 'private-token', 'account': {'id': 'account'},
                      'user': {'email': 'owner@example.com'}})
        return httpx.Response(200, json=catalog())

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        await chrome_http_session(Context(), client, expected_account_id='account')
        assert chrome_generation_cookie(client) == 'session=rotated'
    assert seen == ['session=private-cookie', 'session=rotated']


async def test_chrome_cookie_scope_and_expiry():
    sent = []

    def serve(request):
        sent.append(request.url.host)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        _import_chrome_cookies(client, [
            {'domain': '.chatgpt.com', 'path': '/', 'expires': -1,
             'name': 'base', 'value': 'one'},
            {'domain': 'chatgpt.com', 'path': '/backend-api', 'expires': -1,
             'name': 'api', 'value': 'two'},
            {'domain': 'chatgpt.com', 'path': '/backend-api', 'expires': 1,
             'name': 'expired', 'value': 'three'},
            {'domain': 'other.example', 'path': '/', 'expires': -1,
             'name': 'foreign', 'value': 'four'},
            {'domain': '.chatgpt.com', 'path': '/', 'expires': -1,
             'name': 'partitioned', 'value': 'five', 'partitionKey': 'https://example.org'},
        ])
        auth = client.build_request('GET', 'https://chatgpt.com/api/auth/session')
        generation = client.build_request('POST',
            'https://chatgpt.com/backend-api/f/conversation')
        foreign = client.build_request('GET', 'https://other.example/backend-api')
        assert auth.headers['cookie'] == 'base=one'
        assert generation.headers['cookie'] == 'api=two; base=one'
        assert 'foreign' not in foreign.headers.get('cookie', '')
        assert 'partitioned' not in generation.headers['cookie']
        with pytest.raises(ValueError, match='exact ChatGPT origin'):
            await client.get('https://sub.chatgpt.com/backend-api/test')
        with pytest.raises(ValueError, match='exact ChatGPT origin'):
            await client.get('https://chatgpt.com:8443/backend-api/test')
        assert sent == []


async def test_chrome_login_stops_on_denied_get(caplog):
    paths = []

    def deny(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(403, json={'error': 'denied'})

    with caplog.at_level(logging.INFO, logger='anywhere_computer.subchat_chrome_login'):
        async with httpx.AsyncClient(transport=httpx.MockTransport(deny)) as client:
            with pytest.raises(SubchatAccessError):
                await chrome_http_session(Context(), client)
    assert paths == ['/api/auth/session']
    assert 'Subchat HTTP auth GET status=403 elapsed_ms=' in caplog.text
    assert 'private-cookie' not in caplog.text


@pytest.mark.parametrize(('resource', 'limit', 'error'), [
    ('auth', 131_072, 'Chrome login response is too large'),
    ('catalog', 1_048_576, 'Model catalog is too large'),
])
async def test_chrome_login_stops_stream_at_response_limit(resource, limit, error):
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
    paths = []

    def serve(request):
        paths.append(request.url.path)
        if request.url.path == '/api/auth/session' and resource == 'catalog':
            return httpx.Response(200, json={
                'accessToken': 'private-token', 'account': {'id': 'account'},
                'user': {'email': 'owner@example.com'}})
        return httpx.Response(200, stream=stream,
                              headers={'content-type': 'application/json'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(ValueError, match=error):
            await chrome_http_session(Context(), client)
    assert paths == (['/api/auth/session'] if resource == 'auth' else
                     ['/api/auth/session', '/backend-api/models'])
    assert stream.chunks == limit // 65_536 + 1
    assert stream.closed


async def test_chrome_login_rejects_wrong_account_before_catalog_get():
    paths = []

    def serve(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == '/api/auth/session'
        return httpx.Response(200, json={'accessToken': 'private-token',
                                         'account': {'id': 'another-account'},
                                         'user': {'email': 'other@example.com'}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(SubchatAccountMismatch, match='another Chat account'):
            await chrome_http_session(Context(), client, expected_account_id='account')
    assert paths == ['/api/auth/session']


def test_generation_handoff_rejects_a_different_chrome_cookie():
    data = handoff_data()
    with pytest.raises(ValueError, match='session or origin changed'):
        ObservedHTTPGeneration.from_data(data, authorization=data['headers']['authorization'],
                                         account_id=data['headers']['chatgpt-account-id'],
                                         cookie='another-profile-cookie')
