"""Chrome login bootstraps HTTPX without persisting bearer or Cookie values."""
import httpx
import pytest
from test_subchat_http_catalog import catalog
from test_subchat_http_generation import handoff_data

from anywhere_computer.subchat import SubchatAccessError
from anywhere_computer.subchat_chrome_login import CATALOG_URL, chrome_http_session
from anywhere_computer.subchat_http_generation import ObservedHTTPGeneration
from anywhere_computer.subchat_state import SubchatAccountMismatch


class Page:
    async def evaluate(self, _expression):
        return 'Chrome test user agent'

    async def close(self):
        pass


class Context:
    async def cookies(self, _url):
        return [{'domain': '.chatgpt.com', 'name': 'session', 'value': 'private-cookie'},
                {'domain': 'other.example', 'name': 'other', 'value': 'excluded'}]

    async def new_page(self):
        return Page()


async def test_chrome_login_gets_token_then_validates_catalog():
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

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        session = await chrome_http_session(Context(), client, expected_account_id='account')
    assert len(seen) == 2
    assert session.catalog_url == CATALOG_URL
    assert session.headers()['cookie'] == 'session=private-cookie'
    assert session.user_email == 'owner@example.com'
    assert 'private-token' not in repr(session)
    assert 'private-cookie' not in repr(session)


async def test_chrome_login_stops_on_denied_get():
    paths = []

    def deny(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(403, json={'error': 'denied'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(deny)) as client:
        with pytest.raises(SubchatAccessError):
            await chrome_http_session(Context(), client)
    assert paths == ['/api/auth/session']


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
