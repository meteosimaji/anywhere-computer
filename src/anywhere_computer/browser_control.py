"""Owner-scoped, isolated browser tabs for explicit web navigation and observation."""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import JsonValue

from .models import BrowserClick, BrowserFill, BrowserNavigate, BrowserSession

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright


class BrowserNavigationUnknown(Exception):
    """Navigation was attempted but its resulting page could not be confirmed."""


class BrowserActionUnknown(Exception):
    """A page action may have run, but its outcome could not be confirmed."""


class BrowserStartupUnavailable(Exception):
    """No isolated tab was registered after local browser startup failed."""


_CLEANUP_WAIT_SECONDS = 5.0
_LOG = logging.getLogger(__name__)


def _cleanup_done(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except BaseException:
        _LOG.warning("Browser startup cleanup did not complete successfully")


async def _finish_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    """Await cleanup through repeated cancellation, but do not hold the lock forever."""
    task = asyncio.create_task(cleanup)
    deadline = asyncio.get_running_loop().time() + _CLEANUP_WAIT_SECONDS
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            task.add_done_callback(_cleanup_done)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), remaining)
            return
        except asyncio.CancelledError:
            if task.done():
                await task
                return
        except TimeoutError:
            task.add_done_callback(_cleanup_done)
            return


@dataclass
class _Navigation:
    requested_url: str
    outcome: str = "unconfirmed"
    observed_url: str | None = None


@dataclass
class _Entry:
    owner: str | None
    session_id: str
    tab_id: str
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_navigation: _Navigation | None = None


