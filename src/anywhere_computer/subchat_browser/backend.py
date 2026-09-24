"""Ordinary Chat adapter using an explicitly supplied dedicated browser context.

Used by the local stdio MCP entry. Existing-conversation sends checkpoint
visible message identities before dispatch; see the dated acceptance records.
"""
from __future__ import annotations

import asyncio
import logging
import re
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from anywhere_computer.subchat import (
    SubchatAnswer,
    SubchatBrowserClosed,
    SubchatPendingObservation,
    SubchatPreparationFailed,
    SubchatReceipt,
    SubchatStaleTarget,
)
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmission

from .catalog import (
    COMPOSER,
    CONTROL,
    EDITOR,
    SOURCE,
    TOGGLE,
    TRIGGER,
    collect_page,
    picker_ready,
    require_http_selection,
)
from .efforts import matches_effort, move_effort, snapshot
from .http_reader import ChatHTTPReader
from .request_content import add_resources, generation_input

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext, BrowserContext, Locator, Page, Route

INPUT = Path(__file__).with_name('subchat_input.js').read_text(encoding="utf-8")
STREAM = Path(__file__).with_name('subchat_stream.js').read_text(encoding='utf-8')
COPY = Path(__file__).with_name('subchat_copy.js').read_text(encoding="utf-8")
CHAT = re.compile(r'https://chatgpt\.com/c/'
                  r'([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\Z')
logger = logging.getLogger(__name__)


