"""Receipt extraction in a real DOM, without an account or network access."""
from pathlib import Path

import pytest


async def test_submission_identity_uses_persisted_chat_and_user_bubble():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] / 'scripts/subchat_submission.js').read_text(encoding="utf-8")
    conversation = '11111111-2222-3333-4444-555555555555'
    url = f'https://chatgpt.com/c/{conversation}'
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome is required for the optional real DOM check')
            raise
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body='<main></main>'))
            await page.goto(url)

            def turn(key, prompt='request'):
                return (f'<div data-turn-key="{key}">'
                        f'<div data-user-message-bubble="true">{prompt}</div>'
                        '<div>request</div></div>')

            async def observe(html, baseline=None, prompt='request'):
                await page.set_content(html)
                return await page.evaluate(
                    source + '\n(args) => observeSubchatSubmission(document, ...args)',
                    [prompt, baseline if baseline is not None else [], conversation],
                )

            receipt = {'state': 'submission_observed', 'conversation_id': conversation,
                       'user_message_id': 'new'}
            assert await observe('<main>' + turn('new') + '</main>') == receipt
            assert await observe('<main>' + turn('old') + turn('new') + '</main>',
                                 ['old']) == receipt
            assert await observe('<main>' + turn('new', 'different') + '</main>') == {
                'state': 'submission_unconfirmed'}
            assert await observe('<main>' + turn('new') + '</main>', ['new']) == {
                'state': 'submission_unconfirmed'}
            assert await observe('<main>' + turn('first') + turn('new') + '</main>') == {
                'state': 'submission_unconfirmed'}
            assert await observe('<main>' + turn('new') * 2 + '</main>') == {
                'state': 'ambiguous_turns'}
            assert await observe('<main><div data-turn-key="new">'
                                 + turn('nested') + '</div></main>') == {
                'state': 'unsupported_turn'}
            assert await observe('<main><div data-turn-key="new">'
                                 '<div>request</div></div></main>') == {
                'state': 'unsupported_turn'}
            assert await observe('<main></main><main></main>') == {
                'state': 'transcript_unconfirmed'}
            assert await observe('<main></main>', ['old', 'old']) == {'state': 'invalid_baseline'}
            for destination in [
                'https://chatgpt.com/c/local-chatgpt%3A' + conversation,
                'https://chatgpt.com/', url + '?other=1', url + '#other',
                'https://chatgpt.com/c/99999999-2222-3333-4444-555555555555',
                'https://example.com/c/' + conversation,
            ]:
                await page.goto(destination)
                assert await observe('<main>' + turn('new') + '</main>') == {
                    'state': 'conversation_unconfirmed'}
        finally:
            await browser.close()
