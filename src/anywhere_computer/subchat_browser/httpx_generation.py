"""Send one browser-prepared Chat turn through HTTPX without replaying it.

The browser supplies its current one-turn request. This module owns only the
generation transport; it does not acquire or synthesize protection values.
Request and response contents remain in memory and must never be logged.
"""
from __future__ import annotations

from collections.abc import Callable
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


class HTTPXGenerationPreflightError(ValueError):
    """The intercepted request was rejected before an HTTPX POST began."""


@dataclass(frozen=True, slots=True)
class HTTPXGenerationResponse:
    status: int
    content_type: str
    body: bytes
    http_version: str
    cf_mitigated: bool


async def post_browser_prepared_once(request: Request, client: httpx.AsyncClient,
                                     *, authorization: str, content: bytes,
                                     on_headers: Callable[[HTTPXGenerationResponse], None]
                                     | None = None,
                                     on_chunk: Callable[[bytes], None] | None = None
                                     ) -> HTTPXGenerationResponse:
    """Make exactly one HTTPX POST from an intercepted, account-bound request."""
    parsed = urlsplit(request.url)
    if (request.method != 'POST' or parsed.scheme != 'https'
            or parsed.netloc != 'chatgpt.com'
            or parsed.path != '/backend-api/f/conversation'
            or parsed.query or parsed.fragment):
        raise HTTPXGenerationPreflightError('Unexpected Chat generation route')
    if not content or len(content) > _MAX_REQUEST:
        raise HTTPXGenerationPreflightError('Invalid Chat generation request size')
    headers = await request.all_headers()
    if (headers.get('authorization') != authorization
            or not headers.get('cookie')
            or headers.get('origin') != 'https://chatgpt.com'
            or headers.get('content-type', '').split(';', 1)[0] != 'application/json'):
        raise HTTPXGenerationPreflightError('Chat generation account or origin changed')
    # Playwright may memoize an earlier lookup of an absent optional account
    # header as a None entry in all_headers(). The auth GET binds that account.
    outgoing = {name: value for name, value in headers.items()
                if name.lower() not in _DROP_HEADERS and not name.startswith(':')
                and not (name.lower() == 'chatgpt-account-id' and value is None)}
    invalid_names = [name for name, value in outgoing.items() if not isinstance(value, str)]
    if invalid_names:
        raise HTTPXGenerationPreflightError(
            'Invalid browser request header types: ' + ','.join(invalid_names))
    outgoing['accept-encoding'] = 'identity'
    # Generations can remain silent while thinking. The saved send identity
    # remains recoverable, and cancellation does not authorize another POST.
    async with client.stream('POST', request.url, headers=outgoing, content=content,
                             timeout=httpx.Timeout(connect=10.0, read=None,
                                                   write=120.0, pool=120.0),
                             follow_redirects=False) as response:
        content_type = response.headers.get('content-type', '').split(';', 1)[0].strip()
        status = response.status_code
        metadata = HTTPXGenerationResponse(status, content_type, b'', response.http_version,
                                           'cf-mitigated' in response.headers)
        if on_headers is not None:
            on_headers(metadata)
        if status != 200 or content_type != 'text/event-stream':
            # Provider error bodies can include account data. Never return one.
            return metadata
        body_parts: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE:
                raise ValueError('Chat generation response exceeds the memory limit')
            body_parts.append(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        return HTTPXGenerationResponse(status, content_type, b''.join(body_parts),
                                       response.http_version,
                                       'cf-mitigated' in response.headers)
