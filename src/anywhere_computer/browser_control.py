"""Owner-scoped, isolated browser tabs for explicit web navigation and observation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import JsonValue

from .files import absolute_path, read_bytes, sha256
from .models import (
    BrowserClick,
    BrowserConsole,
    BrowserDownload,
    BrowserDrag,
    BrowserFileUpload,
    BrowserFill,
    BrowserHover,
    BrowserKey,
    BrowserNavigate,
    BrowserNetwork,
    BrowserObserve,
    BrowserResearch,
    BrowserScroll,
    BrowserSelect,
    BrowserSession,
    BrowserSource,
    BrowserTarget,
)

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        ConsoleMessage,
        Frame,
        Locator,
        Page,
        Playwright,
        Request,
        Response,
    )


class BrowserNavigationUnknown(Exception):
    """Navigation was attempted but its resulting page could not be confirmed."""


class BrowserActionUnknown(Exception):
    """A page action may have run, but its outcome could not be confirmed."""


class BrowserStartupUnavailable(Exception):
    """No isolated tab was registered after local browser startup failed."""


_CLEANUP_WAIT_SECONDS = 5.0
_LOG = logging.getLogger(__name__)
_IMAGE_LIMIT = 1024 * 1024
_SOURCE_LIMIT = 32768
_NETWORK_LIMIT = 100
_CONSOLE_LIMIT = 100
_FRAME_LIMIT = 32
_BROWSER_DOWNLOAD_LIMIT = 64 * 1024 * 1024
_FORM_CONTROLS_SCRIPT = r"""() => {
    const clean = value => String(value || '').replace(/[\r\n\t\u2028\u2029\u00a0]/g, ' ')
        .replace(/\s+/gu, ' ').trim().slice(0, 256);
    const controls = [];
    let inspected = 0;
    let truncated = false;
    const roots = [document];
    for (let index = 0; index < roots.length; index++) {
      const root = roots[index];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      let element;
      while ((element = walker.nextNode())) {
        if (++inspected > 4096 || controls.length >= 50) { truncated = true; break; }
        if (element.shadowRoot) roots.push(element.shadowRoot);
        if (!element.matches('input, textarea, select, button, [role="textbox"], '
                             + '[role="combobox"], [role="button"]')) continue;
        const style = getComputedStyle(element);
        if (element.getAttribute('type') === 'hidden' || style.display === 'none'
            || style.visibility === 'hidden' || element.getClientRects().length === 0
            || element.closest('[aria-hidden="true"]')) continue;
        const labels = Array.from(element.labels || [], label =>
            clean(label.innerText || label.textContent)).filter(Boolean).slice(0, 4);
        const ariaLabel = clean(element.getAttribute('aria-label'));
        const labelledBy = clean((element.getAttribute('aria-labelledby') || '')
            .split(/\s+/).map(id => {
                const label = root.getElementById(id);
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
            in_shadow_dom: root !== document,
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
      if (truncated) break;
    }
    return {controls, truncated};
}"""


def _readable_lines(value: str) -> str:
    """Keep meaningful indentation while normalizing display-only line breaks."""
    normalized = (value.replace("\r\n", "\n").replace("\r", "\n")
                  .replace("\u2028", "\n").replace("\u2029", "\n"))
    return re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", normalized)


def _network_url(value: str) -> dict[str, JsonValue]:
    """Expose a request route without URL credentials, fragments or query values."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return {"scheme": parsed.scheme[:32] or "unknown", "route": None}
        host = parsed.hostname[:253]
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        route = f"{parsed.scheme}://{host}{port}{parsed.path[:512]}"
        keys: list[JsonValue] = [part.partition("=")[0][:64]
                                 for part in parsed.query.split("&") if part]
        return {"scheme": parsed.scheme, "route": route,
                "route_truncated": len(parsed.path) > 512,
                "query_keys": keys[:16], "query_keys_truncated": len(keys) > 16}
    except ValueError:
        return {"scheme": "invalid", "route": None}


def _save_download(source_path: str, destination_path: str) -> tuple[int, str]:
    """Copy a completed browser download to an unused destination, without replacement."""
    destination = absolute_path(destination_path)
    digest = hashlib.sha256()
    copied = 0
    temporary: str | None = None
    try:
        with open(source_path, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _BROWSER_DOWNLOAD_LIMIT:
                raise ValueError("Browser download is not a regular file within the 64 MiB limit")
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".anywhere-browser-", delete=False
            ) as output:
                temporary = output.name
                while block := source.read(262144):
                    copied += len(block)
                    if copied > _BROWSER_DOWNLOAD_LIMIT or copied > info.st_size:
                        raise ValueError("Browser download changed or exceeded the 64 MiB limit")
                    output.write(block)
                    digest.update(block)
                if copied != info.st_size or os.fstat(source.fileno()).st_size != info.st_size:
                    raise ValueError("Browser download changed during copying")
                output.flush()
                os.fsync(output.fileno())
        os.link(temporary, destination)
        return copied, digest.hexdigest()
    finally:
        if temporary is not None:
            os.unlink(temporary)


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
class _NetworkEvent:
    id: int
    data: dict[str, JsonValue]


@dataclass
class _ConsoleEvent:
    id: int
    data: dict[str, JsonValue]


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
    snapshot_frame_id: str | None = None
    document_revision: int = 0
    frame_revisions: dict[Frame, int] = field(default_factory=dict)
    observed_frames: dict[str, Frame] = field(default_factory=dict)
    network_events: deque[_NetworkEvent] = field(
        default_factory=lambda: deque(maxlen=_NETWORK_LIMIT)
    )
    network_next_id: int = 0
    console_events: deque[_ConsoleEvent] = field(
        default_factory=lambda: deque(maxlen=_CONSOLE_LIMIT)
    )
    console_next_id: int = 0


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

    @staticmethod
    def _record_response(entry: _Entry, response: Response) -> None:
        request = response.request
        entry.network_next_id += 1
        entry.network_events.append(_NetworkEvent(entry.network_next_id, {
            "id": entry.network_next_id, "event": "response",
            "method": request.method[:16], "resource_type": request.resource_type[:32],
            "status": response.status, **_network_url(response.url),
        }))

    @staticmethod
    def _record_failure(entry: _Entry, request: Request) -> None:
        entry.network_next_id += 1
        entry.network_events.append(_NetworkEvent(entry.network_next_id, {
            "id": entry.network_next_id, "event": "request_failed",
            "method": request.method[:16], "resource_type": request.resource_type[:32],
            "failure": str(request.failure or "network_error")[:128],
            **_network_url(request.url),
        }))

    @staticmethod
    def _record_console(entry: _Entry, message: ConsoleMessage) -> None:
        entry.console_next_id += 1
        raw_text = _readable_lines(message.text)
        location = message.location
        entry.console_events.append(_ConsoleEvent(entry.console_next_id, {
            "id": entry.console_next_id, "event": "console",
            "type": message.type[:32], "text": raw_text[:1024],
            "text_truncated": len(raw_text) > 1024,
            "source": _network_url(str(location.get("url", ""))),
            "line": location.get("lineNumber", 0),
            "column": location.get("columnNumber", 0),
        }))

    @staticmethod
    def _record_page_error(entry: _Entry, error: Exception) -> None:
        entry.console_next_id += 1
        raw_text = _readable_lines(str(error))
        entry.console_events.append(_ConsoleEvent(entry.console_next_id, {
            "id": entry.console_next_id, "event": "page_error",
            "type": "error", "text": raw_text[:1024],
            "text_truncated": len(raw_text) > 1024,
        }))

    @staticmethod
    def _document_changed(entry: _Entry, frame: Frame) -> None:
        # URL equality does not detect a reload or navigation to the same URL.
        if frame is entry.page.main_frame:
            entry.document_revision += 1
            entry.snapshot_id = None
        else:
            entry.frame_revisions[frame] = entry.frame_revisions.get(frame, 0) + 1
            if entry.observed_frames.get(entry.snapshot_frame_id or "") is frame:
                entry.snapshot_id = None

    @staticmethod
    def _scope(entry: _Entry, frame_id: str | None) -> Frame:
        if frame_id is None:
            return entry.page.main_frame
        frame = entry.observed_frames.get(frame_id)
        if frame is None or frame.is_detached() or frame not in entry.page.frames:
            raise ValueError("Browser frame unavailable; observe the tab again")
        return frame

    @staticmethod
    def _frame_rows(entry: _Entry) -> tuple[list[JsonValue], bool]:
        frames = [frame for frame in entry.page.frames if frame is not entry.page.main_frame]
        previous = {frame: frame_id for frame_id, frame in entry.observed_frames.items()}
        entry.observed_frames = {
            previous.get(frame, uuid.uuid4().hex): frame for frame in frames[:_FRAME_LIMIT]
        }
        entry.frame_revisions = {
            frame: revision for frame, revision in entry.frame_revisions.items() if frame in frames
        }
        identifiers = {frame: frame_id for frame_id, frame in entry.observed_frames.items()}
        rows: list[JsonValue] = [{
            "frame_id": frame_id, "name": frame.name[:256],
            "url": _network_url(frame.url),
            "parent_frame_id": (identifiers.get(frame.parent_frame)
                                if frame.parent_frame is not None else None),
        } for frame_id, frame in entry.observed_frames.items()]
        return rows, len(frames) > _FRAME_LIMIT

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
                context = await browser.new_context(accept_downloads=True)
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
            entry = _Entry(owner, session_id, tab_id, driver, browser, context, page)
            page.on("response", lambda response: self._record_response(entry, response))
            page.on("requestfailed", lambda request: self._record_failure(entry, request))
            page.on("console", lambda message: self._record_console(entry, message))
            page.on("pageerror", lambda error: self._record_page_error(entry, error))
            page.on("framenavigated", lambda frame: self._document_changed(entry, frame))
            page.on("framedetached", lambda frame: self._document_changed(entry, frame))
            self.entries[session_id] = entry
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
                entry, include_image=isinstance(args, BrowserObserve) and args.include_image,
                frame_id=args.frame_id if isinstance(args, BrowserObserve) else None,
            )

    async def source(self, args: BrowserSource, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            scope = self._scope(entry, args.frame_id)
            if args.selector is None:
                result = await scope.evaluate("""limit => {
                    const html = document.documentElement?.outerHTML || '';
                    return {html: html.slice(0, limit), total_characters: html.length};
                }""", _SOURCE_LIMIT)
            else:
                target = scope.locator("css=" + args.selector)
                if await target.count() != 1:
                    raise ValueError("Browser source selector must match exactly one element")
                result = await target.evaluate("""(element, limit) => {
                    const html = element.outerHTML;
                    return {html: html.slice(0, limit), total_characters: html.length};
                }""", _SOURCE_LIMIT)
            return {"session_id": entry.session_id, "tab_id": entry.tab_id,
                    "url": scope.url, "frame_id": args.frame_id,
                    "source_kind": "current_dom_outer_html",
                    "selector": args.selector, "html": result["html"],
                    "total_characters": result["total_characters"],
                    "truncated": result["total_characters"] > _SOURCE_LIMIT}

    async def network(self, args: BrowserNetwork, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            rows = [event for event in entry.network_events if event.id > args.after_id]
            first_id = entry.network_events[0].id if entry.network_events else None
            next_id = rows[min(len(rows), args.limit) - 1].id if rows else args.after_id
            events: list[JsonValue] = [event.data for event in rows[:args.limit]]
            return {"session_id": entry.session_id, "tab_id": entry.tab_id,
                    "events": events, "latest_id": entry.network_next_id,
                    "next_id": next_id,
                    "history_truncated": first_id is not None and args.after_id < first_id - 1,
                    "page_url": _network_url(entry.page.url)}

    async def console(self, args: BrowserConsole, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            rows = [event for event in entry.console_events if event.id > args.after_id]
            first_id = entry.console_events[0].id if entry.console_events else None
            next_id = rows[min(len(rows), args.limit) - 1].id if rows else args.after_id
            events: list[JsonValue] = [event.data for event in rows[:args.limit]]
            return {"session_id": entry.session_id, "tab_id": entry.tab_id,
                    "events": events, "latest_id": entry.console_next_id,
                    "next_id": next_id,
                    "history_truncated": first_id is not None and args.after_id < first_id - 1,
                    "page_url": _network_url(entry.page.url)}

    async def research(self, args: BrowserResearch, *, owner: str | None
                       ) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            scope = self._scope(entry, args.frame_id)
            result = await scope.evaluate("""limit => {
                const clean = value => String(value || '').replace(/\\s+/gu, ' ')
                    .trim().slice(0, 256);
                const meta = (...names) => {
                    for (const name of names) {
                        const query = `meta[name="${name}"],meta[property="${name}"]`;
                        const el = document.querySelector(query);
                        if (el?.content) return clean(el.content);
                    }
                    return null;
                };
                const headings = Array.from(document.querySelectorAll('h1,h2'), el =>
                    clean(el.innerText || el.textContent)).filter(Boolean).slice(0, 10);
                const links = [];
                let inspected = 0;
                for (const anchor of document.querySelectorAll('a[href]')) {
                    if (++inspected > 512 || links.length >= limit) break;
                    if (anchor.getClientRects().length === 0) continue;
                    try {
                        const url = new URL(anchor.href, document.baseURI);
                        if (!['http:', 'https:'].includes(url.protocol)) continue;
                        const label = clean(anchor.innerText || anchor.getAttribute('aria-label')
                            || anchor.title);
                        links.push({label, href: url.href,
                            external: url.origin !== location.origin});
                    } catch (_) { /* Ignore malformed page links. */ }
                }
                return {
                    title: clean(document.title),
                    canonical: document.querySelector('link[rel="canonical"]')?.href || null,
                    publisher: meta('og:site_name', 'publisher'),
                    author: meta('author', 'article:author'),
                    published: meta('article:published_time', 'datePublished', 'date'),
                    description: meta('description', 'og:description'),
                    headings, links,
                    links_truncated: inspected > 512 || links.length >= limit,
                };
            }""", args.link_limit)
            links = [{"label": item["label"], "destination": _network_url(item["href"]),
                      "external": item["external"]} for item in result["links"]]
            return {"session_id": entry.session_id, "tab_id": entry.tab_id,
                    "source_kind": "current_dom_claims", "observed_at_unix": time.time(),
                    "page_url": _network_url(scope.url), "frame_id": args.frame_id,
                    "title": result["title"],
                    "canonical": (_network_url(result["canonical"])
                                  if result["canonical"] else None),
                    "publisher_claim": result["publisher"], "author_claim": result["author"],
                    "published_claim": result["published"],
                    "description_claim": result["description"],
                    "headings": result["headings"], "links": links,
                    "links_truncated": result["links_truncated"]}

    @classmethod
    def _target_locator(cls, entry: _Entry, args: BrowserClick | BrowserTarget,
                        frame_id: str | None = None) -> Locator:
        scope = cls._scope(entry, frame_id)
        if args.selector is not None:
            return scope.locator("css=" + args.selector)
        if args.role is not None:
            return scope.get_by_role(args.role, name=args.name, exact=True)
        assert args.label is not None
        return scope.get_by_label(args.label, exact=True)

    @classmethod
    def _check_snapshot(cls, entry: _Entry, snapshot_id: str | None,
                        frame_id: str | None = None) -> None:
        scope = cls._scope(entry, frame_id)
        if snapshot_id is not None and (
            snapshot_id != entry.snapshot_id
            or time.monotonic() - entry.snapshot_at > 60
            or scope.url != entry.snapshot_url
            or frame_id != entry.snapshot_frame_id
        ):
            raise ValueError("Browser snapshot is stale; observe the tab again")

    @classmethod
    async def _unique_target(cls, entry: _Entry, target: BrowserTarget,
                             *, enabled: bool = False, frame_id: str | None = None) -> Locator:
        try:
            locator = cls._target_locator(entry, target, frame_id)
            if await locator.count() > 1:
                raise ValueError("Browser target must match exactly one element")
            await locator.wait_for(state="visible", timeout=3000)
            if await locator.count() != 1:
                raise ValueError("Browser target must match exactly one element")
            if enabled and not await locator.is_enabled():
                raise ValueError("Browser target is disabled")
            return locator
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("Browser target could not be resolved") from error

    async def _action(self, args: BrowserClick, *, owner: str | None,
                      value: str | None = None,
                      key: str | None = None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            # User-facing role/label locators can resolve the current DOM after a
            # framework rerender. Preserve the exact-one preflight and let Playwright
            # check actionability again at dispatch rather than pinning a stale handle.
            try:
                target = self._target_locator(entry, args, args.frame_id)
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
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            try:
                if key is not None:
                    await target.press(key, timeout=10000)
                elif value is None:
                    await target.click(timeout=10000)
                else:
                    await target.fill(value, timeout=10000)
                snapshot = await self._snapshot(entry, frame_id=args.frame_id)
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

    async def key(self, args: BrowserKey, *, owner: str | None) -> dict[str, JsonValue]:
        return await self._action(args, owner=owner, key=args.key)

    async def drag(self, args: BrowserDrag, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            try:
                source = self._target_locator(entry, args.source, args.frame_id)
                target = self._target_locator(entry, args.target, args.frame_id)
                for locator in (source, target):
                    if await locator.count() != 1:
                        raise ValueError("Browser drag target must match exactly one element")
                    if not await locator.is_visible():
                        raise ValueError("Browser drag target is not visible")
                if not await source.is_enabled():
                    raise ValueError("Browser drag source is disabled")
            except ValueError:
                raise
            except Exception as error:
                raise ValueError("Browser drag target could not be resolved") from error
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            try:
                await source.drag_to(target, timeout=10000)
                return await self._snapshot(entry, frame_id=args.frame_id)
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser drag outcome unconfirmed; observe the same tab before another action"
                ) from error

    async def hover(self, args: BrowserHover, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            target = await self._unique_target(entry, args.target, frame_id=args.frame_id)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            try:
                await target.hover(timeout=10000)
                return await self._snapshot(entry, frame_id=args.frame_id)
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser hover outcome unconfirmed; observe the same tab before another action"
                ) from error

    async def select(self, args: BrowserSelect, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            target = await self._unique_target(
                entry, args.target, enabled=True, frame_id=args.frame_id,
            )
            try:
                matches = await target.evaluate("""(el, choice) => {
                    if (!(el instanceof HTMLSelectElement)) return null;
                    return Array.from(el.options).filter(option =>
                        !option.disabled && !option.parentElement?.disabled &&
                        (choice.value !== null ? option.value === choice.value
                                               : option.label === choice.label))
                        .map(option => ({value: option.value, label: option.label}));
                }""", {"value": args.value, "label": args.label})
            except Exception as error:
                raise ValueError("Browser select option could not be resolved") from error
            if matches is None:
                raise ValueError("Browser target is not a select element")
            if len(matches) != 1:
                raise ValueError("Browser option must match exactly one enabled option")
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            try:
                if args.value is not None:
                    selected = await target.select_option(value=args.value, timeout=10000)
                else:
                    selected = await target.select_option(label=args.label, timeout=10000)
                snapshot = await self._snapshot(entry, frame_id=args.frame_id)
                snapshot["selected_option"] = matches[0]
                snapshot["selection_verified"] = selected == [matches[0]["value"]]
                return snapshot
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser selection outcome unconfirmed; observe the same tab before retrying"
                ) from error

    async def scroll(self, args: BrowserScroll, *, owner: str | None) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            target = await self._unique_target(entry, args.target, frame_id=args.frame_id)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            try:
                offsets = await target.evaluate("""(el, delta) => {
                    const scroller = el === document.body
                        ? (document.scrollingElement || el) : el;
                    const before = {x: scroller.scrollLeft, y: scroller.scrollTop};
                    scroller.scrollTo({left: before.x + delta.x, top: before.y + delta.y,
                                     behavior: 'instant'});
                    return {before, after: {x: scroller.scrollLeft, y: scroller.scrollTop}};
                }""", {"x": args.delta_x, "y": args.delta_y})
                snapshot = await self._snapshot(entry, frame_id=args.frame_id)
                snapshot["scroll"] = {**offsets,
                    "changed": offsets["before"] != offsets["after"]}
                return snapshot
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser scroll outcome unconfirmed; observe the same tab before another action"
                ) from error

    async def file_upload(self, args: BrowserFileUpload, *, owner: str | None
                          ) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            path = absolute_path(args.path)
            content = await asyncio.to_thread(read_bytes, path)
            try:
                target = self._target_locator(entry, args.target, args.frame_id)
                if await target.count() != 1:
                    raise ValueError("Browser file input must match exactly one element")
                if not await target.evaluate(
                    "el => el instanceof HTMLInputElement && el.type === 'file'"
                ):
                    raise ValueError("Browser target is not a file input")
                if not await target.is_enabled():
                    raise ValueError("Browser file input is disabled")
            except ValueError:
                raise
            except Exception as error:
                raise ValueError("Browser file input could not be resolved") from error
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                await target.set_input_files(
                    {"name": path.name, "mimeType": mime, "buffer": content}, timeout=10000
                )
                snapshot = await self._snapshot(entry, frame_id=args.frame_id)
                snapshot["selected_file"] = {"name": path.name, "bytes": len(content),
                                             "sha256": sha256(content)}
                return snapshot
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser file input outcome unconfirmed; observe the same tab before retrying"
                ) from error

    async def download(self, args: BrowserDownload, *, owner: str | None
                       ) -> dict[str, JsonValue]:
        entry = self._entry(args, owner)
        async with entry.lock:
            self._entry(args, owner)
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            destination = absolute_path(args.path)
            if not destination.parent.is_dir() or os.path.lexists(destination):
                raise ValueError(
                    "Browser download requires an unused path in an existing directory"
                )
            try:
                target = self._target_locator(entry, args.target, args.frame_id)
                if await target.count() != 1:
                    raise ValueError("Browser download target must match exactly one element")
                if not await target.is_visible() or not await target.is_enabled():
                    raise ValueError("Browser download target is not visible and enabled")
            except ValueError:
                raise
            except Exception as error:
                raise ValueError("Browser download target could not be resolved") from error
            self._check_snapshot(entry, args.snapshot_id, args.frame_id)
            entry.snapshot_id = None
            try:
                async with entry.page.expect_download(timeout=15000) as pending:
                    await target.click(timeout=10000)
                received = await pending.value
                source = await received.path()
                if source is None:
                    raise ValueError("Browser download file is unavailable")
                size, digest = await asyncio.to_thread(_save_download, str(source), args.path)
                snapshot = await self._snapshot(entry, frame_id=args.frame_id)
                snapshot["download"] = {
                    "path": str(destination), "bytes": size, "sha256": digest,
                    "suggested_filename": received.suggested_filename[:255],
                    "url": _network_url(received.url),
                }
                return snapshot
            except Exception as error:
                raise BrowserActionUnknown(
                    "Browser download outcome unconfirmed; inspect the tab and destination path "
                    "before another action"
                ) from error

    async def _snapshot(self, entry: _Entry, *, include_image: bool = False,
                        frame_id: str | None = None,
                        ) -> dict[str, JsonValue]:
        scope = self._scope(entry, frame_id)
        revision = entry.document_revision
        frame_revision = entry.frame_revisions.get(scope, 0)
        frames, frames_truncated = self._frame_rows(entry)
        title = await scope.title()
        text = await scope.evaluate(
            "limit => (document.body?.innerText || '').slice(0, limit + 1)", 16384
        )
        observed_url = scope.url
        snapshot: dict[str, JsonValue] = {
            "session_id": entry.session_id, "tab_id": entry.tab_id,
            "url": observed_url, "title": title[:512],
            "text": _readable_lines(text[:16384]), "text_truncated": len(text) > 16384,
            "frame_id": frame_id, "frames": frames, "frames_truncated": frames_truncated,
            "form_control_coordinate_space": "frame_viewport_css_pixels",
        }
        try:
            form_controls = await scope.evaluate(_FORM_CONTROLS_SCRIPT)
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
                                          "scope": "tab_viewport",
                                          "width": viewport["width"] if viewport else None,
                                          "height": viewport["height"] if viewport else None}
            except Exception:
                snapshot["visual_unavailable"] = "capture_failed"
        try:
            semantic_tree = await scope.locator("body").aria_snapshot(timeout=3000)
        except Exception:
            # Accessible structure is supplementary; URL, title and visible
            # text still give a useful observation if the page has no body.
            snapshot["semantic_tree_unavailable"] = True
        else:
            snapshot["semantic_tree"] = _readable_lines(semantic_tree[:16384])
            snapshot["semantic_tree_truncated"] = len(semantic_tree) > 16384
        if (revision != entry.document_revision or scope.is_detached()
                or frame_revision != entry.frame_revisions.get(scope, 0)):
            raise ValueError("Browser document changed during observation; observe the tab again")
        entry.snapshot_id = uuid.uuid4().hex
        entry.snapshot_at = time.monotonic()
        entry.snapshot_url = observed_url
        entry.snapshot_frame_id = frame_id
        snapshot["snapshot_id"] = entry.snapshot_id
        if entry.last_navigation is not None:
            entry.last_navigation.observed_url = entry.page.url
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
