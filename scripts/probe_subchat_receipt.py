"""Read a submitted ordinary Chat's DOM identity; never send or certify completion."""
import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import Error, async_playwright
from probe_subchat_catalog import minimize_window

from anywhere_computer.subchat_browser.backend import COPY as COPY_SOURCE

SOURCE = Path(__file__).with_name('subchat_submission.js').read_text()

CONVERSATION = re.compile(
    r'https://chatgpt\.com/c/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z')


async def probe(profile: Path, url: str, prompt: str, previous_ids: list[str],
                copy_message: bool = False) -> dict[str, object]:
    match = CONVERSATION.fullmatch(url)
    if match is None:
        raise ValueError('A persisted ordinary Chat URL is required')
    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(profile), channel='chrome', headless=False, args=['--start-minimized'])
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(10_000)
            session = await context.new_cdp_session(page)
            window = await session.send('Browser.getWindowForTarget')
            if not await minimize_window(session, window['windowId']):
                return {'state': 'minimization_unconfirmed'}
            response = await page.goto(url, wait_until='domcontentloaded')
            if response is None or not response.ok:
                return {'state': 'page_unavailable',
                        'http_status': response.status if response else None}
            # Missing rows may be loading, logged out, virtualized, or changed UI.
            # None of these is evidence that the prompt was not submitted.
            await page.locator('main [data-turn-key]').first.wait_for(state='attached')
            if copy_message:
                copied: dict[str, object] = await page.evaluate(
                    COPY_SOURCE + '\n(args) => recoverSubchatSubmission(document, ...args)',
                    [match[1], prompt, previous_ids],
                )
                return copied
            result: dict[str, object] = await page.evaluate(
                SOURCE + '\n(args) => observeSubchatSubmission(document, ...args)',
                [prompt, previous_ids, match[1]],
            )
            return result
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--conversation-url', required=True)
    parser.add_argument('--expected-prompt-file', type=Path, required=True)
    parser.add_argument('--previous-user-id', action='append', default=[])
    parser.add_argument('--copy-message', action='store_true',
                        help='Use message Copy actions to recover original Markdown text')
    args = parser.parse_args()
    if CONVERSATION.fullmatch(args.conversation_url) is None:
        parser.error('Use a persisted https://chatgpt.com/c/ conversation URL')
    prompt = args.expected_prompt_file.read_text(encoding='utf-8')
    if not prompt:
        parser.error('The expected prompt must not be empty')
    try:
        result = asyncio.run(probe(args.profile.resolve(), args.conversation_url,
                                   prompt, args.previous_user_id, args.copy_message))
    except (Error, ConnectionError) as error:
        result = {'state': 'submission_unconfirmed', 'error_type': type(error).__name__}
    # Even an observed user message does not prove the assistant has finished.
    print(json.dumps({**result, 'resend': False, 'completion_confirmed': False}))
    if result['state'] != 'submission_observed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
