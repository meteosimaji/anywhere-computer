"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
import base64
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Literal, cast

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from . import __version__
from .mcp_server import REQUEST_ID_SCHEMA, Execute, MCPSession
from .mcp_server import Catalog as ToolCatalog
from .models import Contract, OperationId, Reply, Request
from .runtime_identity import runtime_identity
from .subchat import (
    SubchatAccessError,
    SubchatBrowserClosed,
    SubchatInterrupted,
    SubchatObservedSubmission,
    SubchatOutcomeUnknown,
    SubchatPreflightFailed,
    SubchatPreparationFailed,
    Subchats,
    SubchatStaleTarget,
    SubchatUnsupported,
)
from .subchat_content import SubchatResources
from .subchat_delete import DeleteRequest, SubchatDeletionUnknown, delete_saved
from .subchat_http_download import SandboxFileTooLarge
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatConcurrentSend,
    SubchatHTTPSelection,
    SubchatList,
    SubchatOperationNotFound,
    SubchatRequestConflict,
    SubchatSelectionError,
    SubchatSubmission,
    SubchatWorkContext,
)


class Send(Contract):
    intent_key: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    conversation_id: str | None = None
    work_context: SubchatWorkContext | None = None
    resources: SubchatResources | None = None
    http_selection: SubchatHTTPSelection | None = None


class Message(Contract):
    mode: Literal['queue', 'steer']
    target_operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)


def public_submission_data(submission: SubchatSubmission) -> dict[str, JsonValue]:
    """Expose a submission receipt without repeating the caller's full prompt."""
    return cast(dict[str, JsonValue], submission.model_dump(mode='json', exclude={'prompt'}))


class Catalog(Contract):
    model: str | None = Field(default=None, min_length=1, max_length=256)
    source: Literal['ui', 'http'] = 'ui'


class ReadOnlyHTTPCatalog(Contract):
    source: Literal['http'] = 'http'


class Wait(OperationId):
    # Leave ample transport/cleanup headroom under direct MCP's 30-second deadline.
    wait_ms: int = Field(default=1000, ge=0, le=10_000)


class QueueWatch(OperationId):
    enabled: bool = True
    lease_seconds: int = Field(default=900, ge=30, le=1800)


class SandboxFile(OperationId):
    sandbox_link: str = Field(min_length=1, max_length=1024)
    max_bytes: int = Field(default=512 * 1024, ge=1, le=512 * 1024)


class ChatImage(OperationId):
    max_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    offset: int = Field(default=0, ge=0)
    chunk_bytes: int | None = Field(default=None, ge=1, le=24 * 1024)


QUEUE_WATCH_INTERVAL = 5.0
SEND_ACK_TIMEOUT = 2.0
WAIT_POLL_INTERVAL_MS = 10_000
READ_ONLY_TOOLS = frozenset({
    'subchat_capabilities', 'subchat_activity', 'subchat_catalog', 'subchat_list',
    'subchat_recover', 'subchat_status', 'subchat_wait', 'subchat_download_file',
    'subchat_download_image', 'subchat_refresh_auth',
})

_BASE_TOOL_DEFINITIONS: dict[str, tuple[type[Contract], str]] = {
    'subchat_activity': (Contract, 'Report counts of live work owned by this MCP controller. '
        'No browser or account request is made. Use to decide whether an idle plugin '
        'session can close without interrupting a send, generation, or queue watch.'),
    'subchat_queue_watch': (QueueWatch, 'Explicitly arm or disarm automatic delivery of an '
        'existing queued input while this MCP controller remains alive. Requires an already '
        'open owned browser tab; never launches Chrome. Checks every five seconds, stops on '
        'errors or after final saved result. At most eight retained watches; disable to '
        'release a slot. '
        'Lease is 30-1800 seconds (default 900); expiry requires explicit re-arming. '
        'Restart requires explicit re-arming. Disabling observation does not cancel a queue '
        'or a preparation already in progress; use subchat_cancel for unsent cancellation. '
        'This is browser-assisted delivery, not quiet HTTP generation or immediate steer.'),
    'subchat_list': (SubchatList, 'List saved submission summaries without opening Chrome.'),
    'subchat_cancel': (OperationId, 'Cancel an unsent queued/prepared input; never stop Chat.'),
    'subchat_delete': (DeleteRequest, 'Hide one exact saved ordinary Chat conversation. '
        'Requires matching saved operation and conversation ID and checks the bound account. '
        'Makes one authenticated HTTP PATCH; unknown outcomes are never replayed.'),
    'subchat_message': (Message, 'Queue an exact follow-up to a confirmed submission. '
                        'A completed tool call confirms local queue registration, not '
                        'delivery to Chat. Steer returns unsupported without sending.'),
    'subchat_send': (Send, 'Send one ordinary Chat message with exact model/effort labels. '
                     'Set one stable intent_key per intended child Chat. After a missing '
                     'reply or host safety block, inspect subchat_list and subchat_status '
                     'before considering another send. Never create a new key for the '
                     'same child; reuse its key only after reconciliation.'),
    'subchat_recover': (OperationId, 'Recover receipt/answer or progress a queued follow-up; '
                        'never replay an uncertain send.'),
    'subchat_status': (OperationId, 'Read the saved submission and latest HTTP transport '
                       'checkpoint without browser interaction.'),
    'subchat_wait': (Wait, 'Wait for an answer without stopping generation or resending. '
                     'Other subchats can progress between observations. Timeout returns '
                     'the current saved state, elapsed_ms and a suggested next poll interval; '
                     'it is not a failed generation or a provider ETA.'),
}
_CAPABILITIES_DEFINITION = (
    Contract, 'Read configured transport capabilities without network or browser work.')
_GATEWAY_CATALOG_DEFINITION = (
    ReadOnlyHTTPCatalog, "Read the selected account's authenticated HTTP model "
    'catalog without changing the browser model picker.')


