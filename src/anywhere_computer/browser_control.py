"""Owner-scoped, isolated browser tabs for explicit web navigation and observation."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import JsonValue

from .models import BrowserClick, BrowserFill, BrowserNavigate, BrowserSession

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright


class BrowserNavigationUnknown(Exception):
    """Navigation was attempted but its resulting page could not be confirmed."""


class BrowserActionUnknown(Exception):
    """A page action may have run, but its outcome could not be confirmed."""


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


class BrowserControl:
    def __init__(self, *, channel: str | None = "chrome") -> None:
        self.channel = channel
        self.entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    def _entry(
        self, args: BrowserSession, owner: str | None, *, require_live: bool = True,
    ) -> _Entry:
        entry = self.entries.get(args.session_id)
        if entry is None or entry.owner != owner or entry.tab_id != args.tab_id:
            raise ValueError("Browser session or tab unavailable for this connection")
        if require_live and (entry.page.is_closed() or not entry.browser.is_connected()):
            raise ValueError("Browser session ended; open a new isolated session")
        return entry

    async def open(self, *, owner: str | None) -> dict[str, JsonValue]:
        # Each session gets its own ephemeral browser process and context. No
        # persistent profile or existing user tab is ever attached here.
        async with self._lock:
            if len(self.entries) >= 4:
                raise ValueError("Browser session capacity reached")
            try:
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise ValueError("Playwright browser dependency unavailable") from error
            driver = await async_playwright().start()
            browser = None
            try:
                browser = await driver.chromium.launch(headless=True, channel=self.channel)
                context = await browser.new_context(accept_downloads=False)
                page = await context.new_page()
            except Exception:
                if browser is not None:
                    await browser.close()
                await driver.stop()
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
            try:
                response = await entry.page.goto(args.url, wait_until="domcontentloaded",
                                                 timeout=15000)
                snapshot = await self._snapshot(entry)
            except Exception as error:
                raise BrowserNavigationUnknown(
                    "Browser navigation outcome unconfirmed; observe the same tab before retrying"
                ) from error
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
        return {"session_id": entry.session_id, "tab_id": entry.tab_id,
                "url": entry.page.url, "title": title[:512],
                "text": text[:16384], "text_truncated": len(text) > 16384}

    async def stop(self, args: BrowserSession, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner, require_live=False)
        async with entry.lock:
            self._entry(args, owner, require_live=False)
            try:
                await entry.browser.close()
            finally:
                await entry.playwright.stop()
                del self.entries[args.session_id]
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
