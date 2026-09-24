"""Experimental ordinary-Chat menu probe; never submits a message.

Requires the optional browser extra and an already authorized dedicated profile.
Do not point this at the user's normal browser profile.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..subchat import SubchatAccessError
from ..subchat_state import SubchatHTTPSelection, SubchatSelectionError
from .efforts import collect_efforts

if TYPE_CHECKING:
    from playwright.async_api import CDPSession, Locator, Page, Response

SOURCE = Path(__file__).with_name("subchat_model_menu.js").read_text(encoding="utf-8")
COMPOSER = 'form[data-type="unified-composer"], form[data-chatgpt-composer]'
EDITOR = ('form[data-type="unified-composer"] #prompt-textarea[role="textbox"], '
          '[data-composer-markdown][role="textbox"]')
TRIGGER = ('form[data-type="unified-composer"] button.__composer-pill[aria-haspopup="menu"], '
           '[data-composer-navigation-target="reasoning"]')
TOGGLE = ('[role="menu"] [role="menuitem"][aria-label="モデルを選択"]:visible, '
          '[data-model-picker-view-toggle="true"]:visible')
CONTROL = ('[role="menu"] [role="menuitem"][aria-label="パワー"]:visible, '
           '[data-reasoning-slider="true"]:visible')


class HTTPModel(BaseModel):
    model_config = ConfigDict(strict=True)
    slug: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    is_work_mode_model: bool


class HTTPPreset(BaseModel):
    model_config = ConfigDict(strict=True)
    id: int
    title: str = Field(min_length=1, max_length=256)
    model_slug: str = Field(min_length=1, max_length=256)
    preset_type: str = Field(min_length=1, max_length=64)
    selected_display_version: str = Field(min_length=1, max_length=256)
    thinking_effort: str | None = Field(default=None, min_length=1, max_length=256)


class HTTPVersion(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1, max_length=256)
    display_text: str = Field(min_length=1, max_length=256)
    enabled: bool
    intelligence_presets: list[HTTPPreset] = Field(max_length=128)


class HTTPCatalog(BaseModel):
    model_config = ConfigDict(strict=True)
    models: list[HTTPModel] = Field(min_length=1, max_length=512)
    versions: list[HTTPVersion] = Field(min_length=1, max_length=32)


def project_http_catalog(payload: bytes) -> dict[str, object]:
    """Project the observed schema; never invent IDs, aliases or availability."""
    if len(payload) > 1_048_576:
        raise ValueError('Model catalog is too large')
    catalog = HTTPCatalog.model_validate_json(payload)
    models = {model.slug: model for model in catalog.models}
    if (len(models) != len(catalog.models)
            or len({version.id for version in catalog.versions}) != len(catalog.versions)):
        raise ValueError('Ambiguous model catalog identity')
    versions: list[dict[str, object]] = []
    for version in catalog.versions:
        if len({preset.id for preset in version.intelligence_presets}) != len(
                version.intelligence_presets):
            raise ValueError('Ambiguous model preset identity')
        choices: list[dict[str, object]] = []
        for preset in version.intelligence_presets:
            model = models.get(preset.model_slug)
            if model is None:
                raise ValueError('Preset references an unknown model')
            if model.is_work_mode_model:
                continue
            choices.append({**preset.model_dump(), 'model_title': model.title,
                            'available': version.enabled and preset.preset_type == 'available',
                            'http_selection': SubchatHTTPSelection(
                                version_id=version.id, preset_id=preset.id,
                                model_slug=preset.model_slug,
                                thinking_effort=preset.thinking_effort).model_dump()})
        versions.append({'id': version.id, 'label': version.display_text,
                         'enabled': version.enabled, 'choices': choices})
    return {'state': 'http_catalog_observed', 'versions': versions, 'submitted': False,
            'source': 'browser_observed_http', 'generation_http_verified': False,
            'send_requires_ui_labels': True}


def require_http_selection(catalog: dict[str, object], selected: SubchatHTTPSelection) -> None:
    """Require the exact caller-observed choice to still be available; never resolve aliases."""
    versions = catalog.get('versions')
    if catalog.get('state') != 'http_catalog_observed' or not isinstance(versions, list):
        raise ValueError('HTTP model catalog is unavailable')
    version_matches: list[dict[str, object]] = []
    for version in versions:
        if not isinstance(version, dict) or not isinstance(version.get('choices'), list):
            raise ValueError('HTTP model catalog shape changed')
        if version.get('id') == selected.version_id:
            version_matches.append(version)
    if not version_matches:
        raise SubchatSelectionError('version_id', 'not_found')
    if len(version_matches) != 1:
        raise SubchatSelectionError('version_id', 'ambiguous')
    choices = version_matches[0]['choices']
    assert isinstance(choices, list)
    matches = [choice for choice in choices
               if isinstance(choice, dict) and isinstance(choice.get('http_selection'), dict)
               and choice['http_selection'].get('preset_id') == selected.preset_id]
    if not matches:
        raise SubchatSelectionError('preset_id', 'not_found')
    if len(matches) != 1:
        raise SubchatSelectionError('preset_id', 'ambiguous')
    choice = matches[0]
    if choice.get('available') is not True:
        raise SubchatSelectionError('preset_id', 'unavailable')
    identity = choice['http_selection']
    assert isinstance(identity, dict)
    if identity.get('model_slug') != selected.model_slug:
        raise SubchatSelectionError('model_slug', 'mismatch')
    if identity.get('thinking_effort') != selected.thinking_effort:
        raise SubchatSelectionError('thinking_effort', 'mismatch')


async def observe_http_catalog(page: Page) -> Response:
    """Observe the app's own catalog GET on a dedicated page; no token copying."""
    def catalog_response(response: Response) -> bool:
        url = urlsplit(response.url)
        return (url.scheme == 'https' and url.netloc == 'chatgpt.com'
                and url.path == '/backend-api/models' and response.request.method == 'GET')

    async with page.expect_response(catalog_response, timeout=15_000) as pending:
        await page.goto('https://chatgpt.com/', wait_until='domcontentloaded')
    response = await pending.value
    if response.status in (401, 403):
        raise SubchatAccessError(response.status)
    if response.status != 200:
        raise ConnectionError('Model catalog request did not succeed')
    # Cookie-only GET can return a reduced catalog with status 200. Require
    # evidence of the app's authenticated request, without retaining its value.
    if not await response.request.header_value('authorization'):
        raise ConnectionError('Authenticated model catalog request was not observed')
    if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
        raise ValueError('Unexpected model catalog response format')
    return response