def _tool_catalog(definitions: dict[str, tuple[type[Contract], str]]) -> list[JsonValue]:
    return [cast(JsonValue, {
        'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
        'annotations': {'readOnlyHint': name in {
            'subchat_capabilities', 'subchat_catalog', 'subchat_status', 'subchat_list',
            'subchat_download_file', 'subchat_download_image', 'subchat_refresh_auth'},
                        'destructiveHint': name == 'subchat_delete', 'openWorldHint': True},
    }) for name, (schema, description) in definitions.items()]


def _require_send_fields(tools: list[JsonValue]) -> list[JsonValue]:
    for tool in tools:
        if isinstance(tool, dict) and tool.get('name') == 'subchat_send':
            schema = tool['inputSchema']
            if isinstance(schema, dict):
                properties = schema.setdefault('properties', {})
                if isinstance(properties, dict):
                    properties['request_id'] = dict(REQUEST_ID_SCHEMA)
                required = schema.setdefault('required', [])
                if isinstance(required, list):
                    required.extend(('intent_key', 'request_id'))
    return tools


def direct_gateway_catalog() -> list[JsonValue]:
    """Advertise selected direct tools without opening or authenticating Chrome."""
    definitions = {name: definition for name, definition in _BASE_TOOL_DEFINITIONS.items()
                   if name in {'subchat_message', 'subchat_send', 'subchat_recover',
                               'subchat_status', 'subchat_list', 'subchat_wait'}}
    definitions['subchat_capabilities'] = _CAPABILITIES_DEFINITION
    definitions['subchat_catalog'] = _GATEWAY_CATALOG_DEFINITION
    return _require_send_fields(_tool_catalog(definitions))


def capability_report(reported: dict[str, object], *,
                      queue_watch_supported: bool) -> dict[str, JsonValue]:
    """Add the implementation identity to configured transport capabilities."""
    return TypeAdapter(dict[str, JsonValue]).validate_python({
        **reported,
        'implementation_version': __version__,
        'implementation_runtime_id': runtime_identity(),
        'queue_watch_supported': queue_watch_supported,
    })

_PREPARATION_REASONS = {
    'Ordinary Chat composer contains a draft': 'composer_has_draft',
    'Ordinary Chat is generating': 'generation_active',
    'Ordinary Chat with an idle empty composer was not confirmed': 'composer_not_ready',
    'Model list did not become visible': 'model_list_unavailable',
    'Model picker view did not become ready': 'model_picker_unavailable',
    'Model menu structure is unsupported or ambiguous': 'model_menu_unsupported',
    'Requested model is not available in the observed menu': 'model_unavailable',
    'Requested effort is not available in the observed menu': 'effort_unavailable',
    'Ordinary Chat conversation changed': 'conversation_changed',
    'Existing conversation history is unavailable': 'existing_history_unavailable',
    'Conversation history unavailable': 'history_unavailable',
    'Conversation history is ambiguous': 'history_ambiguous',
}


INSTRUCTIONS = (
    'Ordinary Chat subchats. Use an observed model and effort; never silently substitute. '
    'Recovery may include a call-scoped observation with its operation_id, source, reason '
    'and timestamp; a queued child may report its predecessor observation. It is not saved '
    'generation state. Missing final output does not prove Thinking, failure or permission '
    'to resend. Status reads only the durable record. '
    'HTTP-read sends require http_selection copied exactly from an available choice in '
    'subchat_catalog source=http with http_selection_send_supported=true. A false flag '
    'allows catalog inspection only; restart with --http-read before constrained sends. '
    'The flag validates HTTP model selection, not standalone HTTP generation. '
    'generation_transport=browser_prepared sends through the dedicated browser. '
    'generation_transport=browser_prepared_httpx requires browser turn preparation '
    'but sends the generation POST once through HTTPX. It is not browser-free. '
    'generation_transport=explicit_handoff_http uses HTTPX after an explicitly '
    'supplied, account-matched generation handoff. The Plugin HTTP observation '
    'mode does not acquire that handoff and exposes read-only tools; browser-send '
    'uses browser_prepared generation. '
    'Keep its version_id, preset_id, model_slug and explicit '
    'thinking_effort (including null); still provide observed UI model/effort labels. '
    'The adapter rechecks availability and rejects a different wire model or effort before '
    'forwarding. Queue follow-ups inherit the selection; never guess IDs from labels. '
    'Choose request_id before subchat_send. Its response may be a pending local checkpoint '
    'while the owned send continues; it is not a finished answer or permission to resend. '
    'For multiple distinct child Chats, choose one stable intent_key per logical slot before '
    'the first send. Reuse the same intent_key if the transport request_id changes. After '
    'an ambiguous host safety block, inspect subchat_list and subchat_status before any '
    'new send; never invent a fresh key for the same slot. '
    'Poll subchat_recover with the returned submission_operation_id; with a new transport '
    'request_id for an existing intent, the original submission operation ID is returned. '
    'subchat_message mode=queue persists a follow-up bound to the target operation; '
    'recover/wait on its message operation dispatches only after that target completes. '
    'subchat_cancel cancels only a local queued/prepared input, never generation. '
    'subchat_delete requires a saved operation plus its exact conversation ID. '
    'It checks the saved input against server history, then makes one authenticated HTTP '
    'visibility change. A deleted result requires provider success and a later history 404. '
    'Unknown deletion outcomes must not be retried automatically. '
    'No background dispatcher is implied. queued is local acceptance, not delivery. '
    'Explicit subchat_queue_watch can observe and dispatch an existing queue in this '
    'controller using an already open owned browser tab. It stops on error, final saved '
    'result or lease expiry; '
    'status exposes queue_watch evidence. It is not saved across controller restart. '
    'mode=steer is currently unsupported by this ordinary Chat adapter; it never falls '
    'back to queue or Stop. Submitted is a receipt, not proof of consumption. '
    'Thinking is pending, not failure. Never repeat an uncertain send with a new ID. '
    'reply_interrupted means saved partial output was not accepted as a final answer; '
    'inspect the conversation instead of automatically resending or releasing its queue. '
    'subchat_wait defaults to one second, allows at most ten seconds, and returns the '
    'current saved state, elapsed_ms and a suggested_poll_interval_ms while pending; '
    'the interval is a client polling hint, not a provider ETA. '
    'a pending result can be waited on again without stopping generation. '
    'subchat_status reads the saved record without browser interaction. '
    'When present, http_progress is the latest saved transport checkpoint, not proof of a '
    'final answer; only completed with a saved answer is final. '
    'prepared means this adapter has not dispatched; external/manual sends are not tracked. '
    'After correcting preparation, reconcile the visible Chat before retrying the same ID. '
    'sending means receipt unconfirmed: recover it, never click Send again. '
    'Only submitted/completed confirm a matching message receipt; completed includes the answer. '
    'If a new Chat remains sending without a conversation_id after process loss, '
    'automatic recovery may be impossible: preserve unknown and reconcile manually; '
    'never scan unrelated history or resend to manufacture a receipt. '
    'This local stdio process uses the dedicated profile chosen by its operator; '
    'it does not establish shared workspace access or grant tools to the Chat.'
    ' Optional work_context is caller-supplied provenance saved with the receipt, '
    'not a grant or verified file snapshot. It is not automatically inserted into '
    'the prompt: explicitly describe relevant work in the exact prompt you send. '
    'resources accepts already-uploaded Chat file references and @ plugin URI/hint '
    'pairs observed in this account; it does not upload local paths or grant access. '
    'Resource sends require the HTTP-read browser adapter. For a file created in '
    'another Chat sandbox, provide its source conversation URL and exact file name '
    'in the target prompt. The target Chat can be asked to find it in Library and '
    'materialize it into its own sandbox; verify that it actually read the expected bytes. '
    'A sandbox path alone does not grant cross-Chat access. HTTP-only sessions expose '
    'subchat_download_file for one exact saved final-answer link, returning at most 512 KiB '
    'of base64 bytes without local storage or a Library upload. Library materialization '
    'remains Chat-driven. subchat_download_image reads the sole verified image tool result '
    'from a saved submitted or completed input, even while final assistant text is pending. '
    'It returns bounded image bytes without accepting an asset ID or URL. '
    'A queue follow-up does '
    'not implicitly reattach resources. Reference local files with device ID and '
    'absolute path in the prompt and use the selected computer plugin to read them.'
)


