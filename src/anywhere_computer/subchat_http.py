"""Browser-free, read-only ordinary-Chat adapter. Independent generation is unavailable."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .subchat import (
    SubchatAnswer,
    SubchatPendingObservation,
    SubchatReceipt,
    SubchatUnsupported,
)
from .subchat_browser.http_reader import ChatHTTPReader
from .subchat_http_session import ObservedHTTPSession
from .subchat_state import SubchatHTTPSelection, SubchatSubmission

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

CONVERSATION_ID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


class HTTPOnlySubchatBackend:
    """No BrowserContext, DOM adapter, generation method or credential discovery.

    Reuses the existing GET transport, projections and durable operation store.
    A supplied read session is not an independent authentication implementation.
    """

    def __init__(self, request_factory: Callable[[], Awaitable[APIRequestContext]],
                 session: ObservedHTTPSession | None = None) -> None:
        self._http_reader = ChatHTTPReader(request_factory, browser_free=True, session=session)

    def capabilities(self) -> dict[str, object]:
        return {'state': 'capabilities', 'transport': 'http_read_only',
                'browser_required': False, 'generation_transport': 'unavailable',
                'http_selection_send_supported': False, 'credential_refresh': False,
                'independent_login': False, 'persistent_credentials': False,
                'session_source': 'explicit_in_memory_handoff', 'automatic_retry': False}

    def validate_send_selection(self, selection: SubchatHTTPSelection | None) -> None:
        raise SubchatUnsupported('http_generation_unavailable')

    async def prepare(self, submission: SubchatSubmission) -> tuple[str, ...]:
        # Called before the existing service's sending reservation. Never substitute
        # a browser, paid API, Codex turn or Work task after the provider's refusal.
        raise SubchatUnsupported('http_generation_unavailable')

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        raise SubchatUnsupported('http_generation_unavailable')

    async def catalog(self, model: str | None = None) -> dict[str, object]:
        # MCP's default is explicitly UI; do not silently turn that into another source.
        raise SubchatUnsupported('ui_unavailable')

    async def http_catalog(self) -> dict[str, object]:
        async with asyncio.timeout(20):
            result = await self._http_reader.catalog(None)
            result['http_selection_send_supported'] = False
            result['generation_transport'] = 'unavailable'
            return result

    def _has_identity(self, submission: SubchatSubmission) -> bool:
        if submission.conversation_id is None or submission.user_message_id is None:
            return False
        if CONVERSATION_ID.fullmatch(submission.conversation_id) is None:
            raise ValueError('Invalid saved conversation identity')
        return True

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if not self._has_identity(submission):
            return None  # No history scan, fabricated identity, bootstrap tab or resend.
        async with asyncio.timeout(20):
            return await self._http_reader.receipt(None, submission)

    async def read_answer(self, submission: SubchatSubmission
                          ) -> SubchatAnswer | SubchatPendingObservation | None:
        if not self._has_identity(submission):
            return None
        async with asyncio.timeout(20):
            return await self._http_reader.history(None, submission)
