"""Native browser editing contract; authenticated serialization is a separate gate."""
from pathlib import Path

import pytest


async def test_literal_draft_preserves_text_and_rejects_existing_content():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] /
              "src/anywhere_computer/subchat_browser/subchat_input.js").read_text(encoding="utf-8")
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome is required for the optional real DOM check')
            raise
        try:
            page = await browser.new_page()
            await page.set_content('<form data-chatgpt-composer>'
                                   '<div data-composer-markdown role="textbox" '
                                   'contenteditable="true"></div></form>')
            editor = page.get_by_role('textbox')
            # Native insertion stays literal. Provider-specific empty paragraph
            # fillers and consecutive blank lines are checked separately below.
            prompt = '日本語 🚀\n```python\nprint("<tag>", "*x*", "a_b")\n```\n- item'

            async def insert(text):
                return await page.evaluate(
                    source + '\ntext => insertSubchatDraft(document, text)', text)

            assert (await insert(prompt))['input_dispatched'] is False
            await editor.click()
            assert await insert(prompt) == {
                'state': 'draft_observed', 'input_dispatched': True, 'submitted': False}
            assert await editor.inner_text() == prompt
            assert await editor.locator('tag').count() == 0
            assert (await insert('replacement'))['input_dispatched'] is False
            assert await editor.inner_text() == prompt
            assert await insert('a\r\nb') == {
                'state': 'invalid_input', 'input_dispatched': False}
            await editor.fill('')
            await editor.click()
            guard = await page.evaluate_handle(
                source + '\n() => insertObservedSubchatDraft(document,"send once")')
            try:
                await page.evaluate('''() => {
                    const form = document.querySelector('form');
                    const accessory = document.createElement('button');
                    accessory.type = 'button'; accessory.textContent = 'Tools';
                    form.append(accessory);
                    accessory.dispatchEvent(new KeyboardEvent('keydown',
                        {key:'Enter', bubbles:true}));
                    const editor = document.querySelector('[role=textbox]');
                    for (const options of [{shiftKey:true}, {isComposing:true}])
                        editor.dispatchEvent(new KeyboardEvent('keydown',
                            {key:'Enter', bubbles:true, ...options}));
                }''')
                assert not await guard.evaluate('guard => guard.intervened')
                await editor.press('Enter')
                assert await guard.evaluate('guard => guard.intervened')
                await guard.evaluate('guard => {guard.stop(); guard.intervened=false}')
                await editor.press('Enter')
                assert not await guard.evaluate('guard => guard.intervened')
            finally:
                await guard.evaluate('guard => guard.stop()')
                await guard.dispose()
            # Real Chat decorates URLs with a noneditable icon. Its block layout
            # changes innerText, although the saved input retains the literal URL.
            await page.set_content('''<main></main><form data-chatgpt-composer>
                <div data-composer-markdown role="textbox" contenteditable="true">
                </div><button type="button" aria-label="Send"
                onclick="window.sends=(window.sends||0)+1">Send</button></form>''')
            url = 'https://github.com/meteosimaji/anywhere-computer/pull/99'
            decorated = (
                '<p>Review <span data-rich-text-generated-autolink '
                f'text-link-href="{url}"><span><span data-inline-url-icon '
                'aria-hidden="true" contenteditable="false" style="display:block">'
                f'<svg></svg></span>{url}</span></span> now</p>')
            await editor.evaluate('(e, html) => e.innerHTML=html', decorated)
            literal = f'Review {url} now'
            assert await editor.inner_text() != literal

            async def submit(text):
                return await page.evaluate(
                    source + '\ntext => submitSubchatDraft(document,location.href,text,[])', text)

            assert await submit(literal)
            assert await page.evaluate('window.sends') == 1
            for changed in [decorated.replace(' now', ' later'),
                            decorated.replace(' now', '<br> now'),
                            decorated.replace(f'>{url}</span>', '>different</span>'),
                            decorated.replace(' now', '<strong> now</strong>')]:
                await editor.evaluate('(e, html) => e.innerHTML=html', changed)
                assert not await submit(literal)
            assert await page.evaluate('window.sends') == 1
            # Matching displayed text must not bypass destination verification.
            inline = decorated.replace('display:block', 'display:inline')
            for changed in [inline.replace(f'text-link-href="{url}"',
                                           'text-link-href="https://example.com/other"'),
                            inline.replace('data-rich-text-generated-autolink ',
                                           'data-rich-text-generated-autolink '
                                           'data-rich-text-link-href='
                                           '"https://example.com/other" ')]:
                await editor.evaluate('(e, html) => e.innerHTML=html', changed)
                assert await editor.inner_text() == literal
                assert not await submit(literal)
            await editor.evaluate('(e, html) => e.innerHTML=html',
                                  decorated + '<p>Keep  two spaces<br>and a newline</p>')
            assert await submit(literal + '\nKeep  two spaces\nand a newline')
        finally:
            await browser.close()


