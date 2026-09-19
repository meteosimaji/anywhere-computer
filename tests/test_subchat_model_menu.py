"""Real DOM checks; optional browser dependency, no account or network needed."""
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_model_menu_visibility_and_identity() -> None:
    playwright = pytest.importorskip("playwright.async_api")
    source = (Path(__file__).parents[1] / "scripts/subchat_model_menu.js").read_text()
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel="chrome", headless=True)
        except playwright.Error as error:
            if "not found" in str(error) or "doesn't exist" in str(error):
                pytest.skip("Chrome is required for the optional real DOM check")
            raise
        try:
            page = await browser.new_page()
            async def observe(html: str) -> object:
                await page.set_content(html)
                return await page.evaluate(source + "\nobserveSubchatModelMenu(document)")

            rows = '''<div role="menuitemradio" aria-checked="true"><span>Future model</span></div>
              <div role="menuitemradio" aria-checked="false" aria-disabled="true">
                <span>旧モデル</span><span>Retiring soon</span></div>'''
            assert await observe('<div role="menu"><div inert>' + rows + '</div></div>') == {
                "state": "model_list_not_visible"}
            assert await observe('<div role="menu" aria-hidden="true">' + rows + '</div>') == {
                "state": "menu_unconfirmed"}
            hidden_menu = '<div role="menu" style="visibility:hidden">' + rows + '</div>'
            assert await observe(hidden_menu) == {
                "state": "menu_unconfirmed"}
            result = await observe('<div role="menu">' + rows + '</div>')
            assert result == {"state": "models_observed", "models": [
                {"label": "Future model", "notices": [], "selected": True, "disabled": False},
                {"label": "旧モデル", "notices": ["Retiring soon"],
                 "selected": False, "disabled": True},
            ]}
            assert await observe('<div role="menu">' + rows.replace(
                'aria-checked="false"', 'aria-checked="true"') + '</div>') == {
                    "state": "ambiguous_menu"}
            assert await observe('<div role="menu">' + rows.replace(
                '旧モデル', 'Future model') + '</div>') == {"state": "ambiguous_menu"}
            assert await observe('<div role="menu">' + rows.replace(
                '<span>', '<b>').replace('</span>', '</b>') + '</div>') == {
                    "state": "unsupported_menu"}
        finally:
            await browser.close()
