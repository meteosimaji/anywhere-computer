"""Read-only Chat HTTP session, with browser bootstrap or explicit in-memory handoff."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from ..subchat import (
    SubchatAccessError,
    SubchatAnswer,
    SubchatPendingObservation,
    SubchatReceipt,
    SubchatUnsupported,
)
from ..subchat_state import SubchatAccountMismatch, SubchatSubmission
from .catalog import observe_http_catalog, project_http_catalog
from .history import matched_input, observe_history, project_observation, project_receipt

if TYPE_CHECKING:
    from httpx import AsyncClient
    from httpx import Response as HTTPXResponse
    from playwright.async_api import APIRequestContext, APIResponse, BrowserContext, Page, Response

    from ..subchat_http_session import ObservedHTTPSession

logger = logging.getLogger(__name__)


async def bounded_httpx_body(response: HTTPXResponse, limit: int, error: str) -> bytes:
    """Read decoded HTTPX bytes without retaining a response above its schema limit."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes(chunk_size=65_536):
        size += len(chunk)
        if size > limit:
            raise ValueError(error)
        chunks.append(chunk)
    return b''.join(chunks)


class ChatHTTPReader:
    """Keep observed auth/account/language in memory; never export credentials.

    Only history and the observed model-catalog URL are requested. Browser-assisted
    reads use the existing Playwright request context; browser-free reads use the
    supplied standalone HTTPX client. No cookies are copied. No generation, redirects
    or automatic retries. Browser-free mode cannot acquire credentials or repair access.
    """

    def __init__(self, request_factory: Callable[
                 [], Awaitable[APIRequestContext | AsyncClient]] | None = None,
                 *, browser_free: bool = False, session: ObservedHTTPSession | None = None,
                 test_origin: str | None = None,
                 access_status: int | None = None) -> None:
        if (browser_free and request_factory is None) or (session is not None and not browser_free):
            raise ValueError('Explicit HTTP sessions require a browser-free request factory')
        if access_status not in (None, 401, 403) or (access_status is not None
                                                    and (session is not None or not browser_free)):
            raise ValueError('Rejected HTTP access requires an unbound browser-free reader')
        if test_origin is not None:
            parsed = urlsplit(test_origin)
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or parsed.port is None or parsed.path or parsed.query or parsed.fragment
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError('Only an exact loopback test origin is supported')
        self._origin = test_origin or 'https://chatgpt.com'
        self._request_factory = request_factory
        self._browser_free = browser_free
        self._context: BrowserContext | None = None
        self._headers: dict[str, str] = session.headers() if session is not None else {}
        self._catalog_url: str | None = session.catalog_url if session is not None else None
        self._access_status: int | None = access_status
        self._denied_urls: set[str] = set()
        self._pending_closes: set[asyncio.Task[None]] = set()

    async def _retry_page_close(self, page: Page) -> None:
        try:
            await asyncio.wait_for(page.close(), timeout=5)
        except Exception:
            # The read has already completed. Report cleanup failure without
            # turning a successful read or its original error into a close error.
            logger.warning('Temporary Chat read tab could not be closed', exc_info=True)

    def can_read_without_browser(self, context: BrowserContext, *, catalog: bool = False
                                 ) -> bool:
        """Reuse only this session's observed authorization, including its rejection latch."""
        return (self._request_factory is not None and self._context is context
                and (self._access_status is not None
                     or bool(self._headers) and (not catalog or self._catalog_url is not None)))

    async def _read(self, context: BrowserContext | None, url: str | None,
                    observe: Callable[[Page], Awaitable[Response]],
                    expected_account: str | None = None) -> bytes:
        if self._browser_free:
            if context is not None:
                raise ValueError('A browser-free reader cannot accept a browser context')
        elif context is None:
            raise ValueError('Browser bootstrap requires an explicit context')
        elif self._context is not context:
            self._context = context
            self._headers = {}
            self._catalog_url = None
            self._access_status = None
            self._denied_urls.clear()
        if self._access_status is not None:
            raise SubchatAccessError(self._access_status)
        if url in self._denied_urls:
            raise SubchatAccessError(403)
        if not self._headers or url is None:
            if self._browser_free:
                raise SubchatUnsupported('http_session_required')
            assert context is not None
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
                self._check_account(expected_account)
                return await response.body()
            except SubchatAccessError as error:
                self._headers = {}
                self._access_status = error.status
                raise
            finally:
                try:
                    await asyncio.wait_for(page.close(), timeout=5)
                except TimeoutError:
                    # Retry in the background so a stalled tab close cannot replace
                    # the read result or its original error.
                    task = asyncio.create_task(self._retry_page_close(page))
                    self._pending_closes.add(task)
                    task.add_done_callback(self._pending_closes.discard)
        self._check_account(expected_account)
        if self._request_factory is not None:
            request = await self._request_factory()
        else:
            assert context is not None
            request = context.request
        # Another read may invalidate access while the client factory is awaiting.
        if self._access_status is not None:
            raise SubchatAccessError(self._access_status)
        if url in self._denied_urls:
            raise SubchatAccessError(403)
        self._check_account(expected_account)
        if self._browser_free:
            # Browser-free readers accept an HTTPX factory; the browser mode above
            # retains its Playwright APIRequestContext contract.
            httpx_request = cast('AsyncClient', request)
            async with httpx_request.stream(
                    'GET', url, headers=self._headers, timeout=15.0,
                    follow_redirects=False) as response_httpx:
                status = response_httpx.status_code
                content_type = response_httpx.headers.get('content-type', '')
                self._check_read_response(url, status, content_type)
                catalog = url == self._catalog_url
                return await bounded_httpx_body(
                    response_httpx, 1_048_576 if catalog else 4_194_304,
                    'Model catalog is too large' if catalog else
                    'Conversation response is too large')
        playwright_request = cast('APIRequestContext', request)
        response_browser: APIResponse = await playwright_request.get(
            url, headers=self._headers, timeout=15_000, max_redirects=0, max_retries=0)
        try:
            self._check_read_response(url, response_browser.status,
                                      response_browser.headers.get('content-type', ''))
            return await response_browser.body()
        finally:
            await response_browser.dispose()

    def _check_read_response(self, url: str | None, status: int,
                             content_type: str) -> None:
        if status in (401, 403):
            if status == 401:
                self._headers = {}
                self._access_status = 401
            else:
                # A forbidden resource does not invalidate unrelated reads.
                # Keep this URL rejected without retries or login fallback.
                if url is not None:
                    self._denied_urls.add(url)
            raise SubchatAccessError(status)
        if status != 200:
            raise ConnectionError('Chat read request did not succeed')
        if content_type.split(';', 1)[0].strip() != 'application/json':
            raise ValueError('Unexpected Chat response format')

    def check_generation_account(self, account: str) -> None:
        """A bound reader must be able to recover a request before it is forwarded."""
        if self._access_status is not None:
            raise SubchatAccessError(self._access_status)
        if self._headers:
            self._check_account(account)

    def _check_account(self, expected_account: str | None) -> None:
        if (expected_account is not None
                and self._headers.get('chatgpt-account-id') != expected_account):
            raise SubchatAccountMismatch('Saved submission belongs to a different Chat account')

    async def history(self, context: BrowserContext | None,
                      submission: SubchatSubmission) -> SubchatAnswer | SubchatPendingObservation:
        return project_observation(await self._history_payload(context, submission), submission)

    async def receipt(self, context: BrowserContext | None,
                      submission: SubchatSubmission) -> SubchatReceipt | None:
        return project_receipt(await self._history_payload(context, submission), submission)

    async def _history_payload(self, context: BrowserContext | None,
                               submission: SubchatSubmission) -> bytes:
        # Report a missing/expired explicit session before account comparison. This
        # never bootstraps a browser or makes a request when authorization is absent.
        if self._browser_free and (not self._headers or self._access_status is not None):
            await self.catalog(context)
        # Establish the account on a non-conversation read before requesting a
        # bound operation's history. Never probe another account's conversation.
        if (submission.provider_account_id is not None
                and (self._context is not context or not self._headers)):
            await self.catalog(context)
        self._check_account(submission.provider_account_id)

        async def observe(page: Page) -> Response:
            return await observe_history(page, submission)

        return await self._read(context,
            self._origin + '/backend-api/conversations/' + str(submission.conversation_id),
            observe, expected_account=submission.provider_account_id)

    async def catalog(self, context: BrowserContext | None) -> dict[str, object]:
        async def observe(page: Page) -> Response:
            response = await observe_http_catalog(page)
            self._catalog_url = response.url  # Preserve observed query parameters.
            return response

        payload = await self._read(context, self._catalog_url, observe)
        result = project_http_catalog(payload)
        result['source'] = 'preauthenticated_http' if self._browser_free else 'browser_session_http'
        return result

    async def verify_delete_target(self, context: BrowserContext | None,
                                   submission: SubchatSubmission) -> None:
        payload = await self._history_payload(context, submission)
        if matched_input(payload, submission) is None:
            raise ValueError('Remote conversation does not match the saved input')

    async def patch_delete(self, context: BrowserContext | None,
                           submission: SubchatSubmission) -> bool:
        self._check_account(submission.provider_account_id)
        if not self._headers:
            raise ValueError('Deletion needs an observed authenticated session')
        if self._access_status is not None:
            raise SubchatAccessError(self._access_status)
        if self._request_factory is not None:
            request = await self._request_factory()
        else:
            assert context is not None
            request = context.request
        self._check_account(submission.provider_account_id)
        url = self._origin + '/backend-api/conversation/' + str(submission.conversation_id)
        headers = {**self._headers, 'content-type': 'application/json'}
        if self._browser_free:
            httpx_request = cast('AsyncClient', request)
            response_httpx = await httpx_request.patch(
                url, content=b'{"is_visible":false}', headers=headers,
                timeout=15.0, follow_redirects=False)
            try:
                status = response_httpx.status_code
                content_type = response_httpx.headers.get('content-type', '')
                body = response_httpx.content
            finally:
                await response_httpx.aclose()
        else:
            playwright_request = cast('APIRequestContext', request)
            response_browser = await playwright_request.patch(
                url, data='{"is_visible":false}', headers=headers,
                timeout=15_000, max_redirects=0, max_retries=0)
            try:
                status = response_browser.status
                content_type = response_browser.headers.get('content-type', '')
                body = await response_browser.body() if status == 200 else b''
            finally:
                await response_browser.dispose()
        if status in (401, 403):
            raise SubchatAccessError(status)
        if status != 200 or content_type.split(';', 1)[0].strip() != 'application/json':
            return False
        if len(body) > 65_536:
            return False
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict) or data.get('success') is not True:
            return False
        history_url = self._origin + '/backend-api/conversations/' + str(
            submission.conversation_id)
        if self._browser_free:
            async with httpx_request.stream(
                    'GET', history_url, headers=self._headers, timeout=15.0,
                    follow_redirects=False) as response_httpx:
                return response_httpx.status_code == 404
        response_browser = await playwright_request.get(
            history_url, headers=self._headers, timeout=15_000,
            max_redirects=0, max_retries=0)
        try:
            return response_browser.status == 404
        finally:
            await response_browser.dispose()