class BrowserSubchatBackend:
    def __init__(self, context: BrowserContext | Callable[[], Awaitable[BrowserContext]],
                 *, http_read: bool = False,
                 http_request_factory: Callable[[], Awaitable[APIRequestContext]] | None = None,
                 record_request: Callable[[str, str, str], None] | None = None,
                 record_preflight_failure: Callable[[str], None] | None = None,
                 record_conversation: Callable[[str, str, str, str], None] | None = None,
                 record_rejection: Callable[[str, str, int, str], None] | None = None,
                 httpx_generation: bool = False,
                 background_pages: bool = False,
                 expected_account_id: str | None = None) -> None:
        if httpx_generation and not http_read:
            raise ValueError('Browser-prepared HTTPX generation requires HTTP history')
        if background_pages and not httpx_generation:
            raise ValueError('Background pages require HTTPX generation')
        self.http_read = http_read
        self._httpx_generation = httpx_generation
        self._background_pages = background_pages
        self._expected_account_id = expected_account_id
        self._record_request = record_request
        self._record_preflight_failure = record_preflight_failure
        self._record_conversation = record_conversation
        self._record_rejection = record_rejection
        self._http_reader = ChatHTTPReader(
            http_request_factory, page_factory=self._new_page if background_pages else None)
        self._context = None if callable(context) else context
        self._create_context = context if callable(context) else None
        self._context_lock = asyncio.Lock()
        self.pages: dict[str, Page] = {}
        self._prepared_baseline_kinds: dict[
            str, Literal['legacy_turn_key', 'message_id', 'empty']] = {}
        self._preparation_touched_pages: set[Page] = set()
        self._unreusable_pages: set[Page] = set()
        self._completed_page_owners: set[str] = set()
        self._closed = False
        if self._context is not None:
            self._context.on('close', self._browser_closed)

    def capabilities(self) -> dict[str, object]:
        return {'queue_dispatch': 'recover_or_wait', 'background_dispatcher': False,
                'native_steer': False, 'provider_stop': False,
                'cancel_scope': 'local_queued_or_prepared',
                'state': 'capabilities', 'transport': 'browser_prepared',
                'browser_required': True,
                'generation_transport': ('browser_prepared_httpx' if self._httpx_generation
                                         else 'browser_prepared'),
                'http_selection_send_supported': self.http_read,
                'credential_refresh': False, 'independent_login': False,
                'http_delete_supported': True, 'deletion_transport': 'authenticated_http'}

    def _browser_closed(self, context: BrowserContext) -> None:
        self._closed = True

    def queue_watch_ready(self, submission: SubchatSubmission) -> bool:
        """Background delivery may reuse a live owned tab, never cold-start Chrome."""
        context = self._context
        if context is None or self._closed:
            return False
        if context.browser is not None and not context.browser.is_connected():
            return False
        return any(not page.is_closed() and page.url == self._url(submission)
                   for page in self.pages.values())

    async def _browser(self) -> BrowserContext:
        async with self._context_lock:
            if self._context is None:
                assert self._create_context is not None
                self._context = await self._create_context()
                self._context.on('close', self._browser_closed)
            browser = self._context.browser
            if self._closed or (browser is not None and not browser.is_connected()):
                raise SubchatBrowserClosed('Dedicated browser disconnected; recover saved IDs')
            return self._context

    async def _new_page(self) -> Page:
        context = await self._browser()
        if self._background_pages:
            from .background import new_background_page

            return await new_background_page(context)
        return await context.new_page()

    async def _click(self, locator: Locator) -> None:
        if self._background_pages:
            from .background import background_pointer_click

            await background_pointer_click(locator)
        else:
            await locator.click()

    async def _press(self, locator: Locator, key: str) -> None:
        if self._background_pages:
            from .background import background_key_press

            await background_key_press(locator, key)
        else:
            await locator.press(key)

    async def catalog(self, model: str | None = None) -> dict[str, object]:
        page = await self._new_page()
        page.set_default_timeout(15_000)
        try:
            response = await page.goto('https://chatgpt.com/', wait_until='domcontentloaded')
            if response is None or not response.ok or not await picker_ready(page):
                return {'state': 'catalog_unavailable', 'submitted': False}
            return await collect_page(page, model, background_input=self._background_pages)
        finally:
            # This is a separate observation tab, never a submission or user draft.
            await page.close()

    async def _read_context(self, *, catalog: bool = False) -> BrowserContext:
        if (self._context is not None
                and self._http_reader.can_read_without_browser(self._context, catalog=catalog)):
            return self._context
        return await self._browser()

    async def http_catalog(self) -> dict[str, object]:
        async with asyncio.timeout(20):
            result = await self._http_reader.catalog(await self._read_context(catalog=True))
            result['http_selection_send_supported'] = self.http_read
            result['generation_transport'] = ('browser_prepared_httpx'
                                              if self._httpx_generation else 'browser_prepared')
            return result

    async def verify_delete_target(self, submission: SubchatSubmission) -> None:
        async with asyncio.timeout(20):
            await self._http_reader.verify_delete_target(await self._read_context(), submission)

    async def patch_delete(self, submission: SubchatSubmission) -> bool:
        async with asyncio.timeout(20):
            return await self._http_reader.patch_delete(await self._read_context(), submission)

    async def _page(self, submission: SubchatSubmission) -> Page | None:
        if self._context is not None:
            await self._browser()  # Diagnose a closed existing context without launching one.
        page = self.pages.get(submission.operation_id)
        if page is not None and not page.is_closed():
            return page
        if submission.conversation_id is None:
            return None
        page = await self._new_page()
        self.pages[submission.operation_id] = page
        await page.goto('https://chatgpt.com/c/' + submission.conversation_id,
                        wait_until='domcontentloaded')
        return page

    def _url(self, submission: SubchatSubmission) -> str:
        if submission.requested_conversation_id is None:
            return 'https://chatgpt.com/'
        url = 'https://chatgpt.com/c/' + submission.requested_conversation_id
        if CHAT.fullmatch(url) is None:
            raise ValueError('Invalid conversation identity')
        return url

    async def _baseline_snapshot(self, page: Page) -> tuple[
        tuple[str, ...], tuple[str, ...], Literal['legacy_turn_key', 'message_id', 'empty']
    ]:
        if await page.locator('main').count() != 1:
            raise ValueError('Conversation history unavailable')
        messages = await page.locator('main [data-message-author-role]').evaluate_all(
            "elements => elements.map(e => ({id: e.getAttribute('data-message-id'), "
            "role: e.getAttribute('data-message-author-role')}))")
        values = await page.locator('main [data-turn-key]').evaluate_all(
            "elements => elements.map(e => e.getAttribute('data-turn-key'))")
        if messages:
            if values:
                raise ValueError('Conversation history is ambiguous')
            if (not isinstance(messages, list) or len(messages) > 10_000
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get('id'), str)
                           or not item['id'].strip()
                           or not isinstance(item.get('role'), str)
                           or not item['role'].strip() for item in messages)):
                raise ValueError('Conversation history is ambiguous')
            ids = tuple(item['id'] for item in messages)
            if len(set(ids)) != len(ids):
                raise ValueError('Conversation history is ambiguous')
            users = tuple(item['id'] for item in messages if item['role'] == 'user')
            return ids, users, 'message_id'
        # The older Chat markup used a key on the turn container. Retain it for
        # existing browser fixtures when no role-bearing messages are present.
        if (not isinstance(values, list) or len(values) > 10_000
                or any(not isinstance(value, str) or not value.strip() for value in values)
                or len(set(values)) != len(values)):
            raise ValueError('Conversation history is ambiguous')
        baseline = tuple(values)
        return baseline, baseline, 'legacy_turn_key' if baseline else 'empty'

    async def _baseline_with_last_user(self, page: Page) -> tuple[tuple[str, ...], str | None]:
        baseline, users, _ = await self._baseline_snapshot(page)
        return baseline, users[-1] if users else None

    async def _baseline(self, page: Page) -> tuple[str, ...]:
        baseline, _, _ = await self._baseline_snapshot(page)
        return baseline

    def baseline_identity_kind(
        self, submission: SubchatSubmission,
    ) -> Literal['legacy_turn_key', 'message_id', 'empty']:
        if submission.operation_id not in self._prepared_baseline_kinds:
            raise ValueError('Prepared history identity is unavailable')
        return self._prepared_baseline_kinds[submission.operation_id]

    async def release_completed(self, submission: SubchatSubmission, *,
                                keep_for_queue: bool) -> None:
        """Release an owned tab after a durable final answer, respecting live aliases."""
        if submission.state != 'completed':
            raise ValueError('Only a completed submission can release its browser page')
        operation_id = submission.operation_id
        page = self.pages.get(operation_id)
        if page is not None:
            self._completed_page_owners.add(operation_id)
        if keep_for_queue:
            return
        url = ('https://chatgpt.com/c/' + submission.conversation_id
               if submission.conversation_id is not None else None)
        released: set[Page] = set()
        for owner, candidate in tuple(self.pages.items()):
            if owner in self._completed_page_owners and (
                    owner == operation_id or url is not None and candidate.url == url):
                self.pages.pop(owner)
                self._prepared_baseline_kinds.pop(owner, None)
                self._completed_page_owners.discard(owner)
                released.add(candidate)
        for candidate in released:
            if candidate not in self.pages.values():
                self._preparation_touched_pages.discard(candidate)
                self._unreusable_pages.discard(candidate)
                if not candidate.is_closed():
                    try:
                        await candidate.close()
                    except Exception as error:
                        # The answer is already durable; cleanup cannot undo it.
                        logger.warning('Completed browser page release failed error_type=%s',
                                       type(error).__name__)

    async def _ready(self, page: Page, submission: SubchatSubmission) -> bool:
        if page.url.rstrip('/') != self._url(submission).rstrip('/'):
            return False
        chat = page.get_by_role('button', name='Chat', exact=True)
        chat_radio = page.locator(
            '[role="radio"][data-tpp-toggle-value="chatgpt"][data-state="on"]'
            '[aria-checked="true"]')
        chat_radios = page.locator('[role="radio"][data-tpp-toggle-value="chatgpt"]')
        editor = page.locator(EDITOR)
        stop = page.get_by_role('button', name=re.compile(r'^(停止|Stop|Stop generating)$'))
        ordinary = (submission.requested_conversation_id is not None
                    or (await chat_radio.count() == 1 if await chat_radios.count() else
                        await chat.count() == 1
                        and await chat.get_attribute('aria-pressed') == 'true'))
        return (ordinary and await page.locator(COMPOSER).count() == 1
                and await editor.count() == 1 and not (await editor.inner_text()).strip()
                and await stop.filter(visible=True).count() == 0)

    async def _wait_for_composer(self, page: Page, submission: SubchatSubmission) -> None:
        """Wait for hydration, rejecting an occupied composer or active generation."""
        editor = page.locator(EDITOR)
        stop = page.get_by_role('button', name=re.compile(r'^(停止|Stop|Stop generating)$'))
        try:
            async with asyncio.timeout(10):
                while True:
                    if page.url.rstrip('/') != self._url(submission).rstrip('/'):
                        raise ValueError('Ordinary Chat conversation changed')
                    if await editor.count() == 1 and (await editor.inner_text()).strip():
                        raise ValueError('Ordinary Chat composer contains a draft')
                    if await stop.filter(visible=True).count():
                        raise ValueError('Ordinary Chat is generating')
                    if await self._ready(page, submission):
                        return
                    await asyncio.sleep(.1)
        except TimeoutError as error:
            raise ValueError(
                'Ordinary Chat with an idle empty composer was not confirmed') from error

    async def _wait_for_models(self, page: Page) -> dict[str, object]:
        """Observe a complete menu after its view changes, without guessing a model."""
        try:
            async with asyncio.timeout(10):
                while True:
                    observed: dict[str, object] = await page.evaluate(
                        SOURCE + '\nobserveSubchatModelMenu(document)')
                    state = observed.get('state')
                    if state == 'models_observed':
                        return observed
                    if state not in {'menu_unconfirmed', 'model_list_not_visible'}:
                        raise ValueError('Model menu structure is unsupported or ambiguous')
                    await asyncio.sleep(.1)
        except TimeoutError as error:
            raise ValueError('Model list did not become visible') from error

    async def _open_model_list(self, page: Page) -> dict[str, object]:
        """Distinguish a loading list from the observed effort view before toggling."""
        try:
            async with asyncio.timeout(10):
                while True:
                    observed: dict[str, object] = await page.evaluate(
                        SOURCE + '\nobserveSubchatModelMenu(document)')
                    state = observed.get('state')
                    if state == 'models_observed':
                        return observed
                    if state == 'model_list_not_visible' and await page.locator(CONTROL).count():
                        await self._click(page.locator(TOGGLE))
                        return await self._wait_for_models(page)
                    if state not in {'menu_unconfirmed', 'model_list_not_visible'}:
                        raise ValueError('Model menu structure is unsupported or ambiguous')
                    await asyncio.sleep(.1)
        except TimeoutError as error:
            raise ValueError('Model picker view did not become ready') from error

    def validate_send_selection(self, selection: SubchatHTTPSelection | None) -> None:
        """Check controller compatibility without opening a browser or saving a queue."""
        if self.http_read and selection is None:
            raise SubchatPreparationFailed('HTTP sends require an exact observed http_selection')
        if not self.http_read and selection is not None:
            raise SubchatPreparationFailed('HTTP selection requires the HTTP-read adapter')

    async def prepare(self, submission: SubchatSubmission) -> tuple[str, ...]:
        self.validate_send_selection(submission.http_selection)
        if self.http_read:
            assert submission.http_selection is not None
            require_http_selection(await self.http_catalog(), submission.http_selection)
        if submission.resources is not None and not self.http_read:
            raise ValueError('Resource sends require HTTP history verification')
        url = self._url(submission)
        # Only reuse tabs already owned by this adapter, never discover or claim
        # arbitrary user tabs. New conversations must always start separately.
        candidates = list(dict.fromkeys(
            page for page in self.pages.values()
            if submission.requested_conversation_id is not None
            and not page.is_closed() and page.url == url
            and page not in self._unreusable_pages))
        if len(candidates) > 1:
            raise ValueError('Multiple owned tabs match the requested conversation')
        page = candidates[0] if candidates else await self._new_page()
        previous = self.pages.get(submission.operation_id)
        previous_kind = self._prepared_baseline_kinds.pop(submission.operation_id, None)
        self.pages[submission.operation_id] = page
        try:
            baseline = await self._prepare_page(page, submission, url, reused=bool(candidates))
            self._preparation_touched_pages.discard(page)
            return baseline
        except BaseException:
            if page in self._preparation_touched_pages and candidates:
                self._unreusable_pages.add(page)
            self._preparation_touched_pages.discard(page)
            if previous is None:
                self.pages.pop(submission.operation_id, None)
            else:
                self.pages[submission.operation_id] = previous
                if previous_kind is not None:
                    self._prepared_baseline_kinds[submission.operation_id] = previous_kind
            if not candidates:
                await page.close()
            raise

    async def _prepare_page(self, page: Page, submission: SubchatSubmission,
                            url: str, *, reused: bool) -> tuple[str, ...]:
        page.set_default_timeout(15_000)
        if not reused:
            response = await page.goto(url, wait_until='domcontentloaded')
            if response is None or not response.ok:
                raise ConnectionError('Authenticated ordinary Chat is unavailable')
        if not await picker_ready(page):
            raise ConnectionError('Authenticated ordinary Chat is unavailable')
        await self._wait_for_composer(page, submission)
        self._preparation_touched_pages.add(page)
        await self._click(page.locator(TRIGGER))
        await page.get_by_role('menu').wait_for(state='visible', timeout=10_000)
        observed = await self._open_model_list(page)
        models = observed.get('models')
        if not isinstance(models, list):
            raise ValueError('Model list is unavailable')
        choices = [model for model in models if isinstance(model, dict)
                   and model.get('label') == submission.model and model.get('disabled') is False]
        if len(choices) != 1:
            raise ValueError('Requested model is not available in the observed menu')
        await self._click(page.get_by_role('menuitemradio', name=submission.model, exact=True))
        await page.locator(CONTROL).wait_for(state='visible')
        # Use verified arrow steps; custom sliders need not implement Home.
        async def read_effort() -> dict[str, object]:
            value: dict[str, object] = await page.evaluate(
                SOURCE + '\nobserveSubchatEffort(document)')
            return value

        async def step_effort(key: str) -> None:
            await self._press(page.locator(CONTROL), key)

        original = snapshot(await read_effort())
        for index in range(original[0], original[1] + 1):
            current = await move_effort(read_effort, step_effort, original, index)
            if matches_effort(current, submission.effort):
                break
        else:
            raise ValueError('Requested effort is not available in the observed menu')
        await self._click(page.locator(TOGGLE))
        selected = await self._wait_for_models(page)
        selected_models = selected.get('models')
        if (not isinstance(selected_models, list)
                or [model.get('label') for model in selected_models
                    if isinstance(model, dict) and model.get('selected') is True]
                != [submission.model]):
            raise ValueError('Selected model changed')
        await self._press(page.get_by_role('menu'), 'Escape')
        await self._wait_for_composer(page, submission)
        baseline, users, kind = await self._baseline_snapshot(page)
        last_user = users[-1] if users else None
        if (submission.expected_last_user_message_id is not None
                and last_user != submission.expected_last_user_message_id):
            raise SubchatStaleTarget('Queue target is stale; another turn has appeared')
        if submission.requested_conversation_id is None and baseline:
            raise ValueError('New Chat already contains messages')
        if submission.requested_conversation_id is not None and not baseline:
            raise ValueError('Existing conversation history is unavailable')
        self._prepared_baseline_kinds[submission.operation_id] = kind
        return baseline

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if (submission.resources is None and self._record_request is None
                and submission.http_selection is None):
            return await self._send(submission)
        page = self.pages.get(submission.operation_id)
        if page is None or page.is_closed():
            raise ValueError('Prepared browser page is unavailable')
        dispatched: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        request_started: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        claimed = False
        pattern = re.compile(r'^https://chatgpt\.com/backend-api/f/conversation(?:\?.*)?$')

        generation_client: httpx.AsyncClient | None = None
        generation_account: str | None = None
        generation_authorization: str | None = None
        if self._httpx_generation:
            from ..subchat_chrome_login import chrome_http_session

            generation_client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                                  transport=httpx.AsyncHTTPTransport(retries=0))
            try:
                session = await chrome_http_session(
                    await self._browser(), generation_client,
                    expected_account_id=self._expected_account_id,
                    page_factory=self._new_page if self._background_pages else None)
                generation_account = session.account_id
                self._http_reader.bind_verified_account(
                    await self._browser(), generation_account)
                generation_authorization = session.authorization.get_secret_value()
            except BaseException:
                await generation_client.aclose()
                raise

        async def augment(route: Route) -> None:
            nonlocal claimed
            if claimed or route.request.method != 'POST':
                await route.abort()
                return
            claimed = True
            if not request_started.done():
                request_started.set_result(True)
            accepted = False
            stage = 'request_validation'
            try:
                payload = route.request.post_data
                if payload is None:
                    raise ValueError('Missing generation payload')
                body = generation_input(payload, submission)
                messages = body['messages']
                assert isinstance(messages, list) and isinstance(messages[0], dict)
                identity = messages[0]['id']
                assert isinstance(identity, str)
                outgoing = (add_resources(payload, submission)
                            if submission.resources is not None else payload)
                if self._record_request is not None:
                    stage = 'account_binding'
                    account = await route.request.header_value('chatgpt-account-id')
                    if generation_account is not None:
                        if account is not None and account != generation_account:
                            raise ValueError('Generation account identity changed')
                        account = generation_account
                    elif account is None or not account.strip() or len(account) > 256:
                        raise ValueError('Generation account identity is unavailable')
                    else:
                        self._http_reader.check_generation_account(account)
                    self._record_request(submission.operation_id, identity, account)
                if generation_client is None or generation_authorization is None:
                    stage = 'browser_transport'
                    await route.continue_(post_data=outgoing)
                else:
                    from .httpx_generation import post_browser_prepared_once

                    stage = 'httpx_transport'
                    response = await post_browser_prepared_once(
                        route.request, generation_client,
                        authorization=generation_authorization,
                        content=outgoing.encode('utf-8'))
                    stage = 'browser_delivery'
                    if response.status == 200 and response.content_type == 'text/event-stream':
                        await route.fulfill(status=200,
                                            headers={'content-type': 'text/event-stream'},
                                            body=response.body)
                    else:
                        await route.fulfill(status=response.status,
                                            headers={'content-type': 'application/json'},
                                            body=b'{}')
                accepted = True
            except Exception as error:
                # Provider details can contain account information; do not expose them.
                logger.warning('Subchat generation stage=%s error_type=%s',
                               stage, type(error).__name__)
                frames = traceback.extract_tb(error.__traceback__)
                logger.warning('Subchat generation failure_path=%s',
                               '>'.join(f'{Path(frame.filename).name}:{frame.lineno}'
                                        for frame in frames[-5:]))
                if str(error).startswith('Invalid browser request header types: '):
                    logger.warning('%s', error)
                await route.abort()
                if (stage in {'request_validation', 'account_binding'}
                        and self._record_preflight_failure is not None):
                    self._record_preflight_failure(submission.operation_id)
            finally:
                if not dispatched.done():
                    dispatched.set_result(accepted)

        route_installed = False
        try:
            if self._record_conversation is not None or self._record_rejection is not None:
                binding = 'ac_stream_' + submission.operation_id

                def observed(source: dict[str, object], message: str,
                             conversation: str | None, account: str,
                             status: int | None = None) -> None:
                    if source.get('page') is not page or source.get('frame') is not page.main_frame:
                        raise ValueError('Stream observation came from another frame')
                    if status is not None:
                        if self._record_rejection is not None:
                            self._record_rejection(submission.operation_id, message,
                                                   status, account)
                        return
                    if (not isinstance(conversation, str)
                            or CHAT.fullmatch('https://chatgpt.com/c/' + conversation) is None):
                        raise ValueError('Invalid stream conversation identity')
                    if self._record_conversation is not None:
                        self._record_conversation(
                            submission.operation_id, message, conversation, account)

                await page.expose_binding(binding, observed)
                await page.evaluate(STREAM + '\n(args)=>observeSubchatStream(...args)',
                                    [binding, generation_account])

            await page.route(pattern, augment)
            route_installed = True
            receipt = await self._send(submission)
            await asyncio.wait_for(asyncio.shield(request_started), 10)
            # Keep the HTTPX client and browser route alive for the complete
            # stream. A valid slow generation is not a failed dispatch.
            if not await asyncio.shield(dispatched):
                raise ValueError('Generation resource request was not confirmed')
            return receipt
        finally:
            try:
                if route_installed:
                    await page.unroute(pattern, augment)
            finally:
                if generation_client is not None:
                    await generation_client.aclose()

    async def _send(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if submission.state != 'sending':
            raise ValueError('Draft exposure requires a reserved submission')
        page = self.pages.get(submission.operation_id)
        if page is None or page.is_closed():
            raise ValueError('Prepared browser page is unavailable')
        if page.url.rstrip('/') != self._url(submission).rstrip('/'):
            raise ValueError('Prepared conversation changed before send')
        editor = page.locator(EDITOR)
        if await editor.count() != 1 or (await editor.inner_text()).strip():
            raise ValueError('Prepared draft changed before send')
        baseline, _, kind = await self._baseline_snapshot(page)
        if (baseline != submission.baseline_message_ids
                or (submission.baseline_identity_kind is not None
                    and kind != submission.baseline_identity_kind)):
            raise ValueError('Conversation history changed before send')
        stop = page.get_by_role('button', name=re.compile(r'^(停止|Stop|Stop generating)$'))
        if await stop.filter(visible=True).count():
            raise ValueError('Chat started generating; do not silently queue the draft')
        # A user can send as soon as text appears. The service has already saved
        # the reservation and baseline, so interruption here cannot permit replay.
        if self._background_pages:
            from .background import background_focus_editor

            await background_focus_editor(editor)
        else:
            await editor.click()
        guard = await page.evaluate_handle(
            INPUT + '\ntext=>insertObservedSubchatDraft(document,text)', submission.prompt)
        try:
            sent = await guard.evaluate(
                INPUT + '\n(guard,args)=>guard.draft.state === "draft_observed" && '
                '!guard.intervened && submitSubchatDraft(document,...args)',
                [self._url(submission), submission.prompt, list(submission.baseline_message_ids)])
        finally:
            try:
                if not page.is_closed():
                    await guard.evaluate('guard=>guard.stop()')
            finally:
                await guard.dispose()
        if not sent:
            raise ValueError('Draft or conversation changed before dispatch; '
                             'recover without replay')
        # Release the caller's browser lock after one observation. An unavailable
        # receipt is durable 'sending', never permission to click Send again.
        if (submission.resources is not None or self._record_request is not None
                or submission.http_selection is not None):
            # HTTP input verification belongs to recover, not the dispatch deadline.
            return None
        return await self.find_submission(submission)

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if (self.http_read and submission.conversation_id is not None
                and submission.user_message_id is not None):
            if CHAT.fullmatch('https://chatgpt.com/c/' + submission.conversation_id) is None:
                raise ValueError('Invalid conversation identity')
            async with asyncio.timeout(20):
                return await self._http_reader.receipt(await self._read_context(), submission)
        page = await self._page(submission)
        if page is None:
            return None
        match = CHAT.fullmatch(page.url)
        if match is None:
            return None
        if submission.conversation_id is not None and match[1] != submission.conversation_id:
            raise ValueError('Browser conversation changed')
        if self.http_read and submission.user_message_id is not None:
            candidate = submission.model_copy(update={'conversation_id': match[1]})
            async with asyncio.timeout(20):
                return await self._http_reader.receipt(await self._browser(), candidate)
        if submission.resources is not None:
            _, users, kind = await self._baseline_snapshot(page)
            expected_kind = submission.baseline_identity_kind
            if (expected_kind == 'empty' and submission.baseline_message_ids):
                return None
            if (expected_kind not in {kind, 'empty'}
                    or (expected_kind == 'empty'
                        and submission.requested_conversation_id is not None)):
                return None
            candidates = [key for key in users
                          if key not in submission.baseline_message_ids]
            if len(candidates) != 1:
                return None
            candidate = submission.model_copy(update={
                'conversation_id': match[1], 'user_message_id': candidates[0]})
            async with asyncio.timeout(20):
                return await self._http_reader.receipt(await self._browser(), candidate)
        observed = await page.evaluate(
            COPY + '\nargs=>recoverSubchatSubmission(document,...args)',
            [match[1], submission.wire_prompt, list(submission.baseline_message_ids),
             submission.baseline_identity_kind])
        if observed.get('state') != 'submission_observed':
            return None
        return SubchatReceipt(conversation_id=match[1],
                              user_message_id=observed['user_message_id'], prompt=submission.prompt)

    async def read_answer(self, submission: SubchatSubmission
                          ) -> SubchatAnswer | SubchatPendingObservation | None:
        if self.http_read:
            if (submission.conversation_id is None or submission.user_message_id is None
                    or CHAT.fullmatch(
                        'https://chatgpt.com/c/' + submission.conversation_id) is None):
                return None
            async with asyncio.timeout(20):
                return await self._http_reader.history(await self._read_context(), submission)
        page = await self._page(submission)
        if page is None or submission.user_message_id is None:
            return None
        user = await page.evaluate(COPY + '\nargs=>copySubchatUserText(document,...args)',
                                   [submission.conversation_id, submission.user_message_id])
        if user.get('state') != 'user_text_observed' or user['text'] != submission.prompt:
            return None
        # The provider can pause an accepted ordinary-Chat request at a Work
        # handoff choice. Keep this request in Chat; never resend or select Work.
        stay = page.get_by_role('button', name=re.compile(
            r'^(Chat\s*に留まる|Stay in Chat)(?:\s|$)')).filter(visible=True)
        work = page.get_by_role('button', name=re.compile(
            r'^(Work\s*で続ける|Continue in Work)$')).filter(visible=True)
        if (await stay.count() == 1 and await work.count() == 1
                and await stay.is_enabled()
                and (await self._baseline_with_last_user(page))[1]
                    == submission.user_message_id
                and page.url == 'https://chatgpt.com/c/' + str(submission.conversation_id)):
            await self._click(stay)
            return None  # Observe completion on a later poll, not from the click.
        answer = await page.evaluate(COPY + '\nargs=>copySubchatMessageText(document,...args)',
                                     [submission.conversation_id, submission.user_message_id,
                                      'assistant'])
        if answer.get('state') != 'answer_text_observed':
            return None
        return SubchatAnswer(conversation_id=answer['conversation_id'],
                             user_message_id=answer['user_message_id'], prompt=submission.prompt,
                             answer_message_id='dom-content-unit:' + answer['answer_reference'],
                             text=answer['text'])
