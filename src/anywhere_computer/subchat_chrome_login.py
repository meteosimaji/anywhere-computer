"""Bootstrap personal Chat HTTP access from an owned, logged-in Chrome profile."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from http.cookiejar import Cookie

import httpx
from playwright.async_api import BrowserContext, Page

from .subchat import SubchatAccessError
from .subchat_browser.catalog import project_http_catalog
from .subchat_browser.http_reader import bounded_httpx_body
from .subchat_http_session import ObservedHTTPSession
from .subchat_state import SubchatAccountMismatch

CATALOG_URL = 'https://chatgpt.com/backend-api/models?language=ja'
AUTH_URL = 'https://chatgpt.com/api/auth/session'
GENERATION_URL = 'https://chatgpt.com/backend-api/f/conversation'
logger = logging.getLogger(__name__)


async def _exact_origin_only(request: httpx.Request) -> None:
    """Prevent an imported Chrome cookie from reaching another host or scheme."""
    if (request.url.scheme != 'https' or request.url.host != 'chatgpt.com'
            or request.url.port not in (None, 443)):
        raise ValueError('Chrome session requests require the exact ChatGPT origin')


def chrome_generation_cookie(client: httpx.AsyncClient) -> str | None:
    """Read the current path-scoped cookie for handoff validation only."""
    cookie = client.build_request('POST', GENERATION_URL).headers.get('cookie')
    return str(cookie) if cookie is not None else None


def _import_chrome_cookies(client: httpx.AsyncClient,
                           cookies: Iterable[Mapping[str, object]]) -> None:
    """Keep Chrome cookie path/expiry and restrict this client to one origin."""
    if _exact_origin_only not in client.event_hooks['request']:
        client.event_hooks['request'].append(_exact_origin_only)
    for item in cookies:
        domain = item.get('domain')
        name = item.get('name')
        value = item.get('value')
        path = item.get('path', '/')
        expires = item.get('expires', -1)
        if (item.get('partitionKey') is not None
                or domain not in ('chatgpt.com', '.chatgpt.com')
                or not isinstance(name, str) or not name
                or not isinstance(value, str) or not isinstance(path, str)
                or not path.startswith('/')
                or not isinstance(expires, (int, float)) or isinstance(expires, bool)):
            continue
        client.cookies.jar.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'),
            domain_initial_dot=domain.startswith('.'), path=path, path_specified=True,
            secure=item.get('secure') is True,
            expires=int(expires) if expires > 0 else None,
            discard=expires <= 0, comment=None, comment_url=None,
            rest={}, rfc2109=False))


async def chrome_http_session(context: BrowserContext, client: httpx.AsyncClient,
                              *, expected_account_id: str | None = None,
                              page_factory: Callable[[], Awaitable[Page]] | None = None
                              ) -> ObservedHTTPSession:
    """GET auth and catalog with HTTPX; retain Chrome cookies only in memory."""
    _import_chrome_cookies(client, await context.cookies())
    if 'cookie' not in client.build_request('GET', AUTH_URL).headers:
        raise SubchatAccessError(401)
    page = await (page_factory() if page_factory is not None else context.new_page())
    try:
        user_agent = await page.evaluate('navigator.userAgent')
    finally:
        await page.close()
    auth_headers = {'accept': 'application/json',
                    'referer': 'https://chatgpt.com/', 'user-agent': user_agent,
                    'sec-fetch-site': 'same-origin', 'sec-fetch-mode': 'cors',
                    'sec-fetch-dest': 'empty'}
    auth_started = time.perf_counter()
    async with client.stream('GET', AUTH_URL, headers=auth_headers,
                             timeout=15.0, follow_redirects=False) as response:
        logger.info('Subchat HTTP auth GET status=%d elapsed_ms=%d',
                    response.status_code, int((time.perf_counter() - auth_started) * 1000))
        if response.status_code in (401, 403):
            raise SubchatAccessError(response.status_code)
        if response.status_code != 200:
            raise ConnectionError('Chrome login session GET failed')
        if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise ValueError('Unexpected Chrome login response')
        body = await bounded_httpx_body(response, 131_072,
                                        'Chrome login response is too large')
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get('account'), dict):
            raise ValueError('Chrome login response has no account')
        account_id = data['account'].get('id')
        authorization = data.get('accessToken')
        user = data.get('user')
        email = user.get('email') if isinstance(user, dict) else None
        if (not isinstance(account_id, str) or not isinstance(authorization, str)
                or not isinstance(email, str)):
            raise ValueError('Chrome login response has no authorization')
        session = ObservedHTTPSession.model_validate({
            'authorization': 'Bearer ' + authorization,
            'account_id': account_id,
            'catalog_url': CATALOG_URL,
            'language': 'ja',
            'user_agent': user_agent,
            'user_email': email,
        })
    if expected_account_id is not None and session.account_id != expected_account_id:
        raise SubchatAccountMismatch('Chrome login selected another Chat account')
    catalog_started = time.perf_counter()
    async with client.stream('GET', CATALOG_URL, headers=session.headers(),
                             timeout=15.0, follow_redirects=False) as catalog:
        logger.info('Subchat HTTP catalog GET status=%d elapsed_ms=%d',
                    catalog.status_code, int((time.perf_counter() - catalog_started) * 1000))
        if catalog.status_code in (401, 403):
            raise SubchatAccessError(catalog.status_code)
        if catalog.status_code != 200:
            raise ConnectionError('Chrome login catalog GET failed')
        if catalog.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise ValueError('Unexpected Chrome login catalog response')
        project_http_catalog(await bounded_httpx_body(
            catalog, 1_048_576, 'Model catalog is too large'))
    return session
