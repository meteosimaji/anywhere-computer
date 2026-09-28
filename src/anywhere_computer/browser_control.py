"""Owner-scoped, isolated browser tabs for explicit web navigation and observation."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import sys
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import JsonValue

from .models import BrowserClick, BrowserFill, BrowserNavigate, BrowserObserve, BrowserSession

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
_IMAGE_LIMIT = 1024 * 1024
_FORM_CONTROLS_SCRIPT = r"""() => {
    const clean = value => String(value || '').replace(/[\r\n\t\u2028\u2029\u00a0]/g, ' ')
        .replace(/\s+/gu, ' ').trim().slice(0, 256);
    const controls = [];
    let inspected = 0;
    let truncated = false;
    for (const element of document.querySelectorAll(
        'input, textarea, select, button, [role="textbox"], [role="combobox"], [role="button"]'
    )) {
        if (++inspected > 512 || controls.length >= 50) { truncated = true; break; }
        const style = getComputedStyle(element);
        if (element.getAttribute('type') === 'hidden' || style.display === 'none'
            || style.visibility === 'hidden' || element.getClientRects().length === 0
            || element.closest('[aria-hidden="true"]')) continue;
        const labels = Array.from(element.labels || [], label =>
            clean(label.innerText || label.textContent)).filter(Boolean).slice(0, 4);
        const ariaLabel = clean(element.getAttribute('aria-label'));
        const labelledBy = clean((element.getAttribute('aria-labelledby') || '')
            .split(/\s+/).map(id => {
                const label = document.getElementById(id);
                return label?.innerText || label?.textContent || '';
            }).join(' '));
        const tag = element.tagName.toLowerCase();
        const buttonText = tag === 'button' || element.getAttribute('role') === 'button'
            ? clean(element.innerText || element.textContent) : '';
        const title = clean(element.getAttribute('title'));
        const label = ariaLabel || labelledBy || labels.join(' ') || buttonText || title;
        const source = ariaLabel ? 'aria-label' : labelledBy ? 'aria-labelledby'
            : labels.length ? 'html-label' : buttonText ? 'button-text'
            : title ? 'title' : null;
        const item = {tag, label, label_source: source,
            disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
            required: Boolean(element.required
                              || element.getAttribute('aria-required') === 'true')};
        const rect = element.getBoundingClientRect();
        const rounded = value => Math.round(value * 10) / 10;
        item.box = {x: rounded(rect.x), y: rounded(rect.y),
            width: rounded(rect.width), height: rounded(rect.height)};
        item.in_viewport = rect.width > 0 && rect.height > 0 && rect.right > 0
            && rect.bottom > 0 && rect.left < innerWidth && rect.top < innerHeight;
        if (labels.length) item.html_labels = labels;
        if (tag === 'input') item.input_type = clean(element.getAttribute('type') || 'text');
        const role = clean(element.getAttribute('role'));
        if (role) item.role_attribute = role;
        const id = clean(element.id);
        if (id) item.id = id.slice(0, 128);
        const placeholder = clean(element.getAttribute('placeholder'));
        if (placeholder) item.placeholder = placeholder;
        controls.push(item);
    }
    return {controls, truncated};
}"""


def _readable_lines(value: str) -> str:
    """Keep meaningful indentation while normalizing display-only line breaks."""
    normalized = (value.replace("\r\n", "\n").replace("\r", "\n")
                  .replace("\u2028", "\n").replace("\u2029", "\n"))
    return re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", normalized)


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
    snapshot_id: str | None = None
    snapshot_at: float = 0.0
    snapshot_url: str | None = None


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
            entry.snapshot_id = None
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
            return await self._snapshot(
                entry, include_image=isinstance(args, BrowserObserve) and args.include_image
            )

    async def _action(self, args: BrowserClick, *, owner: str | None,
                      value: str | None = None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            if args.snapshot_id is not None:
                if (args.snapshot_id != entry.snapshot_id
                        or time.monotonic() - entry.snapshot_at > 60
                        or entry.page.url != entry.snapshot_url):
                    raise ValueError("Browser snapshot is stale; observe the tab again")
            # User-facing role/label locators can resolve the current DOM after a
            # framework rerender. Preserve the exact-one preflight and let Playwright
            # check actionability again at dispatch rather than pinning a stale handle.
            try:
                if args.selector is not None:
                    target = entry.page.locator("css=" + args.selector)
                elif args.role is not None:
                    target = entry.page.get_by_role(args.role, name=args.name, exact=True)
                else:
                    assert args.label is not None
                    target = entry.page.get_by_label(args.label, exact=True)
                if await target.count() > 1:
                    raise ValueError("Browser target must match exactly one element")
                await target.wait_for(state="visible", timeout=3000)
                if await target.count() != 1:
                    raise ValueError("Browser target must match exactly one element")
                if not await target.is_enabled():
                    raise ValueError("Browser target is not visible and enabled")
                if value is not None:
                    editable = await target.evaluate("""el => el.isContentEditable ||
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
            entry.snapshot_id = None
            try:
                if value is None:
                    await target.click(timeout=10000)
                else:
                    await target.fill(value, timeout=10000)
                snapshot = await self._snapshot(entry)
                if value is not None:
                    observed_value = await target.evaluate(
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

    async def _snapshot(self, entry: _Entry, *, include_image: bool = False
                        ) -> dict[str, JsonValue]:
        title = await entry.page.title()
        text = await entry.page.evaluate(
            "limit => (document.body?.innerText || '').slice(0, limit + 1)", 16384
        )
        observed_url = entry.page.url
        snapshot: dict[str, JsonValue] = {
            "session_id": entry.session_id, "tab_id": entry.tab_id,
            "url": observed_url, "title": title[:512],
            "text": _readable_lines(text[:16384]), "text_truncated": len(text) > 16384,
        }
        try:
            form_controls = await entry.page.evaluate(_FORM_CONTROLS_SCRIPT)
        except Exception:
            snapshot["form_controls_unavailable"] = True
        else:
            snapshot["form_controls"] = form_controls["controls"]
            snapshot["form_controls_truncated"] = form_controls["truncated"]
        if include_image:
            try:
                picture = await entry.page.screenshot(type="jpeg", quality=65,
                                                      full_page=False, scale="css",
                                                      timeout=5000)
                if len(picture) > _IMAGE_LIMIT:
                    picture = await entry.page.screenshot(type="jpeg", quality=40,
                                                          full_page=False, scale="css",
                                                          timeout=5000)
                if len(picture) > _IMAGE_LIMIT:
                    snapshot["visual_unavailable"] = "image_too_large"
                else:
                    snapshot["content"] = [{"type": "image", "mimeType": "image/jpeg",
                                            "data": base64.b64encode(picture).decode("ascii")}]
                    viewport = entry.page.viewport_size
                    snapshot["visual"] = {"kind": "rendered_viewport", "mime_type": "image/jpeg",
                                          "bytes": len(picture), "coordinate_unit": "css_px",
                                          "capture_mode": "sequential",
                                          "width": viewport["width"] if viewport else None,
                                          "height": viewport["height"] if viewport else None}
            except Exception:
                snapshot["visual_unavailable"] = "capture_failed"
        try:
            semantic_tree = await entry.page.locator("body").aria_snapshot(timeout=3000)
        except Exception:
            # Accessible structure is supplementary; URL, title and visible
            # text still give a useful observation if the page has no body.
            snapshot["semantic_tree_unavailable"] = True
        else:
            snapshot["semantic_tree"] = _readable_lines(semantic_tree[:16384])
            snapshot["semantic_tree_truncated"] = len(semantic_tree) > 16384
        entry.snapshot_id = uuid.uuid4().hex
        entry.snapshot_at = time.monotonic()
        entry.snapshot_url = observed_url
        snapshot["snapshot_id"] = entry.snapshot_id
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