async def test_unified_composer_uses_confirmed_editor_and_send_button():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] /
              'src/anywhere_computer/subchat_browser/subchat_input.js').read_text(
                  encoding='utf-8')
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''<main></main><form data-type="unified-composer">
              <div id="prompt-textarea" role="textbox" contenteditable="true"><p><br></p></div>
              <button type="button" data-testid="send-button"
                aria-label="プロンプトを送信する" onclick="window.sends=(window.sends||0)+1">
                Send</button></form>''')
            editor = page.locator('#prompt-textarea')
            await editor.click()
            result = await page.evaluate(
                source + '\ntext => insertSubchatDraft(document, text)', '日本語 🚀')
            assert result == {'state': 'draft_observed', 'input_dispatched': True,
                              'submitted': False}
            assert await page.evaluate(
                source + '\ntext => submitSubchatDraft(document,location.href,text,[])',
                '日本語 🚀') is True
            assert await page.evaluate('window.sends') == 1
            await editor.fill('changed')
            assert await page.evaluate(
                source + '\ntext => submitSubchatDraft(document,location.href,text,[])',
                '日本語 🚀') is False
            assert await page.evaluate('window.sends') == 1
        finally:
            await browser.close()


async def test_current_chat_message_ids_guard_followup_dispatch():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] /
              'src/anywhere_computer/subchat_browser/subchat_input.js').read_text(
                  encoding='utf-8')
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''<main>
              <div data-message-author-role="user" data-message-id="user-a"></div>
              <div data-message-author-role="assistant" data-message-id="assistant-a"></div>
              </main><form data-type="unified-composer">
              <div id="prompt-textarea" role="textbox" contenteditable="true"><p><br></p></div>
              <button type="button" data-testid="send-button"
                onclick="window.sends=(window.sends||0)+1">Send</button></form>''')
            editor = page.locator('#prompt-textarea')
            await editor.click()
            assert (await page.evaluate(
                source + '\ntext => insertSubchatDraft(document,text)', 'fixture'))[
                    'state'] == 'draft_observed'
            async def submit(ids):
                return await page.evaluate(
                    source + '\nids => submitSubchatDraft(document,location.href,"fixture",ids)',
                    ids)

            assert await submit(['user-a', 'assistant-a']) is True
            assert await page.evaluate('window.sends') == 1
            await page.locator('main').evaluate('''main => main.insertAdjacentHTML(
                'beforeend', '<div data-message-author-role="user" '
                + 'data-message-id="user-b"></div>')''')
            assert await submit(['user-a', 'assistant-a']) is False
            assert await page.evaluate('window.sends') == 1
        finally:
            await browser.close()