async def empty_chat(page: Page) -> bool:
    if page.url.rstrip("/") != "https://chatgpt.com":
        return False
    chat = page.get_by_role("button", name="Chat", exact=True)
    editors = page.locator(EDITOR)
    radio = page.locator('[role="radio"][data-tpp-toggle-value="chatgpt"][data-state="on"]'
                         '[aria-checked="true"]')
    radios = page.locator('[role="radio"][data-tpp-toggle-value="chatgpt"]')
    ordinary = (await radio.count() == 1 if await radios.count() else
                await chat.count() == 1 and await chat.get_attribute("aria-pressed") == "true")
    return (ordinary
            and await editors.count() == 1 and not (await editors.inner_text()).strip())


async def collect_page(page: Page, model: str | None = None, *,
                       background_input: bool = False) -> dict[str, object]:
    async def click(locator: Locator) -> None:
        if background_input:
            from .background import background_pointer_click

            await background_pointer_click(locator)
        else:
            await locator.click()

    async def press(locator: Locator, key: str) -> None:
        if background_input:
            from .background import background_key_press

            await background_key_press(locator, key)
        else:
            await locator.press(key)

    if not await empty_chat(page):
        return {"state": "empty_chat_unconfirmed"}
    trigger = page.locator(TRIGGER)
    if await trigger.count() != 1 or await trigger.get_attribute("aria-expanded") != "false":
        return {"state": "closed_picker_unconfirmed"}
    try:
        await click(trigger)
        models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if models.get("state") == "model_list_not_visible":
            await click(page.locator(TOGGLE))
            models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if models.get("state") != "models_observed":
            return {"state": "models_unconfirmed"}
        if model is not None:
            choices = [item for item in models['models']
                       if item['label'] == model and not item['disabled']]
            if len(choices) != 1:
                return {'state': 'requested_model_unavailable', 'models': models['models'],
                        'submitted': False}
            await click(page.get_by_role('menuitemradio', name=model, exact=True))
            await click(page.locator(TOGGLE))
            models = await page.evaluate(SOURCE + '\nobserveSubchatModelMenu(document)')
            if [item['label'] for item in models.get('models', [])
                    if item['selected']] != [model]:
                return {'state': 'model_selection_unconfirmed', 'submitted': False}
            # Selecting the verified row opens its effort view. No label table
            # or assumed number of effort choices is involved.
            await click(page.get_by_role('menuitemradio', name=model, exact=True))
        else:
            # Some UI versions remember the model-list view on reopening.
            # In that case return the verified models as a partial catalog.
            await press(page.get_by_role("menu"), "Escape")
            await click(trigger)

        async def read() -> dict[str, object]:
            if not await empty_chat(page):
                raise ConnectionError("empty Chat changed during observation")
            value: dict[str, object] = await page.evaluate(
                SOURCE + "\nobserveSubchatEffort(document)")
            return value

        async def step(key: str) -> None:
            if not await empty_chat(page):
                raise ConnectionError("empty Chat changed before keyboard input")
            await press(page.locator(CONTROL), key)

        efforts = await collect_efforts(read, step)
        final_models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if final_models.get("state") == "model_list_not_visible":
            await click(page.locator(TOGGLE))
            final_models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if final_models != models:
            return {"state": "model_selection_changed", "submitted": False}
        state = "catalog_observed" if efforts["state"] == "efforts_observed" else "catalog_partial"
        return {"state": state, "models": models["models"],
                "efforts_for_selected_model": efforts, "submitted": False}
    finally:
        if await trigger.get_attribute("aria-expanded") == "true":
            await press(page.get_by_role("menu"), "Escape")
        if await trigger.get_attribute("aria-expanded") != "false":
            raise ConnectionError("picker closure was not confirmed")


