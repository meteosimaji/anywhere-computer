"""Experimental ordinary-Chat menu probe; never submits a message.

Requires the optional browser extra and an already authorized dedicated profile.
Do not point this at the user's normal browser profile.
"""
import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import CDPSession, Error, Page, async_playwright

from .efforts import collect_efforts

SOURCE = Path(__file__).with_name("subchat_model_menu.js").read_text(encoding="utf-8")
TRIGGER = '[data-composer-navigation-target="reasoning"]'
TOGGLE = '[data-model-picker-view-toggle="true"]:visible'
CONTROL = '[data-reasoning-slider="true"]:visible'


async def empty_chat(page: Page) -> bool:
    if page.url.rstrip("/") != "https://chatgpt.com":
        return False
    chat = page.get_by_role("button", name="Chat", exact=True)
    editors = page.locator('[data-composer-markdown][role="textbox"]')
    return (await chat.count() == 1 and await chat.get_attribute("aria-pressed") == "true"
            and await editors.count() == 1 and not (await editors.inner_text()).strip())


async def collect_page(page: Page) -> dict[str, object]:
    if not await empty_chat(page):
        return {"state": "empty_chat_unconfirmed"}
    trigger = page.locator(TRIGGER)
    if await trigger.count() != 1 or await trigger.get_attribute("aria-expanded") != "false":
        return {"state": "closed_picker_unconfirmed"}
    try:
        await trigger.click()
        models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if models.get("state") == "model_list_not_visible":
            await page.locator(TOGGLE).click()
            models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if models.get("state") != "models_observed":
            return {"state": "models_unconfirmed"}
        # Return to the simple view by closing and reopening the picker. The
        # observed implementation opens that view without selecting another model.
        await page.get_by_role("menu").press("Escape")
        await trigger.click()

        async def read() -> dict[str, object]:
            if not await empty_chat(page):
                raise ConnectionError("empty Chat changed during observation")
            value: dict[str, object] = await page.evaluate(
                SOURCE + "\nobserveSubchatEffort(document)")
            return value

        async def step(key: str) -> None:
            if not await empty_chat(page):
                raise ConnectionError("empty Chat changed before keyboard input")
            await page.locator(CONTROL).press(key)

        efforts = await collect_efforts(read, step)
        final_models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if final_models.get("state") == "model_list_not_visible":
            await page.locator(TOGGLE).click()
            final_models = await page.evaluate(SOURCE + "\nobserveSubchatModelMenu(document)")
        if final_models != models:
            return {"state": "model_selection_changed", "submitted": False}
        state = "catalog_observed" if efforts["state"] == "efforts_observed" else "catalog_partial"
        return {"state": state, "models": models["models"],
                "efforts_for_selected_model": efforts, "submitted": False}
    finally:
        if await trigger.get_attribute("aria-expanded") == "true":
            await page.get_by_role("menu").press("Escape")
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
    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=not (headed or minimized),
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
