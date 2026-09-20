"""Exact copy-text receipt without touching the system clipboard."""
from pathlib import Path

import pytest


async def test_copy_reads_selected_message_and_restores_clipboard():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] / 'scripts/subchat_copy.js').read_text()
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required')
            raise
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(
                content_type='text/html', body='<main><div data-turn-key="user">'
                '<div data-user-message-bubble="true">rendered code</div>'
                '<button aria-label="Copy message">copy</button></div></main>'))
            await page.goto('https://chatgpt.com/c/conversation')
            text = '```python\nprint("日本語 🚀")\n```'
            await page.evaluate('''text => {
                window.osWrites = 0;
                window.originalWrite = async () => { window.osWrites++; };
                Object.defineProperty(navigator.clipboard, 'writeText',
                    {configurable: true, value: window.originalWrite});
                document.querySelector('button').onclick = () =>
                    navigator.clipboard.writeText(text);
            }''', text)
            async def read(user='user'):
                return await page.evaluate(
                    source + '\nuser => copySubchatUserText(document,"conversation",user)', user)
            assert await read() == {'state': 'user_text_observed',
                                    'conversation_id': 'conversation',
                                    'user_message_id': 'user', 'text': text}
            assert await page.evaluate('window.osWrites') == 0
            assert await page.evaluate('navigator.clipboard.writeText === window.originalWrite')
            recovered = await page.evaluate(source + '''\ntext =>
                recoverSubchatSubmission(document,"conversation",text,[])''', text)
            assert recovered == {'state': 'submission_observed',
                                 'conversation_id': 'conversation', 'user_message_id': 'user'}
            assert await page.evaluate(source + '''\ntext =>
                recoverSubchatSubmission(document,"conversation",text,["user"])''', text) == {
                    'state': 'submission_unconfirmed'}
            await page.evaluate('''() => {
                const original = document.querySelector('button').onclick;
                document.querySelector('button').onclick = () => {
                    original();
                    document.querySelector('[data-turn-key]').setAttribute('data-turn-key','other');
                };
            }''')
            assert await read() == {'state': 'message_unconfirmed'}
            await page.evaluate("document.querySelector('[data-turn-key]')"
                                ".setAttribute('data-turn-key','user')")
            assert await read('missing') == {'state': 'message_unconfirmed'}
            await page.evaluate('document.querySelector("button").onclick = () => {}')
            assert await read() == {'state': 'message_unconfirmed'}
            assert await page.evaluate('navigator.clipboard.writeText === window.originalWrite')
        finally:
            await browser.close()
