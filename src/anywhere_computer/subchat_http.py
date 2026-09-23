"""Browser-free ordinary-Chat adapter with optional generation and deletion."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from .subchat import (
    SubchatAnswer,
    SubchatPendingObservation,
    SubchatPreparedSend,
    SubchatReceipt,
    SubchatUnsupported,
)
from .subchat_browser.catalog import require_http_selection
from .subchat_browser.http_reader import ChatHTTPReader
from .subchat_http_download import MAX_FILE_BYTES
from .subchat_http_generation import ObservedHTTPGeneration, dispatch_generation
from .subchat_http_sender import HTTPFollowupParent, HTTPGenerationPlan
from .subchat_http_session import ObservedHTTPSession
from .subchat_state import (
    SubchatHTTPSelection,
    SubchatSelectionError,
    SubchatSubmission,
    SubchatSubmissions,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

    from .subchat_http_download import SandboxDownload

CONVERSATION_ID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


class HTTPOnlySubchatBackend:
    """No BrowserContext, DOM adapter or credential discovery.

    Reuses the existing HTTP transport, projections and durable operation store.
    A supplied read session is not an independent authentication implementation.
    """

    def __init__(self, request_factory: Callable[[], Awaitable[AsyncClient]],
                 session: ObservedHTTPSession | None = None, *,
                 generation: ObservedHTTPGeneration | None = None,
                 store: SubchatSubmissions | None = None,
                 owner: str | None = None,
                 generation_origin: str = 'https://chatgpt.com',
                 chrome_login: bool = False) -> None:
        if generation is not None and (session is None or store is None):
            raise ValueError('HTTP generation requires a session and durable store')
        if generation is not None and session is not None:
            generation_headers = generation.headers
            if (generation_headers['authorization'] != session.authorization.get_secret_value()
                    or generation_headers['chatgpt-account-id'] != session.account_id
                    or (session.cookie is not None and generation_headers['cookie']
                        != session.cookie.get_secret_value())):
                raise ValueError('HTTP generation handoff does not match login session')
        self._http_reader = ChatHTTPReader(request_factory, browser_free=True, session=session)
        self._request_factory = request_factory
        self._session = session
        self._generation = generation
        self._store = store
        self._owner = owner
        self._generation_origin = generation_origin
        self._delete_session_available = session is not None
        self._chrome_login = chrome_login

    def capabilities(self) -> dict[str, object]:
        enabled = self._generation is not None
        return {'queue_dispatch': 'recover_or_wait' if enabled else 'unavailable',
                'background_dispatcher': False,
                'native_steer': False, 'provider_stop': False,
                'cancel_scope': 'local_queued_or_prepared',
                'state': 'capabilities',
                'transport': 'http_only' if enabled else 'http_read_only',
                'browser_required': False,
                'generation_transport': 'explicit_handoff_http' if enabled else 'unavailable',
                'http_selection_send_supported': enabled, 'credential_refresh': False,
                'independent_login': False, 'persistent_credentials': False,
                'session_source': ('chrome_profile_http_get' if self._chrome_login
                                   else 'explicit_in_memory_handoff'),
                'authenticated_account_id': (self._session.account_id
                                             if self._session is not None else None),
                'authenticated_user_email': (self._session.user_email
                                              if self._chrome_login and self._session is not None
                                              else None),
                'automatic_retry': False,
                'http_delete_supported': self._delete_session_available,
                'deletion_transport': 'authenticated_http'}

    def validate_send_selection(self, selection: SubchatHTTPSelection | None) -> None:
        if self._generation is None:
            raise SubchatUnsupported('http_generation_unavailable')
        if selection is None:
            raise SubchatSelectionError('http_selection', 'required')

    async def prepare(self, submission: SubchatSubmission
                      ) -> tuple[str, ...] | SubchatPreparedSend:
        self.validate_send_selection(submission.http_selection)
        assert submission.http_selection is not None and self._session is not None
        if submission.resources is not None or (submission.requested_conversation_id is not None
                and submission.after_operation_id is None):
            raise ValueError('HTTP generation requires a queued follow-up or new text Chat')
        catalog = await self.http_catalog()
        require_http_selection(catalog, submission.http_selection)
        versions = catalog['versions']
        assert isinstance(versions, list)
        choices = [choice for version in versions if isinstance(version, dict)
                   for choice in version['choices'] if isinstance(choice, dict)
                   and choice.get('http_selection') == submission.http_selection.model_dump()]
        if len(choices) != 1:
            raise SubchatSelectionError('preset_id', 'ambiguous')
        if choices[0].get('model_title') != submission.model:
            raise SubchatSelectionError('model', 'mismatch')
        if choices[0].get('title') != submission.effort:
            raise SubchatSelectionError('effort', 'mismatch')
        baseline: tuple[str, ...] = ()
        if submission.after_operation_id is not None:
            assert self._store is not None
            target = self._store.get(submission.after_operation_id, owner=self._owner)
            if (target.state != 'completed' or target.answer_message_id is None
                    or target.user_message_id is None
                    or target.provider_account_id != self._session.account_id
                    or target.user_message_id != submission.expected_last_user_message_id):
                raise ValueError('HTTP follow-up target is not a verified final answer')
            observed = await self._http_reader.history(None, target)
            if (not isinstance(observed, SubchatAnswer)
                    or observed.answer_message_id != target.answer_message_id):
                raise ValueError('HTTP follow-up final answer changed')
            baseline = (target.user_message_id,)
        return SubchatPreparedSend(baseline, str(uuid4()), self._session.account_id)

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if self._generation is None or self._store is None:
            raise SubchatUnsupported('http_generation_unavailable')
        parent = None
        if submission.after_operation_id is not None:
            target = self._store.get(submission.after_operation_id, owner=self._owner)
            if (target.state != 'completed' or target.conversation_id is None
                    or target.user_message_id is None or target.answer_message_id is None):
                raise ValueError('HTTP follow-up target is not a verified final answer')
            parent = HTTPFollowupParent(target.conversation_id, target.user_message_id,
                                        target.answer_message_id)
        plan = HTTPGenerationPlan.from_reserved(submission, followup_parent=parent)
        await dispatch_generation(plan, submission, handoff=self._generation,
                                  client=await self._request_factory(), store=self._store,
                                  owner=self._owner, origin=self._generation_origin)
        return None  # Only history can confirm the saved input and final answer.

    async def catalog(self, model: str | None = None) -> dict[str, object]:
        # MCP's default is explicitly UI; do not silently turn that into another source.
        raise SubchatUnsupported('ui_unavailable')

    async def http_catalog(self) -> dict[str, object]:
        async with asyncio.timeout(20):
            result = await self._http_reader.catalog(None)
            result['http_selection_send_supported'] = self._generation is not None
            result['generation_transport'] = ('explicit_handoff_http' if self._generation
                                              is not None else 'unavailable')
            return result

    def _has_identity(self, submission: SubchatSubmission) -> bool:
        if submission.conversation_id is None or submission.user_message_id is None:
            return False
        if CONVERSATION_ID.fullmatch(submission.conversation_id) is None:
            raise ValueError('Invalid saved conversation identity')
        return True

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None:
        if not self._has_identity(submission):
            if self._generation is not None and self._store is not None:
                self._store.record_http_event(submission.operation_id, 'history_unknown',
                                              owner=self._owner)
            return None  # No history scan, fabricated identity, bootstrap tab or resend.
        try:
            async with asyncio.timeout(20):
                receipt = await self._http_reader.receipt(None, submission)
        except Exception:
            if self._generation is not None and self._store is not None:
                self._store.record_http_event(submission.operation_id, 'history_failed',
                                              owner=self._owner)
                raise ConnectionError('HTTP history observation failed') from None
            raise
        if self._generation is not None and self._store is not None:
            self._store.record_http_event(submission.operation_id,
                'history_receipt' if receipt is not None else 'history_unknown', owner=self._owner)
        return receipt

    async def read_answer(self, submission: SubchatSubmission
                          ) -> SubchatAnswer | SubchatPendingObservation | None:
        if not self._has_identity(submission):
            return None
        try:
            async with asyncio.timeout(20):
                answer = await self._http_reader.history(None, submission)
        except Exception:
            if self._generation is not None and self._store is not None:
                self._store.record_http_event(submission.operation_id, 'history_failed',
                                              owner=self._owner)
                raise ConnectionError('HTTP history observation failed') from None
            raise
        if answer is None and self._generation is not None and self._store is not None:
            self._store.record_http_event(submission.operation_id, 'history_unknown',
                                          owner=self._owner)
        return answer

    async def verify_delete_target(self, submission: SubchatSubmission) -> None:
        if not self._has_identity(submission):
            raise ValueError('Deletion needs a saved conversation and input')
        async with asyncio.timeout(20):
            await self._http_reader.verify_delete_target(None, submission)

    async def patch_delete(self, submission: SubchatSubmission) -> bool:
        async with asyncio.timeout(20):
            return await self._http_reader.patch_delete(None, submission)

    async def download_sandbox_file(self, operation_id: str,
                                    sandbox_link: str, *, max_bytes: int = MAX_FILE_BYTES
                                    ) -> SandboxDownload:
        """Download one file from a saved, history-verified final answer."""
        from .subchat_http_download import download_verified_sandbox_file

        if self._store is None or self._session is None:
            raise SubchatUnsupported('http_session_required')
        saved = self._store.get(operation_id, owner=self._owner)
        if saved.state != 'completed':
            raise ValueError('File download requires a completed operation')
        async with asyncio.timeout(20):
            answer = await self._http_reader.history(None, saved)
        if not isinstance(answer, SubchatAnswer):
            raise ValueError('A verified final answer is required for file download')
        return await download_verified_sandbox_file(
            saved, answer, sandbox_link, session=self._session,
            client=await self._request_factory(), max_bytes=max_bytes)
