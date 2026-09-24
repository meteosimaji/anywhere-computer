"""Send one browser-prepared Chat turn through HTTPX without replaying it.

The browser supplies its current one-turn request. This module owns only the
generation transport; it does not acquire or synthesize protection values.
Request and response contents remain in memory and must never be logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from playwright.async_api import Request


_MAX_REQUEST = 1_048_576
_MAX_RESPONSE = 16_777_216
_DROP_HEADERS = frozenset({
    'accept-encoding', 'connection', 'content-length', 'host', 'keep-alive',
    'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
    'transfer-encoding', 'upgrade',
})


@dataclass(frozen=True, slots=True)
class HTTPXGenerationResponse:
    status: int
    content_type: str
    body: bytes
    http_version: str
    cf_mitigated: bool


async def post_browser_prepared_once(request: Request, client: httpx.AsyncClient,
                                     *, authorization: str, content: bytes
                                     ) -> HTTPXGenerationResponse:
    """Make exactly one HTTPX POST from an intercepted, account-bound request."""
    parsed = urlsplit(request.url)
    if (request.method != 'POST' or parsed.scheme != 'https'
            or parsed.netloc != 'chatgpt.com'
            or parsed.path != '/backend-api/f/conversation'
            or parsed.query or parsed.fragment):
        raise ValueError('Unexpected Chat generation route')
    if not content or len(content) > _MAX_REQUEST:
        raise ValueError('Invalid Chat generation request size')
    headers = await request.all_headers()
    if (headers.get('authorization') != authorization
            or not headers.get('cookie')
            or headers.get('origin') != 'https://chatgpt.com'
            or headers.get('content-type', '').split(';', 1)[0] != 'application/json'):
        raise ValueError('Chat generation account or origin changed')
    # Playwright may memoize an earlier lookup of an absent optional account
    # header as a None entry in all_headers(). The auth GET binds that account.
    outgoing = {name: value for name, value in headers.items()
                if name.lower() not in _DROP_HEADERS and not name.startswith(':')
                and not (name.lower() == 'chatgpt-account-id' and value is None)}
    invalid_names = [name for name, value in outgoing.items() if not isinstance(value, str)]
    if invalid_names:
        raise ValueError('Invalid browser request header types: ' + ','.join(invalid_names))
    outgoing['accept-encoding'] = 'identity'
    async with client.stream('POST', request.url, headers=outgoing, content=content,
                             timeout=httpx.Timeout(120.0, connect=10.0),
                             follow_redirects=False) as response:
        content_type = response.headers.get('content-type', '').split(';', 1)[0].strip()
        status = response.status_code
        if status != 200 or content_type != 'text/event-stream':
            # Provider error bodies can include account data. Never return one.
            return HTTPXGenerationResponse(status, content_type, b'',
                                           response.http_version,
                                           'cf-mitigated' in response.headers)
        body_parts: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE:
                raise ValueError('Chat generation response exceeds the memory limit')
            body_parts.append(chunk)
        return HTTPXGenerationResponse(status, content_type, b''.join(body_parts),
                                       response.http_version,
                                       'cf-mitigated' in response.headers)