class SubchatSession(MCPSession):
    def __init__(self, catalog: ToolCatalog, execute: Execute,
                 tasks: dict[str, asyncio.Task[SubchatSubmission]],
                 sends: dict[str, asyncio.Task[SubchatSubmission]],
                 *, instructions: str = INSTRUCTIONS,
                 require_send_intent: bool = False,
                 live_transport: Callable[[], bool] | None = None,
                 close_transport: Callable[[], Awaitable[None]] | None = None) -> None:
        self.recoveries = tasks
        self.sends = sends
        self.closed = False
        self.calls: dict[asyncio.Task[Reply], Request] = {}
        self.queue_watches: dict[str, asyncio.Task[None]] = {}
        self.queue_watch_states: dict[str, dict[str, JsonValue]] = {}
        self.queue_watch_deadlines: dict[str, float] = {}
        self.live_transport = live_transport
        self.close_transport = close_transport
        self.require_send_intent = require_send_intent

        async def managed(request: Request) -> Reply:
            if self.closed:
                return Reply(operation_id=request.operation_id, state='failed',
                             error='Subchat session is closed.')
            async def call() -> Reply:
                return await execute(request)

            task = asyncio.create_task(call())
            self.calls[task] = request
            try:
                return await task
            finally:
                self.calls.pop(task, None)

        super().__init__(catalog, managed, instructions=instructions)

    async def close(self) -> None:
        self.closed = True
        tasks = [*self.queue_watches.values(), *self.recoveries.values(),
                 *self.sends.values(), *self.calls]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.close_transport is not None:
            await self.close_transport()
        self.recoveries.clear()
        self.sends.clear()
        self.calls.clear()
        self.queue_watches.clear()
        self.queue_watch_states.clear()
        self.queue_watch_deadlines.clear()


