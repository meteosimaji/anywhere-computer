"""Launch an owned macOS Chrome profile without activating a window."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Locator, Page, Playwright


def _profile_processes(profile: Path) -> list[psutil.Process]:
    argument = f'--user-data-dir={profile}'
    processes = []
    for process in psutil.process_iter(['cmdline']):
        try:
            command = process.info['cmdline'] or []
            if argument in command and any('Google Chrome.app/Contents/MacOS/Google Chrome'
                                           in part for part in command[:1]):
                processes.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


def _stop_profile_processes(profile: Path, *, startup_grace: bool = False) -> None:
    processes = _profile_processes(profile)
    if startup_grace:
        # `open` can return before Launch Services has started Chrome. A
        # cancelled launch must still catch that late process.
        deadline = time.monotonic() + 10
        while not processes and time.monotonic() < deadline:
            time.sleep(.05)
            processes = _profile_processes(profile)
    for process in processes:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive or _profile_processes(profile):
        raise RuntimeError('Background Chrome profile process did not stop')


@asynccontextmanager
async def background_chrome_context(
    driver: Playwright, profile: Path, launch_args: list[str],
) -> AsyncIterator[BrowserContext]:
    """Attach only to a fresh private profile's ephemeral loopback CDP port."""
    if sys.platform != 'darwin':
        raise ValueError('Background Chrome launch is supported on macOS only')
    profile = profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    port_file = profile / 'DevToolsActivePort'
    if port_file.exists() or _profile_processes(profile):
        # Do not trust a pre-existing endpoint even if its Chrome process is
        # gone; a stale file could point at a different loopback listener.
        raise ValueError('Dedicated Chrome profile is already in use')
    command = ['/usr/bin/open', '-g', '-j', '-n', '-b', 'com.google.Chrome', '--args',
               f'--user-data-dir={profile}', '--remote-debugging-port=0',
               '--remote-debugging-address=127.0.0.1',
               '--no-first-run', '--no-default-browser-check', '--no-startup-window',
               *launch_args]
    browser = None
    launched = False
    ownership_confirmed = False
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        launched = True
        if await process.wait() != 0:
            raise ConnectionError('Background Chrome launch failed')
        try:
            async with asyncio.timeout(10):
                while True:
                    if port_file.is_file():
                        lines = port_file.read_text(encoding='ascii').splitlines()
                        if len(lines) == 2 and lines[0].isdigit():
                            break
                    await asyncio.sleep(.05)
        except TimeoutError as error:
            raise ConnectionError('Background Chrome CDP endpoint did not appear') from error
        port = int(lines[0])
        if not 1 <= port <= 65535:
            raise ConnectionError('Background Chrome CDP endpoint is invalid')
        if not _profile_processes(profile):
            raise ConnectionError('Background Chrome profile ownership is unconfirmed')
        ownership_confirmed = True
        browser = await driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
        if len(browser.contexts) != 1:
            raise ConnectionError('Background Chrome context is ambiguous')
        yield browser.contexts[0]
    finally:
        try:
            if browser is not None:
                await browser.close()
        finally:
            if launched:
                await asyncio.to_thread(
                    _stop_profile_processes, profile,
                    startup_grace=not ownership_confirmed)


async def new_background_page(context: BrowserContext) -> Page:
    """Create an owned tab without activating a hidden Chrome application."""
    browser = context.browser
    if browser is None or not browser.is_connected():
        raise ConnectionError('Background Chrome disconnected')
    previous = set(context.pages)
    session = await browser.new_browser_cdp_session()
    target_id = None
    try:
        target = await session.send('Target.createTarget', {
            'url': 'about:blank', 'background': True,
        })
        target_id = target['targetId']
        async with asyncio.timeout(10):
            while True:
                added = [page for page in context.pages if page not in previous]
                if len(added) == 1:
                    return added[0]
                if len(added) > 1:
                    raise ConnectionError('Background Chrome tab is ambiguous')
                await asyncio.sleep(.05)
    except BaseException:
        if target_id is not None:
            await session.send('Target.closeTarget', {'targetId': target_id})
        raise
    finally:
        await session.detach()


async def background_pointer_click(locator: Locator) -> None:
    """Dispatch the app's pointer gesture inside its owned background page."""
    await locator.evaluate('''element => {
        if (!element.isConnected || !element.getClientRects().length ||
            element.closest('[inert], [aria-hidden="true"]') ||
            element.disabled || element.getAttribute('aria-disabled') === 'true')
            throw new Error('Background click target is unavailable');
        const bounds = element.getBoundingClientRect();
        const x = bounds.left + bounds.width / 2;
        const y = bounds.top + bounds.height / 2;
        for (const [type, buttons] of [
            ['pointerdown', 1], ['mousedown', 1], ['pointerup', 0],
            ['mouseup', 0], ['click', 0]]) {
            element.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerType: 'mouse',
                isPrimary: true, button: 0, buttons, clientX: x, clientY: y,
            }));
        }
    }''')


async def background_key_press(locator: Locator, key: str) -> None:
    """Dispatch one observed slider/menu key without requesting OS focus."""
    if key not in {'ArrowLeft', 'ArrowRight', 'Escape'}:
        raise ValueError('Unsupported background key')
    await locator.evaluate('''(element, key) => {
        if (!element.isConnected || !element.getClientRects().length)
            throw new Error('Background key target is unavailable');
        element.focus();
        element.dispatchEvent(new KeyboardEvent('keydown', {
            key, bubbles: true, cancelable: true,
        }));
        element.dispatchEvent(new KeyboardEvent('keyup', {
            key, bubbles: true, cancelable: true,
        }));
    }''', key)


async def background_focus_editor(locator: Locator) -> None:
    """Place a collapsed selection in an empty editor without a desktop click."""
    await locator.evaluate('''element => {
        if (!element.isConnected || !element.isContentEditable ||
            !element.getClientRects().length || element.innerText.trim())
            throw new Error('Background editor is unavailable');
        element.focus();
        const selection = element.ownerDocument.getSelection();
        if (!selection) throw new Error('Background editor selection is unavailable');
        const range = element.ownerDocument.createRange();
        range.selectNodeContents(element);
        range.collapse(false);
        selection.removeAllRanges();
        selection.addRange(range);
    }''')
