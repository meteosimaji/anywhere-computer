"""Exact copy-text receipt without touching the system clipboard."""
from pathlib import Path

import pytest


async def test_copy_reads_selected_message_and_restores_clipboard():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] /
              "src/anywhere_computer/subchat_browser/subchat_copy.js").read_text()
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
            await page.evaluate('''() => {
                document.querySelector('[data-turn-key]').insertAdjacentHTML('beforeend',
                    '<div data-content-search-unit-key="unit:2:assistant">' +
                    '<div data-markdown-text-style="assistant-message">answer</div></div>' +
                    '<div class="turn-action-controls"><button aria-label="Copy">copy</button>' +
                    '<button aria-label="Regenerate response">regenerate</button></div>');
                window.originalRichWrite = async () => { window.osWrites++; };
                Object.defineProperty(navigator.clipboard, 'write',
                    {configurable: true, value: window.originalRichWrite});
                document.querySelector('[aria-label="Copy"]').onclick = () =>
                    navigator.clipboard.write([new ClipboardItem({
                        'text/plain': new Blob(['```answer```'], {type:'text/plain'})})]);
            }''')
            async def answer():
                return await page.evaluate(source + '''\n() =>
                    copySubchatMessageText(document,"conversation","user","assistant")''')
            assert (await answer())['text'] == '```answer```'
            assert await page.evaluate('window.osWrites') == 0
            assert await page.evaluate('navigator.clipboard.write === window.originalRichWrite')
            await page.evaluate('''() => {
                const stop = document.createElement('button');
                stop.setAttribute('aria-label', 'Stop generating');
                stop.textContent = 'stop'; document.body.append(stop);
            }''')
            assert await answer() == {'state': 'message_unconfirmed'}
            await page.evaluate("document.querySelector('[aria-label=\"Stop generating\"]')"
                                ".remove()")
            await page.evaluate('''() => {
                document.querySelector('.turn-action-controls').remove();
                document.querySelector('[data-content-search-unit-key]').remove();
            }''')
            recovered = await page.evaluate(source + '''\ntext =>
                recoverSubchatSubmission(document,"conversation",text,[])''', text)
            assert recovered == {'state': 'submission_observed',
                                 'conversation_id': 'conversation', 'user_message_id': 'user'}
            assert await page.evaluate(source + '''\ntext =>
                recoverSubchatSubmission(document,"conversation",text,["user"])''', text) == {
                    'state': 'submission_unconfirmed'}
            await page.evaluate('''text => {
                const duplicate = document.querySelector('[data-turn-key]').cloneNode(true);
                duplicate.setAttribute('data-turn-key', 'duplicate');
                duplicate.querySelector('button').onclick = () =>
                    navigator.clipboard.writeText(text);
                document.querySelector('main').append(duplicate);
            }''', text)
            assert await page.evaluate(source + '''\ntext =>
                recoverSubchatSubmission(document,"conversation",text,[])''', text) == {
                    'state': 'submission_unconfirmed'}
            await page.evaluate("document.querySelector('[data-turn-key=duplicate]').remove()")
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