def session(service: Subchats, *,
            observe_catalog: Callable[[str | None], Awaitable[dict[str, object]]] | None = None,
            observe_http_catalog: Callable[[], Awaitable[dict[str, object]]] | None = None,
            instructions: str | None = None,
            serialize_recovery: bool = False,
            read_only: bool = False,
            require_send_intent: bool = False,
            owner: str | None = None,
            ) -> SubchatSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    recoveries: dict[str, asyncio.Task[SubchatSubmission]] = {}
    sends: dict[str, asyncio.Task[SubchatSubmission]] = {}

    def save_preparation_failure(operation_id: str, error: BaseException) -> None:
        if isinstance(error, SubchatSelectionError):
            service.store.record_preparation_failure(
                operation_id, owner=owner,
                reason=f'selection_{error.field}_{error.reason}')
            return
        if not isinstance(error, SubchatPreparationFailed):
            return
        cause = error.__cause__
        reason = error.reason or (_PREPARATION_REASONS.get(str(cause))
                  if isinstance(cause, ValueError) else None)
        if reason is None or re.fullmatch(r'[a-z_]{1,64}', reason) is None:
            reason = 'unknown'
        service.store.record_preparation_failure(
            operation_id, owner=owner, reason=reason)

    def raise_failed_preparation(operation_id: str, current: SubchatSubmission) -> None:
        """Expose a late send failure while its unsent ledger row is still prepared."""
        if current.state not in {'prepared', 'queued'}:
            return
        persisted = service.store.preparation_failure(operation_id, owner=owner)
        if persisted is not None:
            for field in ('http_selection', 'version_id', 'preset_id', 'model_slug',
                          'thinking_effort', 'model', 'effort'):
                for reason in ('required', 'not_found', 'unavailable', 'mismatch',
                               'ambiguous'):
                    if persisted == f'selection_{field}_{reason}':
                        raise SubchatSelectionError(field, reason)
            raise SubchatPreparationFailed(
                'Saved preparation failure', reason=persisted)
        sending = sends.get(operation_id)
        if sending is not None and sending.done() and not sending.cancelled():
            error = sending.exception()
            if error is not None:
                raise error

    async def watch_queue(operation_id: str) -> None:
        try:
            while not server.closed:
                if time.monotonic() >= server.queue_watch_deadlines[operation_id]:
                    server.queue_watch_states[operation_id] = {
                        'state': 'stopped', 'reason': 'lease_expired'}
                    return
                current = service.store.get(operation_id, owner=owner)
                if current.state not in {'queued', 'sending', 'submitted'}:
                    server.queue_watch_states[operation_id] = {
                        'state': 'stopped', 'reason': 'submission_finished'
                        if current.state == 'completed' else 'queue_left',
                        'submission_state': current.state}
                    return
                ready = getattr(service.backend, 'queue_watch_ready', None)
                if current.state == 'queued' and (ready is None or not ready(current)):
                    server.queue_watch_states[operation_id] = {
                        'state': 'stopped', 'reason': 'owned_browser_unavailable'}
                    return
                await observe(operation_id)
                if service.store.get(operation_id, owner=owner).state in {
                        'queued', 'sending', 'submitted'}:
                    await asyncio.sleep(min(QUEUE_WATCH_INTERVAL,
                        max(0, server.queue_watch_deadlines[operation_id] - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except (SubchatAccessError, SubchatAccountMismatch):
            server.queue_watch_states[operation_id] = {
                'state': 'stopped', 'reason': 'authorization_lost'}
        except SubchatBrowserClosed:
            server.queue_watch_states[operation_id] = {
                'state': 'stopped', 'reason': 'owned_browser_unavailable'}
        except Exception as error:
            # Stop on errors; a later explicit re-arm is required. Never expose
            # provider text or repeatedly reload/retry while the user is absent.
            server.queue_watch_states[operation_id] = {
                'state': 'stopped', 'reason': 'observation_failed',
                'error_type': type(error).__name__}

    async def observe(operation_id: str) -> SubchatSubmission:
        if server.closed:
            raise RuntimeError('Subchat session is closed')
        current = service.store.get(operation_id, owner=owner)
        raise_failed_preparation(operation_id, current)
        if current.state == 'interrupted':
            raise SubchatInterrupted('Provider interruption is saved; do not resend')
        # An owned send may still be preparing or awaiting its one generation
        # response. Its ledger checkpoint is the only safe immediate observation.
        sending = sends.get(operation_id)
        if sending is not None and not sending.done():
            return current
        # Recovering a queued follow-up can dispatch it when its parent is complete.
        # An observation-only server must leave that durable queue untouched.
        if read_only and current.state == 'queued':
            return current
        task = recoveries.get(operation_id)
        if task is None and current.state in {'queued', 'sending', 'submitted'}:
            if len(recoveries) >= 8:
                raise RuntimeError('Recovery limit reached; collect existing observations first')

            async def run() -> SubchatSubmission:
                lock = browser_lock if serialize_recovery or current.state == 'queued' else (
                    nullcontext())
                async with lock:
                    if current.state == 'queued':
                        return await service.recover(operation_id, owner=owner)
                    async with asyncio.timeout(25):
                        return await service.recover(operation_id, owner=owner)

            task = asyncio.create_task(run())
            recoveries[operation_id] = task

            def completed(done: asyncio.Task[SubchatSubmission]) -> None:
                if not done.cancelled():
                    # Retain late failures for the next observer, not just logs.
                    error = done.exception()
                    if error is not None:
                        save_preparation_failure(operation_id, error)
                    if ((error is None or isinstance(error, SubchatPreparationFailed))
                            and recoveries.get(operation_id) is done):
                        # A successful observation is durable even when still pending.
                        # Preparation failures are durable; retain other late
                        # errors for the next explicit observer.
                        recoveries.pop(operation_id)

            task.add_done_callback(completed)
        if task is not None:
            # An observation timeout must not cancel preparation or release its input lock.
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                current = service.store.get(operation_id, owner=owner)
                if not server.closed and task.cancelled() and current.state == 'cancelled':
                    return current
                raise
            except Exception:
                error = task.exception() if task.done() and not task.cancelled() else None
                if error is not None:
                    save_preparation_failure(operation_id, error)
                if recoveries.get(operation_id) is task:
                    recoveries.pop(operation_id)
                raise
            else:
                if recoveries.get(operation_id) is task:
                    recoveries.pop(operation_id)
                return result
        return current

    definitions = dict(_BASE_TOOL_DEFINITIONS)

    capabilities = getattr(service.backend, 'capabilities', None)
    if capabilities is not None:
        definitions['subchat_capabilities'] = _CAPABILITIES_DEFINITION

    refresh_auth = getattr(service.backend, 'refresh_auth', None)
    if (read_only and refresh_auth is not None and capabilities is not None
            and capabilities().get('credential_refresh') is True):
        definitions['subchat_refresh_auth'] = (
            Contract, 'Refresh the selected Chrome account through a headless temporary '
            'profile snapshot and authenticated HTTP GETs. Keeps the same account; does not '
            'send, recover or delete a Chat. No credentials are accepted as tool arguments.')

    download_sandbox_file = getattr(service.backend, 'download_sandbox_file', None)
    if download_sandbox_file is not None:
        definitions['subchat_download_file'] = (
            SandboxFile, 'Retrieve one exact sandbox link from a saved, completed ordinary Chat '
            'answer through the authenticated HTTP session. The server rechecks the account, '
            'conversation and final answer, then returns base64 bytes and metadata without '
            'writing a local file. Each call is limited to 512 KiB. '
            'This does not upload the file to another Chat or Library.')

    download_image = getattr(service.backend, 'download_image', None)
    if download_image is not None and getattr(service.backend, 'image_download_available', True):
        definitions['subchat_download_image'] = (
            ChatImage, 'Read the sole image in a finished tool result bound to a saved submitted '
            'or completed Chat input. The final assistant answer may still be pending. '
            'Returns at most 2 MiB of base64 image bytes without writing a local file; '
            'use offset and chunk_bytes up to 24 KiB through bounded plugin bridges. '
            'Accepts no asset ID or URL and never submits another message.')

    if read_only and observe_http_catalog is not None:
        definitions['subchat_catalog'] = (
            ReadOnlyHTTPCatalog,
            'Read the authenticated HTTP model catalog without sending or changing a model. '
            'This Plugin supports only source=http.')
    elif observe_catalog is not None and not read_only:
        definitions['subchat_catalog'] = (
            Catalog, 'Observe model labels and effort without sending. Optionally select an '
            'exact observed model in the dedicated empty tab to discover its effort choices; '
            'this can change the dedicated profile default. '
            'A partial catalog preserves known models; never infer missing effort choices. '
            'source=http observes the dedicated browser app catalog without picker interaction; '
            'model must be omitted. Returned transport IDs are not UI labels for subchat_send. '
            'Uses the configured transport; inspect subchat_capabilities when available. '
            'An HTTP catalog does not establish independent login or generation support.')

    if read_only:
        definitions = {name: definition for name, definition in definitions.items()
                       if name in READ_ONLY_TOOLS}

    async def catalog() -> list[JsonValue]:
        tools = _tool_catalog(definitions)
        return _require_send_fields(tools) if require_send_intent else tools

    async def execute(request: Request) -> Reply:
        send_operation_id = request.operation_id
        if read_only and request.tool not in READ_ONLY_TOOLS:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='This Subchat session permits observation only.',
                         data={'error_code': 'read_only', 'dispatched': False})
        try:
            if request.tool == 'subchat_refresh_auth' and 'subchat_refresh_auth' in definitions:
                assert refresh_auth is not None
                Contract.model_validate(request.arguments)
                refreshed = await refresh_auth()
                return Reply(operation_id=request.operation_id, state='completed',
                             data=TypeAdapter(dict[str, JsonValue]).validate_python(refreshed))
            if request.tool == 'subchat_download_file' and download_sandbox_file is not None:
                target_file = SandboxFile.model_validate(request.arguments)
                # Backend download implementations may use their own ledger owner.
                # Never pass another owner's operation to one of them.
                service.store.get(target_file.operation_id, owner=owner)
                try:
                    downloaded = await download_sandbox_file(
                        target_file.operation_id, target_file.sandbox_link,
                        max_bytes=target_file.max_bytes)
                except SandboxFileTooLarge:
                    return Reply(operation_id=request.operation_id, state='failed',
                                 error='The Chat file exceeds the requested byte limit.',
                                 data={'error_code': 'file_too_large',
                                       'max_bytes': target_file.max_bytes,
                                       'automatic_retry': False})
                return Reply(operation_id=request.operation_id, state='completed', data={
                    'submission_operation_id': target_file.operation_id,
                    'sandbox_link': target_file.sandbox_link,
                    'file_name': downloaded.file_name,
                    'mime_type': downloaded.mime_type,
                    'file_size_bytes': downloaded.file_size_bytes,
                    'content_base64': base64.b64encode(downloaded.content).decode('ascii'),
                })
            if (request.tool == 'subchat_download_image' and download_image is not None
                    and 'subchat_download_image' in definitions):
                target_image = ChatImage.model_validate(request.arguments)
                service.store.get(target_image.operation_id, owner=owner)
                try:
                    downloaded_image = await download_image(
                        target_image.operation_id, max_bytes=target_image.max_bytes)
                except SandboxFileTooLarge:
                    return Reply(operation_id=request.operation_id, state='failed',
                                 error='The Chat image exceeds the requested byte limit.',
                                 data={'error_code': 'file_too_large',
                                       'max_bytes': target_image.max_bytes,
                                       'automatic_retry': False})
                if target_image.offset > downloaded_image.file_size_bytes:
                    raise ValueError('Image offset exceeds file size')
                end = (downloaded_image.file_size_bytes if target_image.chunk_bytes is None
                       else min(downloaded_image.file_size_bytes,
                                target_image.offset + target_image.chunk_bytes))
                return Reply(operation_id=request.operation_id, state='completed', data={
                    'submission_operation_id': target_image.operation_id,
                    'submission_state': downloaded_image.submission_state,
                    'final_answer_verified': downloaded_image.submission_state == 'completed',
                    'mime_type': downloaded_image.mime_type,
                    'file_size_bytes': downloaded_image.file_size_bytes,
                    'width': downloaded_image.width,
                    'height': downloaded_image.height,
                    'offset': target_image.offset,
                    'next_offset': end if end < downloaded_image.file_size_bytes else None,
                    'content_base64': base64.b64encode(
                        downloaded_image.content[target_image.offset:end]).decode('ascii'),
                })
            if request.tool == 'subchat_queue_watch':
                watch = QueueWatch.model_validate(request.arguments)
                current = service.store.get(watch.operation_id, owner=owner)
                if not watch.enabled:
                    watch_task = server.queue_watches.get(watch.operation_id)
                    if watch_task is not None:
                        server.queue_watch_states[watch.operation_id] = {'state': 'disabling'}
                        watch_task.cancel()
                        await asyncio.gather(watch_task, return_exceptions=True)
                        if server.queue_watches.get(watch.operation_id) is watch_task:
                            server.queue_watches.pop(watch.operation_id)
                            server.queue_watch_deadlines.pop(watch.operation_id, None)
                        server.queue_watch_states[watch.operation_id] = {
                            'state': 'stopped', 'reason': 'explicit_cancel'}
                    data: dict[str, JsonValue] = {'state': 'disabled'}
                elif (watch.operation_id in server.queue_watches
                      and server.queue_watch_states[watch.operation_id].get('reason')
                      == 'lease_expired'):
                    server.queue_watches.pop(watch.operation_id)
                    server.queue_watch_deadlines.pop(watch.operation_id, None)
                    ready = getattr(service.backend, 'queue_watch_ready', None)
                    if current.state != 'queued' or ready is None or not ready(current):
                        raise ValueError('Automatic delivery requires a queued input and live tab')
                    data = {'state': 'watching'}
                    server.queue_watch_states[watch.operation_id] = data
                    server.queue_watch_deadlines[watch.operation_id] = (
                        time.monotonic() + watch.lease_seconds)
                    server.queue_watches[watch.operation_id] = asyncio.create_task(
                        watch_queue(watch.operation_id))
                elif watch.operation_id in server.queue_watches:
                    data = server.queue_watch_states[watch.operation_id]
                else:
                    ready = getattr(service.backend, 'queue_watch_ready', None)
                    if (current.state != 'queued' or ready is None or not ready(current)):
                        raise ValueError('Automatic delivery requires a queued input and live tab')
                    if len(server.queue_watches) >= 8:
                        raise ValueError('Queue watch limit reached; disable an existing watch')
                    data = {'state': 'watching'}
                    server.queue_watch_states[watch.operation_id] = data
                    server.queue_watch_deadlines[watch.operation_id] = (
                        time.monotonic() + watch.lease_seconds)
                    server.queue_watches[watch.operation_id] = asyncio.create_task(
                        watch_queue(watch.operation_id))
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'submission_operation_id': watch.operation_id, **data})
            if request.tool == 'subchat_activity':
                Contract.model_validate(request.arguments)
                sends_active = sum(not task.done() for task in sends.values())
                recoveries_active = sum(not task.done()
                                        for task in recoveries.values())
                watches_active = sum(not task.done() for task in
                                     server.queue_watches.values())
                generation_active = (server.live_transport is not None
                                     and server.live_transport())
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'state': 'active' if (sends_active or recoveries_active
                                                        or watches_active or generation_active)
                                              else 'idle',
                                   'active_count': sends_active + recoveries_active
                                                   + watches_active + int(generation_active),
                                   'active_sends': sends_active,
                                   'active_recoveries': recoveries_active,
                                   'active_queue_watches': watches_active,
                                   'live_generation': generation_active})
            if request.tool == 'subchat_capabilities' and capabilities is not None:
                Contract.model_validate(request.arguments)
                reported = capabilities()
                if read_only:
                    # The backend can delete with its authenticated HTTP session,
                    # but this Plugin deliberately does not expose that tool.
                    reported = {**reported, 'http_delete_supported': False,
                                'deletion_transport': 'unavailable'}
                data = capability_report(reported,
                    queue_watch_supported=(not read_only
                        and hasattr(service.backend, 'queue_watch_ready')))
                return Reply(operation_id=request.operation_id, state='completed', data=data)
            if request.tool == 'subchat_list':
                page = service.store.list(SubchatList.model_validate(request.arguments),
                                          owner=owner)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=page.model_dump(mode='json'))
            if request.tool == 'subchat_cancel':
                target = OperationId.model_validate(request.arguments)
                result = service.store.cancel(target.operation_id, owner=owner)
                pending: list[asyncio.Task[Reply] | asyncio.Task[SubchatSubmission]] = [
                    task for task, call in server.calls.items()
                           if call.tool == 'subchat_send'
                           and call.operation_id == target.operation_id]
                recovery = recoveries.get(target.operation_id)
                if recovery is not None:
                    pending.append(recovery)
                sending = sends.get(target.operation_id)
                if sending is not None:
                    pending.append(sending)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                recoveries.pop(target.operation_id, None)
                sends.pop(target.operation_id, None)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=public_submission_data(result))
            if request.tool == 'subchat_delete':
                target_delete = DeleteRequest.model_validate(request.arguments)
                async with browser_lock:
                    result_delete = await delete_saved(service, target_delete, owner=owner)
                return Reply(operation_id=request.operation_id,
                             state='completed' if result_delete.state == 'deleted' else 'unknown',
                             data=result_delete.model_dump(mode='json'))
            if request.tool == 'subchat_message':
                message = Message.model_validate(request.arguments)
                service.store.get(message.target_operation_id, owner=owner)
                if message.mode == 'steer':
                    return Reply(operation_id=request.operation_id, state='failed',
                                 error='Immediate steer is not supported by this adapter.',
                                 data={'error_code': 'unsupported', 'mode': 'steer',
                                       'dispatched': False, 'queued': False})
                result = service.queue(request.operation_id, message.target_operation_id,
                                       message.prompt, owner=owner)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=public_submission_data(result))
            if request.tool == 'subchat_wait':
                wait = Wait.model_validate(request.arguments)
                started = time.monotonic()
                result = service.store.get(wait.operation_id, owner=owner)
                raise_failed_preparation(wait.operation_id, result)

                def live_preparation() -> bool:
                    sending = sends.get(wait.operation_id)
                    return (result.state == 'prepared' and sending is not None
                            and not sending.done())

                deadline = asyncio.timeout(wait.wait_ms / 1000)
                try:
                    async with deadline:
                        preparing = live_preparation()
                        while result.state not in {
                                'prepared', 'completed', 'cancelled', 'interrupted',
                                'preflight_failed'} or preparing:
                            if preparing:
                                # Re-read the durable row after the send task may
                                # have left preparation; do not return a stale row.
                                await asyncio.sleep(.5)
                            result = await observe(wait.operation_id)
                            preparing = live_preparation()
                            if not preparing and result.state not in {
                                    'prepared', 'completed', 'cancelled', 'interrupted',
                                    'preflight_failed'}:
                                # Never hold the browser input lock while the model thinks.
                                await asyncio.sleep(.5)
                except TimeoutError:
                    if not deadline.expired():
                        raise
                    current = service.store.get(wait.operation_id, owner=owner)
                    if (current.state != result.state
                            or isinstance(result, SubchatObservedSubmission)
                            and service.store.get(result.observation.operation_id,
                                owner=owner).state != 'submitted'):
                        result = current
                raise_failed_preparation(wait.operation_id, result)
                return Reply(operation_id=request.operation_id, state='completed',
                             data={**public_submission_data(result),
                                   'elapsed_ms': max(0, round((time.monotonic() - started) * 1000)),
                                   'suggested_poll_interval_ms': (
                                       WAIT_POLL_INTERVAL_MS if result.state in {
                                           'queued', 'sending', 'submitted'}
                                       or live_preparation() else None),
                                   **({'http_progress': progress} if (progress :=
                                      service.store.http_progress(result.operation_id,
                                                                  owner=owner)) is not None
                                      else {})})
            if request.tool == 'subchat_catalog' and (
                (read_only and observe_http_catalog is not None)
                or (not read_only and observe_catalog is not None)
            ):
                async with browser_lock:
                    if read_only:
                        ReadOnlyHTTPCatalog.model_validate(request.arguments)
                        assert observe_http_catalog is not None
                        observed = await observe_http_catalog()
                    else:
                        args_catalog = Catalog.model_validate(request.arguments)
                        if args_catalog.source == 'http':
                            if args_catalog.model is not None or observe_http_catalog is None:
                                raise ValueError(
                                    'HTTP catalog requires support and no model selection')
                            observed = await observe_http_catalog()
                        else:
                            assert observe_catalog is not None
                            observed = await observe_catalog(args_catalog.model)
                data = TypeAdapter(dict[str, JsonValue]).validate_python(observed)
                return Reply(operation_id=request.operation_id, state='completed', data=data)
            if request.tool == 'subchat_send':
                args = Send.model_validate(request.arguments)
                if require_send_intent and args.intent_key is None:
                    return Reply(operation_id=request.operation_id, state='failed',
                                 error='subchat_send requires a stable intent_key before '
                                       'sending. Reuse the same key for the same child Chat.',
                                 data={'error_code': 'invalid_parameter',
                                       'dispatched': False})
                prepared = service.store.prepare(
                    request.operation_id, args.prompt, args.model, args.effort, owner=owner,
                    conversation_id=args.conversation_id, work_context=args.work_context,
                    resources=args.resources, http_selection=args.http_selection,
                    intent_key=args.intent_key)
                submission_id = prepared.operation_id
                send_operation_id = submission_id
                sending = sends.get(submission_id)
                if (sending is not None and sending.done() and not sending.cancelled()
                        and sending.exception() is not None and prepared.state == 'prepared'):
                    # An exact explicit retry may prepare again. Observation
                    # never retries, and the ledger still guards dispatch.
                    sends.pop(submission_id)
                    sending = None
                if prepared.state != 'prepared':
                    result = prepared
                else:
                    if sending is None:
                        service.store.clear_preparation_failure(
                            submission_id, owner=owner)
                        async def dispatch() -> SubchatSubmission:
                            async with browser_lock:
                                return await service.send(
                                    submission_id, args.prompt, args.model, args.effort,
                                    owner=owner, conversation_id=args.conversation_id,
                                    work_context=args.work_context, resources=args.resources,
                                    http_selection=args.http_selection)

                        sending = asyncio.create_task(dispatch())
                        sends[submission_id] = sending

                        def completed(done: asyncio.Task[SubchatSubmission]) -> None:
                            # Save a late preparation failure before releasing
                            # this controller's task reference.
                            if sends.get(submission_id) is done:
                                error = done.exception() if not done.cancelled() else None
                                if error is not None:
                                    save_preparation_failure(submission_id, error)
                                if error is None or isinstance(error, SubchatPreparationFailed):
                                    sends.pop(submission_id)

                        sending.add_done_callback(completed)
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(sending), SEND_ACK_TIMEOUT)
                    except TimeoutError:
                        current = service.store.get(submission_id, owner=owner)
                        return Reply(operation_id=request.operation_id, state='running',
                                     data={**public_submission_data(current),
                                           'submission_operation_id': submission_id,
                                           'send_in_progress': True})
            elif request.tool in {'subchat_recover', 'subchat_status'}:
                target = OperationId.model_validate(request.arguments)
                if request.tool == 'subchat_status':
                    result = service.store.get(target.operation_id, owner=owner)
                    raise_failed_preparation(target.operation_id, result)
                else:
                    current = service.store.get(target.operation_id, owner=owner)
                    if current.state == 'queued':
                        # Recover is the explicit retry for a queued child
                        # whose previous preparation failed before dispatch.
                        service.store.clear_preparation_failure(
                            target.operation_id, owner=owner)
                    result = await observe(target.operation_id)
            else:
                raise ValueError('Unknown subchat tool')
            return Reply(operation_id=request.operation_id, state='completed',
                         data={**public_submission_data(result),
                               **({'http_progress': progress} if (progress :=
                                  service.store.http_progress(result.operation_id,
                                                              owner=owner)) is not None else {}),
                               **({'queue_watch': server.queue_watch_states[result.operation_id]}
                                  if result.operation_id in server.queue_watch_states else {})})
        except asyncio.CancelledError:
            if request.tool == 'subchat_send' and not server.closed:
                current = service.store.get(send_operation_id, owner=owner)
                if current.state == 'cancelled':
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data=public_submission_data(current))
            raise
        except SubchatBrowserClosed:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The dedicated browser closed. Restart the subchat controller '
                               'with the same saved state and profile, then recover existing '
                               'operation IDs. Do not resend unconfirmed submissions.',
                         data={'error_code': 'browser_closed', 'automatic_retry': False})
        except SubchatAccountMismatch:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The current Chat account does not match the saved operation. '
                               'Use a controller authenticated to the original account and '
                               'recover the same operation ID. Do not resend or reassign it.',
                         data={'error_code': 'account_mismatch', 'automatic_retry': False})
        except SubchatAccessError as error:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Check the selected Chat login and account access. Saved '
                               'submissions are preserved; after restoring access, '
                               + ('call subchat_refresh_auth or restart the controller. '
                                  if 'subchat_refresh_auth' in definitions else
                                  'restart the controller with the same profile and state. ')
                               + 'Recover existing IDs without sending them again.',
                         data={'error_code': error.code, 'automatic_retry': False})
        except SubchatInterrupted:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The provider recorded an interrupted answer. Inspect the '
                               'conversation; do not automatically resend or advance its queue.',
                         data={'error_code': 'reply_interrupted', 'automatic_retry': False})
        except SubchatStaleTarget:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Queue target is stale; inspect the conversation before continuing.',
                         data={'error_code': 'stale_target', 'dispatched': False})
        except SubchatUnsupported as error:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The configured transport cannot perform this operation. '
                               'No browser, alternative backend or automatic retry is used.',
                         data={'error_code': error.code, 'automatic_retry': False,
                               **({'dispatched': False}
                                  if error.code == 'http_generation_unavailable' else {})})
        except SubchatSelectionError as error:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The selected Chat model parameter is invalid or unavailable. '
                               'Inspect subchat_catalog source=http, then use a new request ID '
                               'for corrected input. The saved original remains unsent.',
                         data={'error_code': error.code, 'field': error.field,
                               'reason': error.reason, 'dispatched': False,
                               'corrected_request_requires_new_operation_id': True})
        except SubchatOperationNotFound:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='No operation with this ID is visible in the selected ledger. '
                               'Check the ID and ledger path, then use subchat_list.',
                         data={'error_code': 'unknown_operation', 'dispatched': False,
                               'automatic_retry': False})
        except SubchatRequestConflict:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='This operation ID belongs to a different Subchat request. '
                               'Inspect its saved status and use a new ID for different input.',
                         data={'error_code': 'request_conflict', 'dispatched': False,
                               'automatic_retry': False})
        except SubchatConcurrentSend as error:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Another Subchat send is active in this conversation. '
                               'Recover it from the owning account before preparing this one. '
                               'If its outcome remains unknown, start a new Chat '
                               'without resending the uncertain request.',
                         data={'error_code': 'concurrent_send', 'dispatched': False,
                               **({'blocking_operation_id': error.blocking_operation_id}
                                  if error.blocking_operation_id is not None else {}),
                               'automatic_retry': False})
        except ValidationError as error:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Subchat input has invalid fields. Correct them before sending.',
                         data={'error_code': 'invalid_parameter',
                               'invalid_params': [
                                   {'path': [str(part) for part in item['loc']],
                                    'code': item['type']}
                                   for item in error.errors(include_input=False,
                                                            include_context=False)],
                               'dispatched': False})
        except SubchatPreparationFailed as error:
            cause = error.__cause__
            reason = error.reason or (_PREPARATION_REASONS.get(str(cause))
                      if isinstance(cause, ValueError) else None)
            if request.tool == 'subchat_send':
                save_preparation_failure(send_operation_id, error)
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Adapter preparation failed before dispatch. Check for an existing '
                               'draft, active generation, missing HTTP selection or unavailable '
                               'model in the dedicated Chat before retrying the same exact '
                               'request ID; '
                               'a new queue also requires its parent selection to match this '
                               'controller. '
                               'For an existing queued message, recover its operation instead.',
                         data={'error_code': 'preparation_failed', 'dispatched': False,
                               **({'reason': reason} if reason is not None else {})})
        except SubchatPreflightFailed:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='HTTP generation stopped before the generation POST. '
                               'This operation is final and will not be resent. '
                               'Inspect subchat_status for the last preparation stage; '
                               'use a new operation ID only after resolving the cause.',
                         data={'error_code': 'http_preflight_failed', 'dispatched': False,
                               'automatic_retry': False})
        except SubchatOutcomeUnknown as error:
            return Reply(operation_id=request.operation_id, state='unknown',
                         error='Submission unconfirmed. Use subchat_recover with '
                               'submission_operation_id; do not send again with a new ID.',
                         data={'submission_operation_id': error.operation_id})
        except SubchatDeletionUnknown:
            return Reply(operation_id=request.operation_id, state='unknown',
                         error='Deletion outcome is unknown. Inspect the exact conversation; '
                               'do not repeat the PATCH automatically.',
                         data={'error_code': 'delete_unknown', 'automatic_retry': False})
        except Exception as error:
            # Never expose provider error text, invalid prompt contents or account data.
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Subchat call failed; inspect its saved status before retrying.',
                         data={'error_type': type(error).__name__})

    server = SubchatSession(
        catalog, execute, recoveries, sends,
        instructions=INSTRUCTIONS if instructions is None else instructions,
        require_send_intent=require_send_intent,
        live_transport=getattr(service.backend, 'has_live_generation', None),
        close_transport=getattr(service.backend, 'close_generations', None))
    return server
