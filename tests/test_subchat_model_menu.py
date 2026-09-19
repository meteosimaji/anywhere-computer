"""Real DOM checks; optional browser dependency, no account or network needed."""
import importlib.util
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
            async def observe(html: str, function: str = "observeSubchatModelMenu") -> object:
                await page.set_content(html)
                return await page.evaluate(source + f"\n{function}(document)")

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
            effort = '''<div role="menu">
              <span role="status" id="level">新しい強度、7件中4番目</span>
              <div data-reasoning-slider="true" aria-describedby="level">
                <span role="slider" aria-hidden="true" aria-valuemin="0"
                  aria-valuemax="6" aria-valuenow="3"></span></div></div>'''
            assert await observe(effort, "observeSubchatEffort") == {
                "state": "effort_observed", "minimum": 0, "maximum": 6, "index": 3,
                "description": "新しい強度、7件中4番目", "disabled": False}
            for wrong in ["", "NaN", "3.5", "7", "9007199254740992"]:
                changed = effort.replace('aria-valuenow="3"', f'aria-valuenow="{wrong}"')
                assert await observe(changed, "observeSubchatEffort") == {
                    "state": "unsupported_effort_control"}
            assert await observe(effort.replace('aria-describedby="level"', ''),
                                 "observeSubchatEffort") == {
                "state": "effort_description_unconfirmed"}
            assert await observe(effort.replace('data-reasoning-slider="true"',
                                                'data-reasoning-slider="true" inert'),
                                 "observeSubchatEffort") == {
                "state": "effort_control_unconfirmed"}
            spec = importlib.util.spec_from_file_location(
                "effort_collector", Path(__file__).parents[1] / "scripts/subchat_efforts.py")
            assert spec and spec.loader
            collector = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(collector)
            await page.set_content(effort.replace('data-reasoning-slider="true"',
                                                  'data-reasoning-slider="true" tabindex="0"'))
            await page.evaluate('''() => {
              const control = document.querySelector('[data-reasoning-slider]');
              control.addEventListener('keydown', event => {
                const thumb = control.querySelector('[role="slider"]');
                const index = Number(thumb.getAttribute('aria-valuenow'));
                const delta = event.key === 'ArrowRight' ? 1 : -1;
                const next = Math.max(0, Math.min(6, index + delta));
                thumb.setAttribute('aria-valuenow', String(next));
                document.getElementById('level').textContent =
                  next === 3 ? '新しい強度、7件中4番目' : 'Choice ' + next;
              });
            }''')

            async def read_effort():
                return await page.evaluate(source + "\nobserveSubchatEffort(document)")

            async def step_effort(key):
                await page.locator('[data-reasoning-slider="true"]').press(key)

            collected = await collector.collect_efforts(read_effort, step_effort)
            assert collected["state"] == "efforts_observed"
            assert len(collected["positions"]) == 7
            assert collected["restored"] is True
            assert (await read_effort())["index"] == 3
        finally:
            await browser.close()