class BrowserControl:
    def __init__(self, *, channel: str | None = "auto") -> None:
        # The isolated tab has no account profile. Use the browser shipped with
        # Windows; callers can still request an explicit Playwright channel.
        self.channel = (
            "msedge" if sys.platform == "win32" else "chrome"
        ) if channel == "auto" else channel
        self.entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _live(entry: _Entry) -> bool:
        return not entry.page.is_closed() and entry.browser.is_connected()

    async def _reap_dead(self) -> None:
        """Release ended sessions before enforcing the isolated-browser limit."""
        for session_id, entry in list(self.entries.items()):
            if self._live(entry) or entry.lock.locked():
                continue
            async with entry.lock:
                if self._live(entry) or self.entries.get(session_id) is not entry:
                    continue
                del self.entries[session_id]
                try:
                    await entry.browser.close()
                except Exception:
                    pass  # The browser may already have exited.
                try:
                    await entry.playwright.stop()
                except Exception:
                    pass  # A dead driver must not exhaust session capacity.

    def _entry(
        self, args: BrowserSession, owner: str | None, *, require_live: bool = True,
    ) -> _Entry:
        entry = self.entries.get(args.session_id)
        if entry is None or entry.owner != owner or entry.tab_id != args.tab_id:
            raise ValueError("Browser session or tab unavailable for this connection")
        if require_live and not self._live(entry):
            raise ValueError("Browser session ended; open a new isolated session")
        return entry

    async def open(self, *, owner: str | None) -> dict[str, JsonValue]:
        # Each session gets its own ephemeral browser process and context. No
        # persistent profile or existing user tab is ever attached here.
        async with self._lock:
            await self._reap_dead()
            if len(self.entries) >= 4:
                raise ValueError("Browser session capacity reached")
            try:
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise BrowserStartupUnavailable(
                    "Isolated browser runtime is unavailable"
                ) from error
            manager = async_playwright()
            starting = asyncio.create_task(manager.start())
            try:
                driver = await asyncio.shield(starting)
            except BaseException as error:
                # Playwright 1.58 starts its connection before start() returns.
                # A failed transport may have no output pipe, so only stop a
                # driver that actually returned from start().
                async def stop_starting() -> None:
                    try:
                        started = await starting
                    except BaseException:
                        # In Playwright 1.58, stop_async requires the transport's
                        # output pipe. A failed subprocess spawn has no pipe.
                        connection = getattr(manager, "_connection", None)
                        transport = getattr(connection, "_transport", None)
                        if getattr(transport, "_output", None) is not None:
                            await manager.__aexit__(None, None, None)
                        return
                    await started.stop()

                try:
                    await _finish_cleanup(stop_starting())
                except BaseException:
                    _LOG.warning("Browser startup cleanup did not complete successfully")
                if isinstance(error, Exception):
                    raise BrowserStartupUnavailable(
                        "Isolated browser runtime could not start"
                    ) from error
                raise
            browser = None
            try:
                browser = await driver.chromium.launch(headless=True, channel=self.channel)
                context = await browser.new_context(accept_downloads=False)
                page = await context.new_page()
            except BaseException as error:
                # Cancellation can arrive before the session is registered, so
                # close both resources here; close() cannot discover them later.
                async def close_started() -> None:
                    try:
                        if browser is not None:
                            await browser.close()
                    finally:
                        await driver.stop()

                try:
                    await _finish_cleanup(close_started())
                except BaseException:
                    _LOG.warning("Browser startup cleanup did not complete successfully")
                if isinstance(error, Exception):
                    raise BrowserStartupUnavailable(
                        "Isolated browser could not start with the selected browser"
                    ) from error
                raise
            session_id = uuid.uuid4().hex
            tab_id = uuid.uuid4().hex
            self.entries[session_id] = _Entry(owner, session_id, tab_id, driver, browser,
                                              context, page)
            return {"session_id": session_id, "tab_id": tab_id, "isolation": "ephemeral_context",
                    "url": page.url}

    async def navigate(self, args: BrowserNavigate, *, owner: str | None) -> dict[str, JsonValue]:
        parsed = urlsplit(args.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password):
            raise ValueError("Browser navigation requires an HTTP or HTTPS URL")
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            # Record the attempt before goto: a timeout or lost snapshot can
            # occur after the browser has already changed pages.
            navigation = _Navigation(requested_url=args.url)
            entry.last_navigation = navigation
            try:
                response = await entry.page.goto(args.url, wait_until="domcontentloaded",
                                                 timeout=15000)
                snapshot = await self._snapshot(entry)
            except Exception as error:
                raise BrowserNavigationUnknown(
                    "Browser navigation outcome unconfirmed; observe the same tab before retrying"
                ) from error
            navigation.outcome = "confirmed"
            snapshot["last_navigation"] = self._navigation_state(navigation)
            snapshot["http_status"] = response.status if response is not None else None
            return snapshot

    async def observe(self, args: BrowserSession, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            return await self._snapshot(entry)

    async def _action(self, args: BrowserClick, *, owner: str | None,
                      value: str | None = None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            # A retained element handle pins the element selected by the preflight.
            # A locator would resolve the selector again after the checks below.
            try:
                target = entry.page.locator("css=" + args.selector)
                if await target.count() != 1:
                    raise ValueError("Browser selector must match exactly one element")
                element = await target.element_handle()
                if (element is None or not await element.is_visible()
                        or not await element.is_enabled()):
                    raise ValueError("Browser target is not visible and enabled")
                if value is not None:
                    editable = await element.evaluate("""el => el.isContentEditable ||
                        (el instanceof HTMLTextAreaElement && !el.readOnly) ||
                        (el instanceof HTMLInputElement && !el.readOnly &&
                         ['text', 'search', 'email', 'number', 'password', 'tel',
                          'url'].includes(el.type))
                    """)
                    if not editable:
                        raise ValueError("Browser target is not editable")
            except ValueError:
                raise
            except Exception as error:
                raise ValueError("Browser target could not be resolved") from error
            try:
                if value is None:
                    await element.click(timeout=10000)
                else:
                    await element.fill(value, timeout=10000)
                snapshot = await self._snapshot(entry)
                if value is not None:
                    observed_value = await element.evaluate(
                        "el => el.isContentEditable ? el.innerText : el.value"
                    )
                    snapshot["value_verified"] = observed_value == value
                return snapshot
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser action outcome unconfirmed; observe the same tab before another action"
                ) from error

    async def click(self, args: BrowserClick, *, owner: str | None) -> dict[str, JsonValue]:
        return await self._action(args, owner=owner)

    async def fill(self, args: BrowserFill, *, owner: str | None) -> dict[str, JsonValue]:
        return await self._action(args, owner=owner, value=args.value)

    async def _snapshot(self, entry: _Entry) -> dict[str, JsonValue]:
        title = await entry.page.title()
        text = await entry.page.evaluate(
            "limit => (document.body?.innerText || '').slice(0, limit + 1)", 16384
        )
        observed_url = entry.page.url
        snapshot: dict[str, JsonValue] = {
            "session_id": entry.session_id, "tab_id": entry.tab_id,
            "url": observed_url, "title": title[:512],
            "text": text[:16384], "text_truncated": len(text) > 16384,
        }
        if entry.last_navigation is not None:
            entry.last_navigation.observed_url = observed_url
            snapshot["last_navigation"] = self._navigation_state(entry.last_navigation)
        return snapshot

    @staticmethod
    def _navigation_state(navigation: _Navigation) -> dict[str, JsonValue]:
        return {"requested_url": navigation.requested_url,
                "outcome": navigation.outcome,
                "observed_url": navigation.observed_url}

    async def stop(self, args: BrowserSession, *, owner: str | None) -> dict[str, JsonValue]:
        async with self._lock:
            entry = self._entry(args, owner, require_live=False)
            async with entry.lock:
                self._entry(args, owner, require_live=False)
                del self.entries[args.session_id]
                try:
                    await entry.browser.close()
                finally:
                    await entry.playwright.stop()
        return {"session_id": args.session_id, "tab_id": args.tab_id, "state": "closed"}

    async def close(self) -> None:
        async with self._lock:
            entries = list(self.entries.values())
            self.entries.clear()
        for entry in entries:
            async with entry.lock:
                try:
                    await entry.browser.close()
                finally:
                    await entry.playwright.stop()
