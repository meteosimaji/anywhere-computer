"""Bootstrap personal Chat HTTP access from an owned, logged-in Chrome profile."""
from __future__ import annotations

import httpx
from playwright.async_api import BrowserContext

from .subchat import SubchatAccessError
from .subchat_browser.catalog import project_http_catalog
from .subchat_http_session import ObservedHTTPSession

CATALOG_URL = 'https://chatgpt.com/backend-api/models?language=ja'


async def chrome_http_session(context: BrowserContext, client: httpx.AsyncClient
                              ) -> ObservedHTTPSession:
    """GET auth and catalog with HTTPX; retain Chrome cookies only in memory."""
    cookies = await context.cookies('https://chatgpt.com')
    cookie_header = '; '.join(
        f"{cookie['name']}={cookie['value']}" for cookie in cookies
        if cookie.get('domain', '').lstrip('.') == 'chatgpt.com')
    if not cookie_header:
        raise SubchatAccessError(401)
    page = await context.new_page()
    try:
        user_agent = await page.evaluate('navigator.userAgent')
    finally:
        await page.close()
    auth_headers = {'cookie': cookie_header, 'accept': 'application/json',
                    'referer': 'https://chatgpt.com/', 'user-agent': user_agent,
                    'sec-fetch-site': 'same-origin', 'sec-fetch-mode': 'cors',
                    'sec-fetch-dest': 'empty'}
    response = await client.get('https://chatgpt.com/api/auth/session',
                                headers=auth_headers, timeout=15.0,
                                follow_redirects=False)
    try:
        if response.status_code in (401, 403):
            raise SubchatAccessError(response.status_code)
        if response.status_code != 200:
            raise ConnectionError('Chrome login session GET failed')
        if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise ValueError('Unexpected Chrome login response')
        if len(response.content) > 131_072:
            raise ValueError('Chrome login response is too large')
        data = response.json()
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
            'cookie': cookie_header,
            'user_agent': user_agent,
            'user_email': email,
        })
    finally:
        await response.aclose()
    catalog = await client.get(CATALOG_URL, headers=session.headers(),
                               timeout=15.0, follow_redirects=False)
    try:
        if catalog.status_code in (401, 403):
            raise SubchatAccessError(catalog.status_code)
        if catalog.status_code != 200:
            raise ConnectionError('Chrome login catalog GET failed')
        if catalog.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise ValueError('Unexpected Chrome login catalog response')
        project_http_catalog(catalog.content)
    finally:
        await catalog.aclose()
    return session
