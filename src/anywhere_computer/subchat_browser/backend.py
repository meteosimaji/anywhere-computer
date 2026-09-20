"""Ordinary Chat adapter using an explicitly supplied dedicated browser context.

Used by the local stdio MCP entry. Existing-conversation sends checkpoint
visible message identities before dispatch; see the dated acceptance records.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from anywhere_computer.subchat import SubchatAnswer, SubchatReceipt, SubchatStaleTarget
from anywhere_computer.subchat_state import SubchatSubmission

from .catalog import CONTROL, SOURCE, TOGGLE, TRIGGER, collect_http_page, collect_page, picker_ready
from .efforts import move_effort, snapshot

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

INPUT = Path(__file__).with_name('subchat_input.js').read_text(encoding="utf-8")
COPY = Path(__file__).with_name('subchat_copy.js').read_text(encoding="utf-8")
CHAT = re.compile(r'https://chatgpt\.com/c/'
                  r'([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\Z')


class BrowserSubchatBackend:
    def __init__(self, context: BrowserContext | Callable[[], Awaitable[BrowserContext]]) -> None:
        self._context = None if callable(context) else context
        self._create_context = context if callable(context) else None
        self._context_lock = asyncio.Lock()
        self.pages: dict[str, Page] = {}

    async def _browser(self) -> BrowserContext:
        async with self._context_lock:
            if self._context is None:
                assert self._create_context is not None
                self._context = await self._create_context()
            return self._context

    async def catalog(self, model: str | None = None) -> dict[str, object]:
        page = await (await self._browser()).new_page()
        page.set_default_timeout(15_000)
        try:
            response = await page.goto('https://chatgpt.com/', wait_until='domcontentloaded')
            if response is None or not response.ok or not await picker_ready(page):
                return {'state': 'catalog_unavailable', 'submitted': False}
            return await collect_page(page, model)
        finally:
            # This is a separate observation tab, never a submission or user draft.
            await page.close()

    async def http_catalog(self) -> dict[str, object]:
        page = None
        try:
            async with asyncio.timeout(20):
                page = await (await self._browser()).new_page()
                return await collect_http_page(page)
        finally:
            if page is not None:
                # Leave cleanup headroom under the outer direct-MCP deadline.
                await asyncio.wait_for(page.close(), timeout=5)

    async def _page(self, submission: SubchatSubmission) -> Page | None:
        page = self.pages.get(submission.operation_id)
        if page is not None and not page.is_closed():
            return page
        if submission.conversation_id is None:
            return None
        page = await (await self._browser()).new_page()
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

    async def _baseline(self, page: Page) -> tuple[str, ...]:
        if await page.locator('main').count() != 1:
            raise ValueError('Conversation history unavailable')
        values = await page.locator('main [data-turn-key]').evaluate_all(
            "elements => elements.map(e => e.getAttribute('data-turn-key'))")
        if (not isinstance(values, list) or len(values) > 10_000
                or any(not isinstance(value, str) or not value.strip() for value in values)
                or len(set(values)) != len(values)):
            raise ValueError('Conversation history is ambiguous')
        return tuple(values)

    async def _ready(self, page: Page, submission: SubchatSubmission) -> bool:
        if page.url.rstrip('/') != self._url(submission).rstrip('/'):
            return False
        chat = page.get_by_role('button', name='Chat', exact=True)
        editor = page.locator('[data-composer-markdown][role="textbox"]')
        stop = page.get_by_role('button', name=re.compile(r'^(停止|Stop|Stop generating)$'))
        ordinary = (submission.requested_conversation_id is not None
                    or (await chat.count() == 1
                        and await chat.get_attribute('aria-pressed') == 'true'))
        return (ordinary and await page.locator('form[data-chatgpt-composer]').count() == 1
                and await editor.count() == 1 and not (await editor.inner_text()).strip()
                and await stop.filter(visible=True).count() == 0)

    async def prepare(self, submission: SubchatSubmission) -> tuple[str, ...]:
        url = self._url(submission)
        # Only reuse tabs already owned by this adapter, never discover or claim
        # arbitrary user tabs. New conversations must always start separately.
        candidates = list(dict.fromkeys(
            page for page in self.pages.values()
            if submission.requested_conversation_id is not None
            and not page.is_closed() and page.url == url))
        if len(candidates) > 1:
            raise ValueError('Multiple owned tabs match the requested conversation')
        page = candidates[0] if candidates else await (await self._browser()).new_page()
        self.pages[submission.operation_id] = page
        page.set_default_timeout(15_000)
        if not candidates:
            response = await page.goto(url, wait_until='domcontentloaded')
            if response is None or not response.ok:
                raise ConnectionError('Authenticated ordinary Chat is unavailable')
        if not await picker_ready(page):
            raise ConnectionError('Authenticated ordinary Chat is unavailable')
        if not await self._ready(page, submission):
            raise ValueError('Ordinary Chat with an idle empty composer was not confirmed')
        await page.locator(TRIGGER).click()
        observed = await page.evaluate(SOURCE + '\nobserveSubchatModelMenu(document)')
        if observed['state'] == 'model_list_not_visible':
            await page.locator(TOGGLE).click()
            observed = await page.evaluate(SOURCE + '\nobserveSubchatModelMenu(document)')
        choices = [model for model in observed.get('models', [])
                   if model['label'] == submission.model and not model['disabled']]
        if len(choices) != 1:
            raise ValueError('Requested model is not available in the observed menu')
        await page.get_by_role('menuitemradio', name=submission.model, exact=True).click()
        await page.locator(CONTROL).wait_for(state='visible')
        # Use verified arrow steps; custom sliders need not implement Home.
        async def read_effort() -> dict[str, object]:
            value: dict[str, object] = await page.evaluate(
                SOURCE + '\nobserveSubchatEffort(document)')
            return value

        async def step_effort(key: str) -> None:
            await page.locator(CONTROL).press(key)

        original = snapshot(await read_effort())
        for index in range(original[0], original[1] + 1):
            current = await move_effort(read_effort, step_effort, original, index)
            if current[3] == submission.effort:
                break
        else:
            raise ValueError('Requested effort is not available in the observed menu')
        await page.locator(TOGGLE).click()
        selected = await page.evaluate(SOURCE + '\nobserveSubchatModelMenu(document)')
        if [model['label'] for model in selected.get('models', []) if model['selected']] != [
            submission.model
        ]:
            raise ValueError('Selected model changed')
        await page.get_by_role('menu').press('Escape')
        if not await self._ready(page, submission):
            raise ValueError('Chat changed before input')
        baseline = await self._baseline(page)
        if (submission.expected_last_user_message_id is not None
                and (not baseline or baseline[-1] != submission.expected_last_user_message_id)):
            raise SubchatStaleTarget('Queue target is stale; another turn has appeared')
        if submission.requested_conversation_id is None and baseline:
            raise ValueError('New Chat already contains messages')
        return baseline

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if submission.state != 'sending':
            raise ValueError('Draft exposure requires a reserved submission')
        page = self.pages.get(submission.operation_id)
        if page is None or page.is_closed():
            raise ValueError('Prepared browser page is unavailable')
        if page.url.rstrip('/') != self._url(submission).rstrip('/'):
            raise ValueError('Prepared conversation changed before send')
        editor = page.locator('[data-composer-markdown][role="textbox"]')
        if await editor.count() != 1 or (await editor.inner_text()).strip():
            raise ValueError('Prepared draft changed before send')
        if await self._baseline(page) != submission.baseline_message_ids:
            raise ValueError('Conversation history changed before send')
        stop = page.get_by_role('button', name=re.compile(r'^(停止|Stop|Stop generating)$'))
        if await stop.filter(visible=True).count():
            raise ValueError('Chat started generating; do not silently queue the draft')
        # A user can send as soon as text appears. The service has already saved
        # the reservation and baseline, so interruption here cannot permit replay.
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
        return await self.find_submission(submission)

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        page = await self._page(submission)
        if page is None:
            return None
        match = CHAT.fullmatch(page.url)
        if match is None:
            return None
        if submission.conversation_id is not None and match[1] != submission.conversation_id:
            raise ValueError('Browser conversation changed')
        observed = await page.evaluate(
            COPY + '\nargs=>recoverSubchatSubmission(document,...args)',
            [match[1], submission.prompt, list(submission.baseline_message_ids)])
        if observed.get('state') != 'submission_observed':
            return None
        return SubchatReceipt(conversation_id=match[1],
                              user_message_id=observed['user_message_id'], prompt=submission.prompt)

    async def read_answer(self, submission: SubchatSubmission) -> SubchatAnswer | None:
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
                and (await self._baseline(page))[-1:] == (submission.user_message_id,)
                and page.url == 'https://chatgpt.com/c/' + str(submission.conversation_id)):
            await stay.click()
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
