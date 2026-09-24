"""Browser-free ordinary-Chat adapter with optional generation and deletion."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakSet

import httpx

from .subchat import (
    SubchatAccessError,
    SubchatAnswer,
    SubchatPendingObservation,
    SubchatPreparedSend,
    SubchatReceipt,
    SubchatUnsupported,
)
from .subchat_browser.catalog import require_http_selection
from .subchat_browser.http_reader import ChatHTTPReader, bounded_httpx_body
from .subchat_http_download import MAX_FILE_BYTES
from .subchat_http_generation import ObservedHTTPGeneration, dispatch_generation
from .subchat_http_sender import HTTPFollowupParent, HTTPGenerationPlan
from .subchat_http_session import ObservedHTTPSession, seed_cookie_jar
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatHTTPSelection,
    SubchatSelectionError,
    SubchatSubmission,
    SubchatSubmissions,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

    from .subchat_http_download import SandboxDownload
    from .subchat_http_image import ImageDownload

CONVERSATION_ID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')
_ReadResult = TypeVar('_ReadResult')


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
                 chrome_login: bool = False,
                 startup_access_status: int | None = None,
                 startup_account_mismatch: bool = False,
                 refresh_session: Callable[[str | None], Awaitable[tuple[
                     ObservedHTTPSession, Callable[[], Awaitable[AsyncClient]]]]]
                 | None = None) -> None:
        if startup_access_status is not None and (startup_access_status not in (401, 403)
                                                  or not chrome_login or session is not None):
            raise ValueError('Chrome login rejection requires an unbound HTTP session')
        if startup_account_mismatch and (not chrome_login or session is not None
                                         or startup_access_status != 401):
            raise ValueError('Account mismatch requires a rejected Chrome login')
        if generation is not None and (session is None or store is None):
            raise ValueError('HTTP generation requires a session and durable store')
        if refresh_session is not None and (not chrome_login or generation is not None):
            raise ValueError('Login refresh is limited to read-only Chrome sessions')
        if generation is not None and session is not None:
            generation_headers = generation.headers
            if (generation_headers['authorization'] != session.authorization.get_secret_value()
                    or generation.account_id != session.account_id
                    or (session.cookie is not None and generation_headers.get('cookie')
                        != session.cookie.get_secret_value())):
                raise ValueError('HTTP generation handoff does not match login session')
        explicit_cookie = session is not None and session.cookie is not None and not chrome_login
        cookie_clients: WeakSet[AsyncClient] = WeakSet()
        cookie_lock = asyncio.Lock()

        async def session_request_factory() -> AsyncClient:
            client = await request_factory()
            if explicit_cookie and client not in cookie_clients:
                async with cookie_lock:
                    if client not in cookie_clients:
                        assert session is not None and session.cookie is not None
                        origin = urlsplit(generation_origin)
                        host = origin.hostname
                        if host not in {'chatgpt.com', '127.0.0.1'}:
                            raise ValueError('Unsupported explicit Cookie origin')
                        seeded_names = seed_cookie_jar(
                            client, session.cookie.get_secret_value(), domain=host)
                        cookie_scopes = {name: (host, '/') for name in seeded_names}

                        async def exact_origin(request: httpx.Request) -> None:
                            if (request.url.scheme != origin.scheme
                                    or request.url.host != host
                                    or request.url.port != origin.port):
                                raise ValueError('Explicit session requires the exact Chat origin')

                        async def retire_seeded_cookies(response: httpx.Response) -> None:
                            # The observed Cookie header has no domain/path metadata.
                            # A provider Set-Cookie with a different scope must replace
                            # its synthetic seed, rather than create a stale duplicate.
                            for cookie in response.cookies.jar:
                                if cookie.name in cookie_scopes:
                                    previous = cookie_scopes[cookie.name]
                                    current = (cookie.domain, cookie.path)
                                    if current == previous:
                                        continue
                                    try:
                                        client.cookies.jar.clear(*previous, cookie.name)
                                    except KeyError:
                                        pass
                                    cookie_scopes[cookie.name] = current

                        client.event_hooks['request'].append(exact_origin)
                        client.event_hooks['response'].append(retire_seeded_cookies)
                        cookie_clients.add(client)
            return client

        self._http_reader = ChatHTTPReader(
            session_request_factory, browser_free=True, session=session,
            use_client_cookies=explicit_cookie,
            access_status=startup_access_status)
        self._request_factory: Callable[[], Awaitable[AsyncClient]] = session_request_factory
        self._session = session
        self._generation = generation
        self._store = store
        self._owner = owner
        self._generation_origin = generation_origin
        self._delete_session_available = session is not None
        self._chrome_login = chrome_login
        self._startup_access_status = startup_access_status
        self._startup_account_mismatch = startup_account_mismatch
        self._refresh_session = refresh_session
        self._refresh_lock = asyncio.Lock()
        self._last_auto_refresh_at = 0.0

    async def _refresh_locked(self) -> None:
        if self._refresh_session is None:
            raise ValueError('Credential refresh unavailable')
        previous_account = self._session.account_id if self._session is not None else None
        session, request_factory = await self._refresh_session(previous_account)
        if previous_account is not None and session.account_id != previous_account:
            raise SubchatAccountMismatch('Chrome login selected another Chat account')
        self._session = session
        self._request_factory = request_factory
        self._http_reader = ChatHTTPReader(request_factory, browser_free=True,
                                           session=session)
        self._startup_access_status = None
        self._startup_account_mismatch = False
        self._delete_session_available = True

    async def refresh_auth(self) -> dict[str, object]:
        """Refresh only the selected read session; never recover or dispatch work."""
        async with self._refresh_lock:
            await self._refresh_locked()
            return {'authentication_state': 'authenticated',
                    'generation_transport': 'unavailable',
                    'credential_refresh': True}

    async def _authenticated_read(self, read: Callable[[], Awaitable[_ReadResult]]
                                  ) -> _ReadResult:
        if self._startup_account_mismatch:
            raise SubchatAccountMismatch('Chrome login selected another Chat account')
        reader = self._http_reader
        try:
            return await read()
        except SubchatAccessError as error:
            if error.status == 401 and self._http_reader is reader:
                self._startup_access_status = 401
            if (error.status != 401 or self._refresh_session is None
                    or self._session is None):
                raise
            async with self._refresh_lock:
                if self._http_reader is reader:
                    now = time.monotonic()
                    if now - self._last_auto_refresh_at < 30:
                        raise
                    self._last_auto_refresh_at = now
                    await self._refresh_locked()
            # A concurrent refresh can satisfy this read too. Never retry twice.
            return await read()

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
                'http_selection_send_supported': enabled,
                'credential_refresh': self._refresh_session is not None,
                'independent_login': False, 'persistent_credentials': False,
                'session_source': ('chrome_profile_http_get' if self._chrome_login
                                   else 'explicit_in_memory_handoff'),
                'authentication_state': (
                    'account_mismatch' if self._startup_account_mismatch else
                    'authentication_required' if self._startup_access_status == 401 else
                    'access_denied' if self._startup_access_status == 403 else
                    'authenticated' if self._session is not None else
                    'http_session_required'),
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

    async def _verify_explicit_generation_account(self) -> None:
        """Bind the supplied Cookie and bearer to one account before reserving a send."""
        if self._chrome_login or self._generation_origin != 'https://chatgpt.com':
            return
        assert self._session is not None
        if self._session.cookie is None:
            raise SubchatAccountMismatch('HTTP generation Cookie is required')
        assert self._generation is not None
        headers = {'accept': 'application/json',
                   'referer': 'https://chatgpt.com/',
                   'user-agent': (self._session.user_agent
                                  or self._generation.headers['user-agent']),
                   'sec-fetch-site': 'same-origin', 'sec-fetch-mode': 'cors',
                   'sec-fetch-dest': 'empty'}
        client = await self._request_factory()
        async with client.stream('GET', 'https://chatgpt.com/api/auth/session',
                                 headers=headers, timeout=15.0,
                                 follow_redirects=False) as response:
            if response.status_code in (401, 403):
                raise SubchatAccessError(response.status_code)
            if response.status_code != 200 or response.headers.get(
                    'content-type', '').split(';', 1)[0].strip() != 'application/json':
                raise SubchatAccountMismatch('HTTP generation login could not be verified')
            body = await bounded_httpx_body(response, 131_072,
                                            'HTTP generation login response is too large')
        try:
            data = json.loads(body)
            account = data.get('account') if isinstance(data, dict) else None
            account_id = account.get('id') if isinstance(account, dict) else None
            token = data.get('accessToken') if isinstance(data, dict) else None
            matches = (isinstance(account_id, str) and isinstance(token, str)
                       and account_id == self._session.account_id
                       and hmac.compare_digest('Bearer ' + token,
                                               self._session.authorization.get_secret_value()))
        except (ValueError, TypeError):
            matches = False
        if not matches:
            raise SubchatAccountMismatch('HTTP generation login does not match selected account')

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
        await self._verify_explicit_generation_account()
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
        assert self._session is not None
        try:
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
                                      owner=self._owner, origin=self._generation_origin,
                                      use_client_cookies=(self._chrome_login
                                                          or self._session.cookie is not None))
        except BaseException:
            # The SQLite claim is the dispatch boundary. A failed or cancelled
            # preflight is terminal, never an invitation to replay the request.
            self._store.fail_http_before_dispatch(submission.operation_id, owner=self._owner)
            raise
        return None  # Only history can confirm the saved input and final answer.

    async def catalog(self, model: str | None = None) -> dict[str, object]:
        # MCP's default is explicitly UI; do not silently turn that into another source.
        raise SubchatUnsupported('ui_unavailable')

    async def http_catalog(self) -> dict[str, object]:
        async def read() -> dict[str, object]:
            reader = self._http_reader
            async with asyncio.timeout(20):
                return await reader.catalog(None)

        result = await self._authenticated_read(read)
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
            async def read() -> SubchatReceipt | None:
                reader = self._http_reader
                async with asyncio.timeout(20):
                    return await reader.receipt(None, submission)

            receipt = await self._authenticated_read(read)
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
            async def read() -> SubchatAnswer | SubchatPendingObservation | None:
                reader = self._http_reader
                async with asyncio.timeout(20):
                    return await reader.history(None, submission)

            answer = await self._authenticated_read(read)
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
            if self._startup_account_mismatch:
                raise SubchatAccountMismatch('Chrome login selected another Chat account')
            if self._startup_access_status is not None:
                raise SubchatAccessError(self._startup_access_status)
            raise SubchatUnsupported('http_session_required')
        saved = self._store.get(operation_id, owner=self._owner)
        if saved.state != 'completed':
            raise ValueError('File download requires a completed operation')
        async def read() -> SandboxDownload:
            reader = self._http_reader
            session = self._session
            request_factory = self._request_factory
            async with asyncio.timeout(20):
                answer = await reader.history(None, saved)
            if not isinstance(answer, SubchatAnswer):
                raise ValueError('A verified final answer is required for file download')
            assert session is not None
            return await download_verified_sandbox_file(
                saved, answer, sandbox_link,
                session=(session.model_copy(update={'cookie': None})
                         if session.cookie is not None and not self._chrome_login else session),
                client=await request_factory(), max_bytes=max_bytes)

        return await self._authenticated_read(read)

    async def download_image(self, operation_id: str, *, max_bytes: int
                             ) -> ImageDownload:
        """Read one history-bound image tool result, including before final text."""
        from .subchat_http_image import download_verified_image

        if self._store is None or self._session is None:
            if self._startup_account_mismatch:
                raise SubchatAccountMismatch('Chrome login selected another Chat account')
            if self._startup_access_status is not None:
                raise SubchatAccessError(self._startup_access_status)
            raise SubchatUnsupported('http_session_required')
        saved = self._store.get(operation_id, owner=self._owner)
        if saved.state not in {'submitted', 'completed'}:
            raise ValueError('Image download requires a confirmed submission')

        async def read() -> ImageDownload:
            reader = self._http_reader
            session = self._session
            request_factory = self._request_factory
            async with asyncio.timeout(20):
                history_payload = await reader._history_payload(None, saved)
            assert session is not None
            return await download_verified_image(
                saved, history_payload,
                session=(session.model_copy(update={'cookie': None})
                         if session.cookie is not None and not self._chrome_login else session),
                client=await request_factory(), max_bytes=max_bytes)

        return await self._authenticated_read(read)
