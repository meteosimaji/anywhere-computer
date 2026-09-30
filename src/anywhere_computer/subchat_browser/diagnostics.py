"""Bounded ownership of a blocked tab, independent of send/recovery state."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from playwright.async_api import Page

RETENTION_SECONDS = 300.0
DiagnosticAction = Literal['status', 'show', 'close']
logger = logging.getLogger(__name__)


class BrowserDiagnostics:
    def __init__(self, close_page: Callable[[Page], Awaitable[None]]) -> None:
        self._close_page = close_page
        self._page: Page | None = None
        self._expires_at = 0.0
        self._expiry: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closing = False

    def is_retained(self) -> bool:
        return (self.has_resources() and not self._closing
                and time.monotonic() < self._expires_at)

    def has_resources(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def retain(self, page: Page) -> None:
        """Only callers which just created the blocked page may transfer it here."""
        async with self._lock:
            await self._close()
            if self.has_resources():
                raise RuntimeError('Blocked browser tab cleanup is unconfirmed')
            self._page = page
            self._closing = False
            self._expires_at = time.monotonic() + RETENTION_SECONDS

            async def expire() -> None:
                await asyncio.sleep(RETENTION_SECONDS)
                async with self._lock:
                    if self._page is page:
                        await self._close()

            self._expiry = asyncio.create_task(expire())

    async def _close(self) -> None:
        expiry, self._expiry = self._expiry, None
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        page = self._page
        self._closing = True
        if page is not None and not page.is_closed():
            try:
                await self._close_page(page)
            except Exception as error:
                logger.warning('Blocked tab cleanup incomplete: error_type=%s',
                               type(error).__name__)
        # A bounded close helper may return after both attempts failed. Preserve
        # ownership and activity until the actual target is confirmed closed.
        if page is None or page.is_closed():
            self._page = None

    async def inspect(self, action: DiagnosticAction) -> dict[str, object]:
        """Never acquire a context, navigate, export content or reset authorization."""
        if action not in ('status', 'show', 'close'):
            raise ValueError('Unknown browser diagnostic action')
        async with self._lock:
            if action == 'close' or (not self._closing
                                    and time.monotonic() >= self._expires_at):
                await self._close()
            page = self._page
            result: dict[str, object] = {
                'state': 'unavailable', 'automatic_retry': False,
                'challenge_cleared': False, 'new_session_required': True,
                'cleanup_confirmed': not self.has_resources(),
            }
            if page is None or page.is_closed():
                return result
            if self._closing:
                result['state'] = 'cleanup_pending'
                return result
            result.update(state='retained',
                          remaining_seconds=min(RETENTION_SECONDS,
                              max(0.0, self._expires_at - time.monotonic())))
            if action == 'show':
                # Resolve the window through this exact owned page, not a global
                # Chrome window/profile selector. No navigation or challenge click.
                async with asyncio.timeout(10):
                    session = await page.context.new_cdp_session(page)
                    try:
                        target = (await session.send('Target.getTargetInfo'))['targetInfo']
                        if target.get('type') != 'page' or not target.get('targetId'):
                            raise ValueError('Blocked tab target is unavailable')
                        window = await session.send('Browser.getWindowForTarget', {
                            'targetId': target['targetId']})
                        await session.send('Browser.setWindowBounds', {
                            'windowId': window['windowId'], 'bounds': {'windowState': 'normal'}})
                        # Windows background mode starts outside the desktop.
                        # An explicit display request must return this window to it.
                        await session.send('Browser.setWindowBounds', {
                            'windowId': window['windowId'],
                            'bounds': {'left': 80, 'top': 80, 'width': 1000, 'height': 750}})
                        await page.bring_to_front()
                        result.update(state='display_requested',
                                      document_visibility=await page.evaluate(
                                          'document.visibilityState'))
                        if sys.platform == 'darwin':
                            from .background import reveal_background_page

                            result['native_display'] = await reveal_background_page(page)
                    finally:
                        await session.detach()
                # A CDP acknowledgement is not proof of an OS foreground window,
                # and displaying the tab is never evidence the challenge cleared.
            return result
