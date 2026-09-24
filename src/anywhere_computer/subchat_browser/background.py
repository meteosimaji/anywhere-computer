"""Launch an owned macOS Chrome profile without activating a window."""

from __future__ import annotations

import asyncio
import importlib
import os
import secrets
import stat
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Locator, Page, Playwright

if sys.platform == 'win32':
    _NOFOLLOW = 0  # The protected profile path is only used on macOS.
else:
    _NOFOLLOW = os.O_NOFOLLOW


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


def _owned_processes(profile: Path, token: str) -> list[psutil.Process]:
    marker = f'--anywhere-background-owner={token}'
    return [process for process in _profile_processes(profile)
            if marker in (process.info['cmdline'] or [])]


@contextmanager
def _profile_lock(profile: Path) -> Iterator[None]:
    fcntl = importlib.import_module('fcntl')

    descriptor = os.open(profile / '.anywhere-background.lock',
                         os.O_CREAT | os.O_RDWR | _NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Dedicated Chrome profile is already in use') from error
        yield
    finally:
        os.close(descriptor)


def _read_owner_token(owner_file: Path) -> str:
    try:
        descriptor = os.open(owner_file, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return ''
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ''
        token = os.read(descriptor, 33).decode('ascii')
        if len(token) != 32 or any(char not in '0123456789abcdef' for char in token):
            return ''
        return token
    finally:
        os.close(descriptor)


def _stop_profile_processes(profile: Path, token: str, *, startup_grace: bool = False) -> None:
    processes = _owned_processes(profile, token)
    if startup_grace:
        # `open` can return before Launch Services has started Chrome. A
        # cancelled launch must still catch that late process.
        deadline = time.monotonic() + 10
        while not processes and time.monotonic() < deadline:
            time.sleep(.05)
            processes = _owned_processes(profile, token)
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
    if alive or _owned_processes(profile, token):
        raise RuntimeError('Background Chrome profile process did not stop')


@asynccontextmanager
async def background_chrome_context(
    driver: Playwright, profile: Path, launch_args: list[str],
) -> AsyncIterator[BrowserContext]:
    """Attach only to an owned private profile's ephemeral loopback CDP port."""
    if sys.platform != 'darwin':
        raise ValueError('Background Chrome launch is supported on macOS only')
    profile = profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    with _profile_lock(profile):
        port_file = profile / 'DevToolsActivePort'
        owner_file = profile / '.anywhere-background.owner'
        previous = _profile_processes(profile)
        if previous:
            token = _read_owner_token(owner_file)
            if not token or len(_owned_processes(profile, token)) != len(previous):
                raise ValueError('Dedicated Chrome profile is already in use')
            await asyncio.to_thread(_stop_profile_processes, profile, token)
        # This file is only a Chrome discovery hint. Once the exact-profile
        # process is gone, it must not direct us to an unrelated listener.
        port_file.unlink(missing_ok=True)
        token = secrets.token_hex(16)
        temporary_owner = profile / f'.anywhere-background.owner.{token}'
        descriptor = os.open(temporary_owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             _NOFOLLOW,
                             0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as owner_stream:
            owner_stream.write(token)
        temporary_owner.replace(owner_file)
        command = ['/usr/bin/open', '-g', '-j', '-n', '-b', 'com.google.Chrome', '--args',
                   f'--user-data-dir={profile}', '--remote-debugging-port=0',
                   '--remote-debugging-address=127.0.0.1',
                   f'--anywhere-background-owner={token}',
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
            if not _owned_processes(profile, token):
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
                        _stop_profile_processes, profile, token,
                        startup_grace=not ownership_confirmed)
                    port_file.unlink(missing_ok=True)


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
