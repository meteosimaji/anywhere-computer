"""Read-only Chat HTTP session, bootstrapped from the dedicated browser once."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..subchat import SubchatAnswer
from ..subchat_state import SubchatSubmission
from .catalog import observe_http_catalog, project_http_catalog
from .history import observe_history, project_history

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Response


class ChatHTTPReader:
    """Keep observed auth/account/language in memory; never export credentials.

    Only history and the observed model-catalog URL are requested. Cookies stay
    with the browser context. No generation, redirects or automatic retries.
    """

    def __init__(self) -> None:
        self._context: BrowserContext | None = None
        self._headers: dict[str, str] = {}
        self._catalog_url: str | None = None

    async def _read(self, context: BrowserContext, url: str | None,
                    observe: Callable[[Page], Awaitable[Response]]) -> bytes:
        if self._context is not context:
            self._context = context
            self._headers = {}
            self._catalog_url = None
        if not self._headers or url is None:
            page = await context.new_page()
            try:
                response = await observe(page)
                headers = {}
                for name in ('authorization', 'chatgpt-account-id', 'oai-language'):
                    value = await response.request.header_value(name)
                    if value:
                        headers[name] = value
                # Both observers require an authenticated exact-origin GET.
                self._headers = headers
                return await response.body()
            finally:
                await asyncio.wait_for(page.close(), timeout=5)
        response_http = await context.request.get(
            url, headers=self._headers, timeout=15_000, max_redirects=0, max_retries=0)
        try:
            if response_http.status in (401, 403):
                self._headers = {}  # Next explicit read re-observes login, never generation.
            if response_http.status != 200:
                raise ConnectionError('Chat read request did not succeed')
            if response_http.headers.get('content-type', '').split(';', 1)[0].strip() != (
                    'application/json'):
                raise ValueError('Unexpected Chat response format')
            return await response_http.body()
        finally:
            await response_http.dispose()

    async def history(self, context: BrowserContext,
                      submission: SubchatSubmission) -> SubchatAnswer | None:
        async def observe(page: Page) -> Response:
            return await observe_history(page, submission)

        payload = await self._read(context,
            'https://chatgpt.com/backend-api/conversations/' + str(submission.conversation_id),
            observe)
        return project_history(payload, submission)

    async def catalog(self, context: BrowserContext) -> dict[str, object]:
        async def observe(page: Page) -> Response:
            response = await observe_http_catalog(page)
            self._catalog_url = response.url  # Preserve observed query parameters.
            return response

        payload = await self._read(context, self._catalog_url, observe)
        result = project_http_catalog(payload)
        result['source'] = 'browser_session_http'
        return result
