"""Development adapter using an explicitly supplied dedicated browser context.

Not a registered production backend. Existing-conversation sends checkpoint
visible message identities before dispatch; live follow-up acceptance is pending.
"""
import asyncio
import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from probe_subchat_catalog import CONTROL, SOURCE, TOGGLE, TRIGGER, picker_ready

from anywhere_computer.subchat import SubchatAnswer, SubchatReceipt
from anywhere_computer.subchat_state import SubchatSubmission

INPUT = Path(__file__).with_name('subchat_input.js').read_text()
COPY = Path(__file__).with_name('subchat_copy.js').read_text()
CHAT = re.compile(r'https://chatgpt\.com/c/'
                  r'([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\Z')


class BrowserSubchatBackend:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self.pages: dict[str, Page] = {}

    async def _page(self, submission: SubchatSubmission) -> Page | None:
        page = self.pages.get(submission.operation_id)
        if page is not None and not page.is_closed():
            return page
        if submission.conversation_id is None:
            return None
        page = await self.context.new_page()
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
        return (await chat.count() == 1 and await chat.get_attribute('aria-pressed') == 'true'
                and await editor.count() == 1 and not (await editor.inner_text()).strip()
                and await stop.filter(visible=True).count() == 0)

    async def prepare(self, submission: SubchatSubmission) -> tuple[str, ...]:
        url = self._url(submission)
        page = await self.context.new_page()
        self.pages[submission.operation_id] = page
        page.set_default_timeout(15_000)
        response = await page.goto(url, wait_until='domcontentloaded')
        if response is None or not response.ok or not await picker_ready(page):
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
        # Use the account's observed description, without a model or effort table.
        await page.locator(CONTROL).press('Home')
        for _ in range(32):
            effort = await page.evaluate(SOURCE + '\nobserveSubchatEffort(document)')
            if effort.get('state') != 'effort_observed' or effort['disabled']:
                raise ValueError('Effort selection is unavailable')
            if effort['description'] == submission.effort:
                break
            if effort['index'] == effort['maximum']:
                raise ValueError('Requested effort is not available in the observed menu')
            await page.locator(CONTROL).press('ArrowRight')
        else:
            raise ValueError('Effort menu exceeded its observation bound')
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
        if submission.requested_conversation_id is None and baseline:
            raise ValueError('New Chat already contains messages')
        editor = page.locator('[data-composer-markdown][role="textbox"]')
        await editor.click()
        draft = await page.evaluate(INPUT + '\ntext=>insertSubchatDraft(document,text)',
                                    submission.prompt)
        if draft.get('state') != 'draft_observed' or await editor.inner_text() != submission.prompt:
            raise ValueError('Exact draft was not confirmed')
        return baseline

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt:
        page = self.pages.get(submission.operation_id)
        if page is None or page.is_closed():
            raise ValueError('Prepared browser page is unavailable')
        if page.url.rstrip('/') != self._url(submission).rstrip('/'):
            raise ValueError('Prepared conversation changed before send')
        editor = page.locator('[data-composer-markdown][role="textbox"]')
        if await editor.count() != 1 or await editor.inner_text() != submission.prompt:
            raise ValueError('Prepared draft changed before send')
        if await self._baseline(page) != submission.baseline_message_ids:
            raise ValueError('Conversation history changed before send')
        await page.get_by_role('button', name=re.compile(r'^(送信|Send)$')).click()
        async with asyncio.timeout(120):
            while True:
                receipt = await self.find_submission(submission)
                if receipt is not None:
                    return receipt
                await asyncio.sleep(1)

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
        answer = await page.evaluate(COPY + '\nargs=>copySubchatMessageText(document,...args)',
                                     [submission.conversation_id, submission.user_message_id,
                                      'assistant'])
        if answer.get('state') != 'answer_text_observed':
            return None
        return SubchatAnswer(conversation_id=answer['conversation_id'],
                             user_message_id=answer['user_message_id'], prompt=submission.prompt,
                             answer_message_id='dom-content-unit:' + answer['answer_reference'],
                             text=answer['text'])
