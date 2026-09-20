"""Read-only Chat HTTP session, bootstrapped from the dedicated browser once."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..subchat import SubchatAccessError, SubchatAnswer, SubchatReceipt
from ..subchat_state import SubchatSubmission
from .catalog import observe_http_catalog, project_http_catalog
from .history import observe_history, project_history, project_receipt

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext, BrowserContext, Page, Response


class ChatHTTPReader:
    """Keep observed auth/account/language in memory; never export credentials.

    Only history and the observed model-catalog URL are requested. Cookies stay
    with the browser context; an injected standalone client receives no copied cookies.
    Its owner manages disposal. No generation, redirects or automatic retries.
    """

    def __init__(self, request_factory: Callable[[], Awaitable[APIRequestContext]] | None = None
                 ) -> None:
        self._request_factory = request_factory
        self._context: BrowserContext | None = None
        self._headers: dict[str, str] = {}
        self._catalog_url: str | None = None
        self._access_status: int | None = None

    def can_read_without_browser(self, context: BrowserContext, *, catalog: bool = False
                                 ) -> bool:
        """Reuse only this session's observed authorization, including its rejection latch."""
        return (self._request_factory is not None and self._context is context
                and (self._access_status is not None
                     or bool(self._headers) and (not catalog or self._catalog_url is not None)))

    async def _read(self, context: BrowserContext, url: str | None,
                    observe: Callable[[Page], Awaitable[Response]]) -> bytes:
        if self._context is not context:
            self._context = context
            self._headers = {}
            self._catalog_url = None
            self._access_status = None
        if self._access_status is not None:
            raise SubchatAccessError(self._access_status)
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
            except SubchatAccessError as error:
                self._headers = {}
                self._access_status = error.status
                raise
            finally:
                await asyncio.wait_for(page.close(), timeout=5)
        request = (await self._request_factory() if self._request_factory is not None
                   else context.request)
        response_http = await request.get(
            url, headers=self._headers, timeout=15_000, max_redirects=0, max_retries=0)
        try:
            if response_http.status in (401, 403):
                self._headers = {}
                # Polling must not open login tabs after access is rejected.
                # A new reader/context explicitly starts a new login observation.
                self._access_status = response_http.status
                raise SubchatAccessError(response_http.status)
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
        return project_history(await self._history_payload(context, submission), submission)

    async def receipt(self, context: BrowserContext,
                      submission: SubchatSubmission) -> SubchatReceipt | None:
        return project_receipt(await self._history_payload(context, submission), submission)

    async def _history_payload(self, context: BrowserContext,
                               submission: SubchatSubmission) -> bytes:
        async def observe(page: Page) -> Response:
            return await observe_history(page, submission)

        return await self._read(context,
            'https://chatgpt.com/backend-api/conversations/' + str(submission.conversation_id),
            observe)

    async def catalog(self, context: BrowserContext) -> dict[str, object]:
        async def observe(page: Page) -> Response:
            response = await observe_http_catalog(page)
            self._catalog_url = response.url  # Preserve observed query parameters.
            return response

        payload = await self._read(context, self._catalog_url, observe)
        result = project_http_catalog(payload)
        result['source'] = 'browser_session_http'
        return result