async def test_observed_paragraphs_preserve_exact_newlines_and_only_known_fillers():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] /
              'src/anywhere_computer/subchat_browser/subchat_input.js').read_text(
                  encoding='utf-8')
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''<main></main><form data-chatgpt-composer>
              <div data-composer-markdown role="textbox" contenteditable="true"></div>
              <button type="button" aria-label="Send"
                onclick="window.sends=(window.sends||0)+1">Send</button></form>''')
            editor = page.get_by_role('textbox')
            empty = '<p data-empty-paragraph="true"><br class="ProseMirror-trailingBreak"></p>'
            # These are observed editor shapes, independent of native browser
            # innerText's extra paragraph spacing. No provider model turn runs.
            cases = [
                ('<p>alpha</p>', 'alpha', True),
                ('<p>alpha</p>' + empty, 'alpha\n', True),
                ('<p>alpha</p>' + empty * 2, 'alpha\n\n', True),
                (empty + '<p>first  line</p>' + empty + '<p>last\tline</p>' + empty * 2,
                 '\nfirst  line\n\nlast\tline\n\n', True),
                ('<p>alpha</p><p>beta</p>', 'alpha\nbeta', True),
                ('<p>alpha</p><p>beta</p>', 'alpha\n\nbeta', False),
                ('<p>alpha<br>beta<br></p>', 'alpha\nbeta\n', True),
                ('<p>alpha</p>' + empty, 'alpha', False),
                ('<p>alpha</p>' + empty, 'alpha\n\n', False),
                ('<p>alpha</p>' + empty.replace('true', 'false'), 'alpha\n', False),
                ('<p>alpha</p>' + empty.replace('class=', 'aria-hidden="true" class='),
                 'alpha\n', False),
                ('<p>alpha</p>' + empty.replace('</p>', 'changed</p>'), 'alpha\n', False),
                ('<p>alpha<br class="ProseMirror-trailingBreak"></p>', 'alpha', False),
                ('<p>alpha<br><br class="ProseMirror-trailingBreak"></p>',
                 'alpha\n\n', False),
                ('<p aria-hidden="true">alpha</p>', 'alpha', False),
                ('<p>alpha</p><div>beta</div>', 'alpha\nbeta', False),
            ]
            accepted = 0
            for html, text, expected in cases:
                await editor.evaluate('(element, html) => element.innerHTML=html', html)
                assert await page.evaluate(
                    source + '\ntext => submitSubchatDraft(document,location.href,text,[])',
                    text) is expected, (html, text)
                accepted += expected
                assert await page.evaluate('window.sends || 0') == accepted
        finally:
            await browser.close()


@pytest.mark.parametrize('interference', [
    'edit', 'replace', 'remove', 'manual', 'throw', 'reject', 'no_mutation',
])
async def test_pending_literal_input_preserves_intervention_and_cleanup(interference):
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] /
              'src/anywhere_computer/subchat_browser/subchat_input.js').read_text(
                  encoding='utf-8')
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''<main></main><form data-chatgpt-composer>
              <div data-composer-markdown role="textbox" contenteditable="plaintext-only"></div>
              <button type="button" aria-label="Send"
                onclick="window.sends=(window.sends||0)+1">Send</button></form>''')
            await page.get_by_role('textbox').click()
            result = await page.evaluate(source + '''
                async interference => {
                  const editor = document.querySelector(subchatEditorSelector);
                  const original = document.execCommand;
                  let inputs = 0, activeListeners = 0;
                  const add = window.addEventListener.bind(window);
                  window.addEventListener = (name, listener, options) => {
                    if (options?.signal) {
                      activeListeners++;
                      options.signal.addEventListener('abort', () => activeListeners--,
                                                      {once:true});
                    }
                    return add(name, listener, options);
                  };
                  editor.addEventListener('input', () => {
                    inputs++;
                    queueMicrotask(() => {
                      if (interference === 'edit') editor.textContent = 'human edit';
                      if (interference === 'replace') editor.replaceWith(editor.cloneNode(true));
                      if (interference === 'remove') editor.remove();
                      if (interference === 'manual') editor.dispatchEvent(
                        new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
                    });
                  }, {once:true});
                  if (interference === 'throw') document.execCommand = () => {
                    throw new Error('native input failure');
                  };
                  if (interference === 'reject') document.execCommand = () => false;
                  if (interference === 'no_mutation') document.execCommand = () => true;
                  try {
                    const guard = await insertObservedSubchatDraft(document, 'send once');
                    try {
                      return {draft: guard.draft, intervened: guard.intervened, inputs,
                        outcome: submitObservedSubchatDraft(
                          document, guard, location.href, 'send once', [])};
                    } finally { guard.stop(); }
                  } catch(error) {
                    return {error:error.message, inputs, activeListeners};
                  } finally {
                    document.execCommand = original;
                    window.addEventListener = add;
                    window.remainingListeners = activeListeners;
                  }
                }''', interference)
            if interference == 'throw':
                assert result == {'error': 'native input failure', 'inputs': 0,
                                  'activeListeners': 0}
            elif interference == 'manual':
                assert result['inputs'] == 1
                assert result['intervened'] is True
                assert result['outcome'] == 'intervened'
            else:
                assert result['inputs'] == (0 if interference in {'reject', 'no_mutation'} else 1)
                assert result['draft'] == {'state': 'draft_unconfirmed', 'input_dispatched': True}
                assert result['outcome'] == (
                    'submission_unconfirmed' if interference in {'remove', 'replace'}
                    else 'draft_rejected')
            assert await page.evaluate('window.sends || 0') == 0
            assert await page.evaluate('window.remainingListeners') == 0
        finally:
            await browser.close()
