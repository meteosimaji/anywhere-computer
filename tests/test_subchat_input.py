"""Native browser editing contract; authenticated serialization is a separate gate."""
from pathlib import Path

import pytest


async def test_literal_draft_preserves_text_and_rejects_existing_content():
    playwright = pytest.importorskip('playwright.async_api')
    source = (Path(__file__).parents[1] / 'scripts/subchat_input.js').read_text()
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
                                   'contenteditable="true"><p><br></p></div></form>')
            editor = page.get_by_role('textbox')
            prompt = '日本語 🚀\n```python\nprint("<tag>", "*x*", "a_b")\n```\n\n- item'

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
        finally:
            await browser.close()