async def picker_ready(page: Page) -> bool:
    trigger = page.locator(TRIGGER)
    login = page.get_by_role("button", name=re.compile(r"^(ログイン|Log in)$"))
    await trigger.or_(login).filter(visible=True).first.wait_for(state="visible")
    if await login.filter(visible=True).count():
        return False
    await trigger.wait_for(state="visible")
    return True


async def minimize_window(
    session: CDPSession, window_id: int, *, timeout: float = 2, interval: float = .1,
) -> bool:
    """Request once; wait only for OS confirmation before any page action."""
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout and interval must be positive")
    try:
        async with asyncio.timeout(timeout):
            await session.send("Browser.setWindowBounds", {
                "windowId": window_id, "bounds": {"windowState": "minimized"},
            })
            while True:
                bounds = await session.send("Browser.getWindowBounds", {"windowId": window_id})
                if bounds["bounds"].get("windowState") == "minimized":
                    return True
                await asyncio.sleep(interval)
    except TimeoutError:
        return False


async def probe(profile: Path, headed: bool, minimized: bool = False) -> dict[str, object]:
    from playwright.async_api import async_playwright

    from . import CHROME_PROFILE_IGNORED_DEFAULT_ARGS

    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=not (headed or minimized),
            ignore_default_args=list(CHROME_PROFILE_IGNORED_DEFAULT_ARGS),
            args=["--start-minimized"] if minimized else [])
        try:
            page = context.pages[0] if minimized and context.pages else await context.new_page()
            if minimized:
                session = await context.new_cdp_session(page)
                window = await session.send("Browser.getWindowForTarget")
                if not await minimize_window(session, window["windowId"]):
                    return {"state": "minimization_unconfirmed", "submitted": False}
            page.set_default_timeout(10_000)
            response = await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            if response is None or not response.ok:
                return {"state": "page_unavailable",
                        "http_status": response.status if response else None, "submitted": False}
            if not await picker_ready(page):
                return {"state": "login_required", "submitted": False}
            return await collect_page(page)
        finally:
            await context.close()


def main() -> None:
    from playwright.async_api import Error

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headed", action="store_true")
    mode.add_argument("--minimized", action="store_true",
                      help="Use a dedicated visible-browser process with its window minimized")
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(args.profile.resolve(), args.headed, args.minimized))
    except (Error, ConnectionError, ValueError) as error:
        result = {"state": "probe_unconfirmed", "error_type": type(error).__name__,
                  "submitted": False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
