"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
import base64
import hashlib
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from contextvars import ContextVar
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
    SubchatOutputLimit,
    SubchatPreflightFailed,
    SubchatPreparationFailed,
    Subchats,
    SubchatStaleTarget,
    SubchatUnsupported,
)
from .subchat_browser.catalog import (
    compare_http_and_ui_catalog,
    decode_choice_id,
    require_http_selection,
)
from .subchat_content import SubchatResources
from .subchat_delete import DeleteRequest, SubchatDeletionUnknown, delete_saved
from .subchat_http_download import SandboxFileTooLarge
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatAutoQueueDisarmed,
    SubchatCommittedMutationConflict,
    SubchatConcurrentSend,
    SubchatHTTPSelection,
    SubchatList,
    SubchatOperationNotFound,
    SubchatQueueRevisionConflict,
    SubchatRequestConflict,
    SubchatSelectionError,
    SubchatSubmission,
    SubchatWorkContext,
    confirmed_conversation_url,
    provider_receipt_state,
)

_QUEUE_AUTHORIZATION_GRANT: ContextVar[str | None] = ContextVar(
    'subchat_queue_authorization_grant', default=None)
logger = logging.getLogger(__name__)


def _mutation_digest(request: Request, grant_id: str | None = None) -> str:
    """Bind one mutation ID to its exact tool, arguments and trusted grant."""
    payload = json.dumps((request.tool, request.arguments, grant_id),
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class Send(Contract):
    intent_key: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=256, description=(
        'For source=http, copy the exact model_title from the chosen available '
        'subchat_catalog choice, or pass choice_id alone. A version label is not the model.'))
    effort: str | None = Field(default=None, min_length=1, max_length=256, description=(
        'For source=http, copy the exact title from the same available '
        'subchat_catalog choice, or pass choice_id alone.'))
    choice_id: str | None = Field(default=None, min_length=1, max_length=8192,
                                  description='Exact choice_id from source=http catalog. '
                                  'Binds model, effort and HTTP selection; not authorization.')
    conversation_id: str | None = None
    work_context: SubchatWorkContext | None = None
    resources: SubchatResources | None = None
    http_selection: SubchatHTTPSelection | None = None


class Message(Contract):
    mode: Literal['queue', 'steer']
    target_operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)
    resources: SubchatResources | None = None


class QueueModelChange(OperationId):
    expected_revision: int = Field(ge=0)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=256)
    choice_id: str | None = Field(default=None, min_length=1, max_length=8192)


class HTTPQueueModelChange(OperationId):
    expected_revision: int = Field(ge=0)
    choice_id: str = Field(min_length=1, max_length=8192)


class QueueResourcesChange(OperationId):
    expected_revision: int = Field(ge=0)
    resources: SubchatResources


def public_submission_data(submission: SubchatSubmission) -> dict[str, JsonValue]:
    """Expose a submission receipt without repeating the caller's full prompt."""
    return {
        **cast(dict[str, JsonValue], submission.model_dump(mode='json', exclude={'prompt'})),
        'submission_operation_id': submission.operation_id,
        'provider_receipt': provider_receipt_state(submission),
        'conversation_url': confirmed_conversation_url(submission),
    }


class Catalog(Contract):
    model: str | None = Field(default=None, min_length=1, max_length=256)
    source: Literal['ui', 'http', 'compare'] = 'ui'


class ReadOnlyHTTPCatalog(Contract):
    source: Literal['http'] = 'http'


class Wait(OperationId):
    # Leave ample transport/cleanup headroom under direct MCP's 30-second deadline.
    wait_ms: int = Field(default=1000, ge=0, le=10_000)


class QueueWatch(OperationId):
    enabled: bool = True
    lease_seconds: int = Field(default=900, ge=30, le=1800)


class QueueAuto(OperationId):
    enabled: bool = True
    lease_seconds: int = Field(default=86_400, ge=30, le=86_400)
    notify_desktop: bool = False


class QueueEvents(Contract):
    limit: int = Field(default=50, ge=1, le=100)
    after_id: int | None = Field(default=None, ge=0)
    operation_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class SandboxFile(OperationId):
    sandbox_link: str = Field(min_length=1, max_length=1024)
    max_bytes: int = Field(default=512 * 1024, ge=1, le=512 * 1024)
    offset: int = Field(default=0, ge=0, lt=16 * 1024 * 1024)


class ChatImage(OperationId):
    image_index: int | None = Field(default=None, ge=0)
    max_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    offset: int = Field(default=0, ge=0)
    chunk_bytes: int | None = Field(default=None, ge=1, le=24 * 1024)


QUEUE_WATCH_INTERVAL = 5.0
SEND_ACK_TIMEOUT = 2.0
WAIT_POLL_INTERVAL_MS = 10_000
READ_ONLY_TOOLS = frozenset({
    'subchat_capabilities', 'subchat_activity', 'subchat_catalog', 'subchat_list',
    'subchat_observe', 'subchat_recover', 'subchat_status', 'subchat_wait',
    'subchat_download_file', 'subchat_download_image', 'subchat_refresh_auth',
    'subchat_preview',
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
    'subchat_queue_auto': (QueueAuto, 'Opt in one saved queued follow-up for durable '
        'background delivery. The controller resumes armed work after restart until the '
        'lease expires. It may open its configured browser and never repeats a send once '
        'the durable sending checkpoint exists. Disabling stops future observations but '
        'cannot undo a send already started. Completion or failure is saved as an event. '
        'On macOS, notify_desktop=true also shows a local desktop notification; '
        'the durable event remains available if the alert is missed.'),
    'subchat_queue_events': (QueueEvents, 'Read owner-scoped durable queue completion and '
        'failure events. The controller also sends MCP logging notifications when connected; '
        'a host may not surface them to the model turn. Pass after_id=0, then use '
        'next_cursor to recover all later events in order after a missed notification.'),
    'subchat_preview': (OperationId, 'Opt in to one bounded, provisional preview of '
        'uniquely correlated in-progress assistant text from authenticated history. '
        'Returns no preview if the provider has not exposed partial text. '
        'Never saves partial text, marks a submission complete, or sends a message.'),
    'subchat_list': (SubchatList, 'List owner-scoped saved submission summaries without '
                     'opening Chrome. Set include_prompt_preview=true to explicitly '
                     'include at most 160 characters of each saved prompt.'),
    'subchat_cancel': (OperationId, 'Cancel an unsent queued/prepared input; never stop Chat.'),
    'subchat_queue_model_change': (QueueModelChange, 'Change only an unsent queued input model. '
        'Pass expected_revision from subchat_status; use choice_id for HTTP selections, or '
        'model and effort for UI selections. A stale revision or sending input is rejected. '
        'The selected choice is checked again at dispatch; this never resends.'),
    'subchat_queue_resources_change': (QueueResourcesChange,
        'Replace resource references on one unsent queued follow-up. Pass the current '
        'queue_revision and explicit already-uploaded Chat file descriptors or observed '
        'plugin references. No parent resources are inherited. Empty resources clears '
        'the references. Local paths are never accepted; this does not send.'),
    'subchat_delete': (DeleteRequest, 'Hide one exact saved ordinary Chat conversation. '
        'Requires matching saved operation and conversation ID and checks the bound account. '
        'Makes one authenticated HTTP PATCH; unknown outcomes are never replayed.'),
    'subchat_message': (Message, 'Queue an exact follow-up to a confirmed submission. '
                        'Resources explicitly attach already-uploaded files or observed '
                        'plugin references to this child; parent resources are not inherited. '
                        'A completed tool call confirms local queue registration, not '
                        'delivery to Chat. Steer returns unsupported without sending.'),
    'subchat_send': (Send, 'Send one ordinary Chat message with exact model/effort labels. '
                     'For source=http, choice_id can supply model, effort and http_selection '
                     'together; the current catalog is checked again before dispatch. '
                     'For an HTTP catalog choice, model is its model_title (not the '
                     'version label), and effort is its title. '
                     'Set one stable intent_key per intended child Chat. A completed tool '
                     'call is not proof of provider acceptance: inspect provider_receipt '
                     'and the saved submission_operation_id. After a missing reply or '
                     'host safety block, inspect subchat_list and subchat_status before '
                     'considering another send. Never create a new key for the same child; '
                     'reuse its key only after reconciliation.'),
    'subchat_recover': (OperationId, 'Recover receipt/answer; in a send-capable session, '
                        'a queued follow-up may be sent once when its parent is complete. '
                        'Never replay an uncertain send.'),
    'subchat_observe': (OperationId, 'Reconcile provider receipt and answer for one saved '
                        'submission without dispatching a queued follow-up. May update '
                        'the local saved receipt; never sends a Chat message.'),
    'subchat_status': (OperationId, 'Read the saved submission and latest HTTP transport '
                       'checkpoint without browser interaction.'),
    'subchat_wait': (Wait, 'Wait for an answer without stopping generation or resending. '
                     'In a send-capable session, this may send a queued follow-up once '
                     'when its parent is complete. '
                     'Other subchats can progress between observations. Timeout returns '
                     'the current saved state, elapsed_ms and a suggested next poll interval; '
                     'it is not a failed generation or a provider ETA.'),
}
_CAPABILITIES_DEFINITION = (
    Contract, 'Read configured transport capabilities without network or browser work.')
_GATEWAY_CATALOG_DEFINITION = (
    ReadOnlyHTTPCatalog, "Read the selected account's authenticated HTTP model "
    'catalog without changing the browser model picker.')


def _tool_catalog(definitions: dict[str, tuple[type[Contract], str]], *,
                  read_only_mode: bool = False) -> list[JsonValue]:
    # MCP annotations describe the effect a tool can have, independently of
    # whether it is admitted by the current account, grant or host policy.
    local_reads = {
        'subchat_activity', 'subchat_capabilities', 'subchat_list', 'subchat_status',
        'subchat_queue_events',
    }
    account_reads = {
        'subchat_preview', 'subchat_download_file', 'subchat_download_image',
    }
    # Recovery and waiting may dispatch a queued follow-up in send-capable
    # sessions. Observation-only sessions explicitly leave queues untouched.
    dispatch_capable = {'subchat_recover', 'subchat_wait'} if not read_only_mode else set()
    irreversible = {
        'subchat_send', 'subchat_delete', 'subchat_queue_watch',
        'subchat_queue_auto', *dispatch_capable,
    }
    open_world = {
        'subchat_send', 'subchat_queue_watch', 'subchat_queue_auto',
        *dispatch_capable,
    }
    catalog_definition = definitions.get('subchat_catalog')
    catalog_is_read_only = read_only_mode or (
        catalog_definition is not None and catalog_definition[0] is ReadOnlyHTTPCatalog
    )
    return [cast(JsonValue, {
        'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
        'annotations': {
            'readOnlyHint': (name in local_reads | account_reads
                             or (name == 'subchat_catalog' and catalog_is_read_only)),
            'destructiveHint': name in irreversible,
            'openWorldHint': name in open_world,
        },
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
                               'subchat_observe',
                               'subchat_status', 'subchat_list', 'subchat_wait',
                               'subchat_cancel', 'subchat_queue_events',
                               'subchat_queue_auto', 'subchat_queue_model_change',
                               'subchat_queue_resources_change'}}
    definitions['subchat_queue_model_change'] = (
        HTTPQueueModelChange, 'Change an unsent queued HTTP follow-up to one exact '
        'available choice_id from subchat_catalog. Requires its own OAuth scope and '
        'the queue_revision from subchat_status; the queued row is updated atomically '
        'only if both its revision and account binding still match. Does not send.')
    definitions['subchat_queue_auto'] = (
        QueueAuto, 'Opt one saved queued follow-up into bounded automatic delivery '
        'through the selected account. This has its own OAuth scope. A service-owned '
        'worker can finish after the HTTP connection closes; recover terminal events '
        'with subchat_queue_events. A stopped service resumes only when a send-capable '
        'gateway session opens again. No parent model wakeup is implied.')
    definitions['subchat_capabilities'] = _CAPABILITIES_DEFINITION
    definitions['subchat_activity'] = _BASE_TOOL_DEFINITIONS['subchat_activity']
    definitions['subchat_catalog'] = _GATEWAY_CATALOG_DEFINITION
    definitions['subchat_download_file'] = (
        SandboxFile, 'Download one exact saved final-answer sandbox link from the selected '
        'account. Reads at offset and returns at most 512 KiB as base64 per call; '
        'total file limit is 16 MiB. Does not upload or send a message.')
    definitions['subchat_download_image'] = (
        ChatImage, 'Download a verified image bound to one owned saved submission. '
        'Returns at most 2 MiB as base64, with optional 24 KiB chunks; does not send.')
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
    'thinking_effort (including null); set model to that choice\'s model_title '
    'and effort to its title. The version label is not the model field. '
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
    'Use subchat_observe to reconcile a saved send without dispatching a queued follow-up; '
    'recover and wait may dispatch a ready queued follow-up in send-capable sessions. '
    'subchat_message mode=queue persists a follow-up bound to the target operation; '
    'pass resources explicitly to attach already-uploaded files or observed plugin '
    'references to that child. Parent resources are not inherited. '
    'recover/wait on its message operation dispatches only after that target completes. '
    'subchat_cancel cancels only a local queued/prepared input, never generation. '
    'subchat_queue_model_change uses the queue_revision from subchat_status to change only '
    'an unsent queued input. HTTP choices require a current choice_id; the choice is checked '
    'again at dispatch. A stale revision or sending checkpoint rejects the change. '
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
    'reply_output_limit means the provider ended at its output limit; '
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
    'Submitted/completed/interrupted confirm a matching message receipt; '
    'completed includes the answer. '
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
    'remains Chat-driven. subchat_download_image reads a selected verified image tool result '
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
        self.auto_queue_tasks: dict[str, asyncio.Task[None]] = {}
        self.auto_queue_notifications: dict[str, asyncio.Task[None]] = {}
        self.notifications: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue(maxsize=32)
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

    async def execute_with_queue_grant(self, request: Request, grant_id: str) -> Reply:
        """Bind a trusted HTTP grant to this one arm request, never to tool input."""
        context = _QUEUE_AUTHORIZATION_GRANT.set(grant_id)
        try:
            return await self.execute(request)
        finally:
            _QUEUE_AUTHORIZATION_GRANT.reset(context)

    async def close(self) -> None:
        self.closed = True
        tasks = [*self.queue_watches.values(), *self.auto_queue_tasks.values(),
                 *self.auto_queue_notifications.values(),
                 *self.recoveries.values(),
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
        self.auto_queue_tasks.clear()
        self.auto_queue_notifications.clear()
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
            auto_queue_grant_active: Callable[[str], bool] | None = None,
            ) -> SubchatSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    recoveries: dict[str, asyncio.Task[SubchatSubmission]] = {}
    sends: dict[str, asyncio.Task[SubchatSubmission]] = {}

    def send_worker(operation_id: str) -> dict[str, JsonValue]:
        """Session-local diagnostics, independent of provider receipt state."""
        task = sends.get(operation_id)
        if task is None:
            return {'state': 'not_owned'}
        if not task.done():
            return {'state': 'running'}
        if task.cancelled():
            return {'state': 'cancelled'}
        error = task.exception()
        if error is None:
            return {'state': 'finished'}
        cause = error.__cause__ or error
        reason = ('timeout' if isinstance(cause, TimeoutError) else
                  'connection_failed' if isinstance(cause, ConnectionError) else
                  'preflight_failed' if isinstance(error, SubchatPreflightFailed) else
                  'dispatch_outcome_unknown' if isinstance(error, SubchatOutcomeUnknown) else
                  'worker_failed')
        return {'state': 'failed', 'reason': reason}

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
            for field in ('choice_id', 'http_selection', 'version_id', 'preset_id', 'model_slug',
                          'thinking_effort', 'model', 'effort'):
                for reason in ('required', 'invalid', 'not_found', 'unavailable', 'mismatch',
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

    async def emit_auto_event(operation_id: str, event: str, epoch: int) -> None:
        status = service.store.auto_queue_status(operation_id, owner=owner)
        event_id = service.store.finish_auto_queue(operation_id, owner=owner, event=event,
                                                   expected_epoch=epoch)
        if event_id is None:
            return
        if status is not None and status['epoch'] == epoch and status['notify_desktop']:
            try:
                message = ('Subchat が完了しました' if event == 'completed'
                           else 'Subchat の処理を確認してください')
                process = await asyncio.create_subprocess_exec(
                    'osascript', '-e',
                    f'display notification "{message}" with title "Anywhere Computer"',
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            except (OSError, TimeoutError):
                pass  # The durable event is still available.

    def auto_queue_authorized(operation_id: str) -> bool:
        if auto_queue_grant_active is None:
            return True
        bound_grant = service.store.auto_queue_grant_id(operation_id, owner=owner)
        return bound_grant is not None and auto_queue_grant_active(bound_grant)

    async def notify_armed_session(operation_id: str, after_id: int,
                                  expected_epoch: int) -> None:
        """Notify only the live controller that explicitly armed this queue."""
        try:
            while not server.closed:
                events, more = service.store.auto_queue_events_page(
                    owner=owner, after_id=after_id, limit=100)
                for event in events:
                    event_id = event['id']
                    assert type(event_id) is int
                    after_id = event_id
                    if event['operation_id'] != operation_id:
                        continue
                    status = service.store.auto_queue_status(operation_id, owner=owner)
                    if status is None or status['epoch'] != expected_epoch:
                        return
                    name = event['event']
                    assert isinstance(name, str)
                    notice: dict[str, JsonValue] = {
                        'jsonrpc': '2.0', 'method': 'notifications/message',
                        'params': {'level': 'info' if name == 'completed' else 'warning',
                                   'logger': 'anywhere-computer.subchat',
                                   'data': {'operation_id': operation_id, 'event': name,
                                            'event_id': event_id,
                                            'recover_tool': 'subchat_status'}},
                    }
                    try:
                        server.notifications.put_nowait(notice)
                    except asyncio.QueueFull:
                        pass  # The durable event remains queryable.
                    return
                if not more:
                    status = service.store.auto_queue_status(operation_id, owner=owner)
                    if (status is None or status['state'] != 'armed'
                            or status['epoch'] != expected_epoch):
                        return
                    await asyncio.sleep(QUEUE_WATCH_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed notification never changes the saved send or event.
            return

    def start_auto_notification(operation_id: str, after_id: int,
                                expected_epoch: int) -> None:
        previous = server.auto_queue_notifications.get(operation_id)
        if previous is not None:
            previous.cancel()
        server.auto_queue_notifications[operation_id] = asyncio.create_task(
            notify_armed_session(operation_id, after_id, expected_epoch))

    async def run_auto_queue(operation_id: str) -> None:
        observed_epoch: int | None = None
        try:
            while not server.closed:
                status = service.store.auto_queue_status(operation_id, owner=owner)
                if status is None or status['state'] != 'armed':
                    return
                epoch = status['epoch']
                if type(epoch) is not int:
                    raise ValueError('Invalid automatic queue epoch')
                observed_epoch = epoch
                if not auto_queue_authorized(operation_id):
                    await emit_auto_event(operation_id, 'authorization_lost', epoch)
                    return
                expires_at = status['expires_at']
                if not isinstance(expires_at, (int, float)):
                    await emit_auto_event(operation_id, 'observation_failed', epoch)
                    return
                if time.time() >= expires_at:
                    await emit_auto_event(operation_id, 'lease_expired', epoch)
                    return
                saved = service.store.get(operation_id, owner=owner)
                if saved.state in {'completed', 'cancelled', 'interrupted',
                                   'preflight_failed'}:
                    await emit_auto_event(operation_id, saved.state, epoch)
                    return
                if saved.state not in {'queued', 'sending', 'submitted'}:
                    await emit_auto_event(operation_id, 'observation_failed', epoch)
                    return
                try:
                    await observe(operation_id, auto_queue=True)
                except SubchatQueueRevisionConflict:
                    # A model change won before the send reservation. Re-read
                    # the queued row and prepare its new selection on next pass.
                    continue
                except ValueError:
                    # Another controller can win the queued -> sending CAS or
                    # commit the final answer while this one observes. Follow
                    # the new durable checkpoint; never prepare/send again.
                    changed = service.store.get(operation_id, owner=owner)
                    if (changed.state == saved.state
                            or changed.state not in {'sending', 'submitted', 'completed',
                                                     'cancelled', 'interrupted',
                                                     'preflight_failed'}):
                        raise
                    continue
                await asyncio.sleep(min(QUEUE_WATCH_INTERVAL,
                                        max(0.0, expires_at - time.time())))
        except asyncio.CancelledError:
            # Armed rows survive controller shutdown and resume from durable state.
            raise
        except (SubchatAccessError, SubchatAccountMismatch):
            if observed_epoch is not None:
                await emit_auto_event(operation_id, 'authorization_lost', observed_epoch)
        except SubchatPreparationFailed:
            if observed_epoch is not None:
                await emit_auto_event(operation_id, 'preparation_failed', observed_epoch)
        except SubchatAutoQueueDisarmed:
            # Explicit disable is already saved; no failure event is needed.
            return
        except SubchatInterrupted:
            if observed_epoch is not None:
                await emit_auto_event(operation_id, 'predecessor_interrupted', observed_epoch)
        except Exception:
            # An unknown transport outcome remains in the submission ledger. Never
            # retry its send; a caller can inspect the saved operation and event.
            if observed_epoch is not None:
                await emit_auto_event(operation_id, 'observation_failed', observed_epoch)

    auto_queue_restarts: set[str] = set()

    def start_auto_queue(operation_id: str) -> None:
        running = server.auto_queue_tasks.get(operation_id)
        if running is not None and not running.done():
            # A rearm can win after the old worker records its final event but
            # before that worker actually exits. Start its successor on exit.
            auto_queue_restarts.add(operation_id)
            return
        if sum(not task.done() for task in server.auto_queue_tasks.values()) >= 8:
            raise ValueError('Automatic queue limit reached')
        auto_queue_restarts.discard(operation_id)
        task = asyncio.create_task(run_auto_queue(operation_id))
        server.auto_queue_tasks[operation_id] = task

        def restart_if_rearmed(finished: asyncio.Task[None]) -> None:
            if server.closed or server.auto_queue_tasks.get(operation_id) is not finished:
                return
            if operation_id not in auto_queue_restarts:
                return
            auto_queue_restarts.discard(operation_id)
            status = service.store.auto_queue_status(operation_id, owner=owner)
            if status is not None and status['state'] == 'armed':
                start_auto_queue(operation_id)

        task.add_done_callback(restart_if_rearmed)

    async def observe(operation_id: str, *, auto_queue: bool = False,
                      allow_queue_dispatch: bool = True
                      ) -> SubchatSubmission:
        if server.closed:
            raise RuntimeError('Subchat session is closed')
        current = service.store.get(operation_id, owner=owner)
        raise_failed_preparation(operation_id, current)
        if current.state == 'interrupted':
            if current.interruption_reason == 'output_limit':
                raise SubchatOutputLimit('Provider output limit is saved; do not resend')
            raise SubchatInterrupted('Provider interruption is saved; do not resend')
        # An owned send may still be preparing or awaiting its one generation
        # response. Its ledger checkpoint is the only safe immediate observation.
        sending = sends.get(operation_id)
        if sending is not None and not sending.done():
            return current
        # Recovering a queued follow-up can dispatch it when its parent is complete.
        # An observation-only server must leave that durable queue untouched.
        if (read_only or not allow_queue_dispatch) and current.state == 'queued':
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
                        return await service.recover(
                            operation_id, owner=owner,
                            require_auto_queue_armed=auto_queue,
                            auto_queue_authorized=(
                                (lambda: auto_queue_authorized(operation_id))
                                if auto_queue else None))
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
    if (getattr(service.backend, 'preview', None) is None
            or getattr(service.backend, 'http_read', True) is False):
        definitions.pop('subchat_preview')

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
            'writing a local file. Use offset to read successive chunks of a file up to '
            '16 MiB. Each call returns at most 512 KiB. '
            'This does not upload the file to another Chat or Library.')

    download_image = getattr(service.backend, 'download_image', None)
    if download_image is not None and getattr(service.backend, 'image_download_available', True):
        definitions['subchat_download_image'] = (
            ChatImage, 'Read an image in a finished tool result bound to a saved submitted '
            'or completed Chat input. The final assistant answer may still be pending. '
            'For a turn with multiple images, provide its zero-based image_index. '
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
            'source=http observes the dedicated app catalog without picker interaction. '
            'source=compare reads both catalogs and marks whether a selectable UI row '
            'was observed for each HTTP choice, without sending; omit model. '
            'Returned transport IDs are not UI labels for subchat_send. '
            'Uses the configured transport; inspect subchat_capabilities when available. '
            'An HTTP catalog does not establish independent login or generation support.')

    if read_only:
        definitions = {name: definition for name, definition in definitions.items()
                       if name in READ_ONLY_TOOLS}

    async def catalog() -> list[JsonValue]:
        tools = _tool_catalog(definitions, read_only_mode=read_only)
        return _require_send_fields(tools) if require_send_intent else tools

    async def execute(request: Request) -> Reply:
        send_operation_id = request.operation_id

        def saved_receipt(operation_id: str) -> dict[str, JsonValue]:
            try:
                return public_submission_data(service.store.get(operation_id, owner=owner))
            except SubchatOperationNotFound:
                return {'submission_operation_id': operation_id,
                        'provider_receipt': None, 'conversation_url': None}

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
                        max_bytes=target_file.max_bytes, offset=target_file.offset)
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
                    'offset': downloaded.offset,
                    'eof': downloaded.eof,
                    'content_base64': base64.b64encode(downloaded.content).decode('ascii'),
                })
            if (request.tool == 'subchat_download_image' and download_image is not None
                    and 'subchat_download_image' in definitions):
                target_image = ChatImage.model_validate(request.arguments)
                service.store.get(target_image.operation_id, owner=owner)
                try:
                    downloaded_image = await download_image(
                        target_image.operation_id, max_bytes=target_image.max_bytes,
                        image_index=target_image.image_index)
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
                    'image_index': downloaded_image.image_index,
                    'image_count': downloaded_image.image_count,
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
            if request.tool == 'subchat_queue_auto':
                auto = QueueAuto.model_validate(request.arguments)
                trusted_grant = _QUEUE_AUTHORIZATION_GRANT.get()
                if auto_queue_grant_active is not None and (
                    trusted_grant is None or not auto_queue_grant_active(trusted_grant)
                ):
                    raise SubchatAccessError(403)
                digest = _mutation_digest(request, trusted_grant)
                receipt = service.store.mutation_receipt(
                    request.operation_id, owner=owner, tool=request.tool, digest=digest)
                if receipt is not None:
                    if auto.enabled:
                        current_status = service.store.auto_queue_status(
                            auto.operation_id, owner=owner)
                        if current_status is not None and current_status['state'] == 'armed':
                            start_auto_queue(auto.operation_id)
                        saved_status = receipt
                    else:
                        recorded_status = receipt['auto_status']
                        if not isinstance(recorded_status, dict):
                            raise ValueError('Saved automatic queue status is invalid')
                        saved_status = recorded_status
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data={'submission_operation_id': auto.operation_id,
                                       **saved_status})
                auto_status: dict[str, JsonValue] | None
                if auto.enabled:
                    if auto.notify_desktop and sys.platform != 'darwin':
                        raise ValueError('Desktop notifications require macOS')
                    armed = service.store.active_auto_queues(owner=owner)
                    if (auto.operation_id not in armed and len(armed) >= 8):
                        raise ValueError('Automatic queue limit reached')
                    running = server.auto_queue_tasks.get(auto.operation_id)
                    if ((running is None or running.done())
                            and sum(not task.done() for task in
                                    server.auto_queue_tasks.values()) >= 8):
                        raise ValueError('Automatic queue limit reached')
                    auto_status = service.store.arm_auto_queue(
                        auto.operation_id, owner=owner, lease_seconds=auto.lease_seconds,
                        authorization_grant_id=trusted_grant,
                        notify_desktop=auto.notify_desktop,
                        request_id=request.operation_id, digest=digest)
                    cursor = auto_status['event_cursor']
                    epoch = auto_status['epoch']
                    assert type(cursor) is int and type(epoch) is int
                    start_auto_notification(auto.operation_id, cursor, epoch)
                    start_auto_queue(auto.operation_id)
                else:
                    service.store.disable_auto_queue(
                        auto.operation_id, owner=owner,
                        request_id=request.operation_id, digest=digest)
                    subscriber = server.auto_queue_notifications.pop(auto.operation_id, None)
                    if subscriber is not None:
                        subscriber.cancel()
                    auto_status = service.store.auto_queue_status(
                        auto.operation_id, owner=owner)
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'submission_operation_id': auto.operation_id,
                                   **(auto_status or {'state': 'disabled'})})
            if request.tool == 'subchat_queue_events':
                events = QueueEvents.model_validate(request.arguments)
                if events.after_id is not None:
                    event_page, more = service.store.auto_queue_events_page(
                        owner=owner, after_id=events.after_id, limit=events.limit,
                        operation_id=events.operation_id)
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data={'events': cast(JsonValue, event_page),
                                       'next_cursor': (event_page[-1]['id'] if event_page
                                                       else events.after_id),
                                       'has_more': more})
                return Reply(operation_id=request.operation_id, state='completed',
                            data={'events': cast(JsonValue, service.store.auto_queue_events(
                                 owner=owner, limit=events.limit,
                                 operation_id=events.operation_id))})
            if request.tool == 'subchat_activity':
                Contract.model_validate(request.arguments)
                sends_active = sum(not task.done() for task in sends.values())
                recoveries_active = sum(not task.done()
                                        for task in recoveries.values())
                watches_active = sum(not task.done() for task in
                                     server.queue_watches.values())
                auto_active = sum(not task.done() for task in
                                  server.auto_queue_tasks.values())
                generation_active = (server.live_transport is not None
                                     and server.live_transport())
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'state': 'active' if (sends_active or recoveries_active
                                                        or watches_active or auto_active
                                                        or generation_active)
                                              else 'idle',
                                   'active_count': sends_active + recoveries_active
                                                   + watches_active + auto_active
                                                   + int(generation_active),
                                   'active_sends': sends_active,
                                   'failed_send_workers': sum(
                                       send_worker(key)['state'] == 'failed' for key in sends),
                                   'active_recoveries': recoveries_active,
                                   'active_queue_watches': watches_active,
                                   'active_auto_queues': auto_active,
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
                result = service.store.cancel(
                    target.operation_id, owner=owner, request_id=request.operation_id,
                    digest=_mutation_digest(request))
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
                                       message.prompt, owner=owner,
                                       resources=message.resources)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=public_submission_data(result))
            if request.tool == 'subchat_queue_model_change':
                change = QueueModelChange.model_validate(request.arguments)
                digest = _mutation_digest(request)
                receipt = service.store.mutation_receipt(
                    request.operation_id, owner=owner, tool=request.tool, digest=digest)
                if receipt is not None:
                    saved = SubchatSubmission.model_validate(receipt['submission'])
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data={**public_submission_data(saved),
                                       'queue_revision': receipt['queue_revision']})
                current = service.store.get(change.operation_id, owner=owner)
                if current.state != 'queued':
                    raise ValueError('Only an unsent queued input can change its model')
                if current.http_selection is None:
                    if (change.choice_id is not None or change.model is None
                            or change.effort is None):
                        raise SubchatSelectionError('model', 'required')
                    queue_model, queue_effort, queue_selection = (
                        change.model, change.effort, None)
                else:
                    if change.choice_id is None:
                        raise SubchatSelectionError('choice_id', 'required')
                    try:
                        queue_selection, queue_model, queue_effort = decode_choice_id(
                            change.choice_id)
                    except ValueError as error:
                        raise SubchatSelectionError('choice_id', 'invalid') from error
                    if ((change.model is not None and change.model != queue_model)
                            or (change.effort is not None and change.effort != queue_effort)):
                        raise SubchatSelectionError('choice_id', 'mismatch')
                validate = getattr(service.backend, 'validate_send_selection', None)
                if validate is not None:
                    validate(queue_selection)
                if queue_selection is not None:
                    catalog_reader = getattr(service.backend, 'http_catalog', None)
                    if catalog_reader is None:
                        raise SubchatSelectionError('http_selection', 'unavailable')
                    async with browser_lock:
                        require_http_selection(await catalog_reader(), queue_selection,
                                               model=queue_model, effort=queue_effort)
                result, revision = service.store.change_queued_model(
                    change.operation_id, owner=owner,
                    expected_revision=change.expected_revision, model=queue_model,
                    effort=queue_effort, http_selection=queue_selection,
                    request_id=request.operation_id, digest=digest)
                return Reply(operation_id=request.operation_id, state='completed',
                             data={**public_submission_data(result),
                                   'queue_revision': revision})
            if request.tool == 'subchat_queue_resources_change':
                change_resources = QueueResourcesChange.model_validate(request.arguments)
                digest = _mutation_digest(request)
                receipt = service.store.mutation_receipt(
                    request.operation_id, owner=owner, tool=request.tool, digest=digest)
                if receipt is not None:
                    saved = SubchatSubmission.model_validate(receipt['submission'])
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data={**public_submission_data(saved),
                                       'queue_revision': receipt['queue_revision']})
                result, revision = service.store.change_queued_resources(
                    change_resources.operation_id, owner=owner,
                    expected_revision=change_resources.expected_revision,
                    resources=change_resources.resources,
                    request_id=request.operation_id, digest=digest)
                return Reply(operation_id=request.operation_id, state='completed',
                             data={**public_submission_data(result),
                                   'queue_revision': revision})
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
                                   'send_worker': send_worker(result.operation_id),
                                   'elapsed_ms': max(0, round((time.monotonic() - started) * 1000)),
                                   'suggested_poll_interval_ms': (
                                       WAIT_POLL_INTERVAL_MS if result.state in {
                                           'queued', 'sending', 'submitted'}
                                       or live_preparation() else None),
                                   **({'http_progress': progress} if (progress :=
                                      service.store.http_progress(result.operation_id,
                                                                  owner=owner)) is not None
                                      else {})})
            if request.tool == 'subchat_preview':
                target = OperationId.model_validate(request.arguments)
                saved = service.store.get(target.operation_id, owner=owner)
                if saved.state != 'submitted':
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data={'submission_operation_id': target.operation_id,
                                       'preview': None, 'reason': 'not_in_progress'})
                reader = getattr(service.backend, 'preview', None)
                if reader is None:
                    raise SubchatUnsupported('http_history_required')
                async with browser_lock:
                    preview = await reader(saved)
                current = service.store.get(target.operation_id, owner=owner)
                if current.state != 'submitted':
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data={'submission_operation_id': target.operation_id,
                                       'preview': None, 'reason': 'not_in_progress'})
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'submission_operation_id': target.operation_id,
                                   'preview': (cast(JsonValue, preview.model_dump(mode='json'))
                                               if preview is not None else None),
                                   'reason': ('available' if preview is not None
                                              else 'partial_text_not_observed')})
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
                        elif args_catalog.source == 'compare':
                            if args_catalog.model is not None or observe_http_catalog is None:
                                raise ValueError(
                                    'Catalog comparison requires support and no model selection')
                            assert observe_catalog is not None
                            http_observed = await observe_http_catalog()
                            ui_observed = await observe_catalog(None)
                            observed = compare_http_and_ui_catalog(http_observed, ui_observed)
                        else:
                            assert observe_catalog is not None
                            observed = await observe_catalog(args_catalog.model)
                data = TypeAdapter(dict[str, JsonValue]).validate_python(observed)
                return Reply(operation_id=request.operation_id, state='completed', data=data)
            if request.tool == 'subchat_send':
                args = Send.model_validate(request.arguments)
                model, effort, selection = args.model, args.effort, args.http_selection
                if args.choice_id is not None:
                    try:
                        selected, selected_model, selected_effort = decode_choice_id(
                            args.choice_id)
                    except ValueError as error:
                        raise SubchatSelectionError('choice_id', 'invalid') from error
                    if ((model is not None and model != selected_model)
                            or (effort is not None and effort != selected_effort)
                            or (selection is not None and selection != selected)):
                        raise SubchatSelectionError('choice_id', 'mismatch')
                    model, effort, selection = selected_model, selected_effort, selected
                if model is None or effort is None:
                    raise SubchatSelectionError('choice_id' if args.choice_id is not None
                                                else 'model', 'required')
                if require_send_intent and args.intent_key is None:
                    return Reply(operation_id=request.operation_id, state='failed',
                                 error='subchat_send requires a stable intent_key before '
                                       'sending. Reuse the same key for the same child Chat.',
                                 data={'error_code': 'invalid_parameter',
                                       'dispatched': False})
                prepared = service.store.prepare(
                    request.operation_id, args.prompt, model, effort, owner=owner,
                    conversation_id=args.conversation_id, work_context=args.work_context,
                    resources=args.resources, http_selection=selection,
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
                                    submission_id, args.prompt, model, effort,
                                    owner=owner, conversation_id=args.conversation_id,
                                    work_context=args.work_context, resources=args.resources,
                                    http_selection=selection)

                        sending = asyncio.create_task(dispatch())
                        sends[submission_id] = sending

                        def completed(done: asyncio.Task[SubchatSubmission]) -> None:
                            # Save a late preparation failure before releasing
                            # this controller's task reference.
                            if sends.get(submission_id) is done:
                                error = done.exception() if not done.cancelled() else None
                                if error is not None:
                                    save_preparation_failure(submission_id, error)
                                    current = service.store.get(submission_id, owner=owner)
                                    if current.state == 'sending':
                                        # No provider text, exception message or traceback.
                                        logger.warning('Subchat send worker failed '
                                                       'operation_id=%s reason=%s',
                                                       submission_id,
                                                       send_worker(submission_id)['reason'])
                                        if current.http_selection is not None:
                                            service.store.record_http_event(
                                                submission_id, 'send_worker_failed', owner=owner)
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
                                           'send_worker': send_worker(submission_id),
                                           'submission_operation_id': submission_id,
                                           'send_in_progress': True})
            elif request.tool in {'subchat_recover', 'subchat_observe', 'subchat_status'}:
                target = OperationId.model_validate(request.arguments)
                if request.tool == 'subchat_status':
                    result = service.store.get(target.operation_id, owner=owner)
                    raise_failed_preparation(target.operation_id, result)
                elif request.tool == 'subchat_observe':
                    result = await observe(target.operation_id, allow_queue_dispatch=False)
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
                               'send_worker': send_worker(result.operation_id),
                               'queue_revision': service.store.queue_revision(
                                   result.operation_id, owner=owner),
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
        except SubchatOutputLimit:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The provider stopped this answer at its output limit. '
                               'Inspect the partial conversation; do not resend or '
                               'advance its queued follow-ups automatically.',
                         data={'error_code': 'reply_output_limit',
                               'automatic_retry': False})
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
            guidance = ('For an HTTP catalog choice, set model to the exact model_title '
                        'of the selected available choice, effort to its title, and copy '
                        'http_selection unchanged. ' if error.field == 'model' else
                        'Inspect subchat_catalog source=http and use the exact available '
                        'choice. ')
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The selected Chat model parameter is invalid or unavailable. '
                               + guidance + 'Use a new request ID '
                               'for corrected input. The saved original remains unsent.',
                         data={'error_code': error.code, 'field': error.field,
                               'reason': error.reason, 'dispatched': False,
                               'corrected_request_requires_new_operation_id': True})
        except SubchatOperationNotFound:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='No operation with this ID is visible in the selected ledger. '
                               'Check the original connection, account and ledger path, '
                               'then use subchat_list. This does not prove the send failed.',
                         data={'error_code': 'unknown_operation', 'dispatched': None,
                               'automatic_retry': False})
        except SubchatCommittedMutationConflict:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='This request ID already committed a different Subchat '
                               'mutation. Inspect subchat_status; do not retry with a new ID '
                               'until the saved outcome is understood.',
                         data={'error_code': 'request_conflict', 'dispatched': None,
                               'automatic_retry': False})
        except SubchatRequestConflict:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='This operation ID belongs to a different Subchat request. '
                               'Inspect its saved status and use a new ID for different input.',
                         data={'error_code': 'request_conflict', 'dispatched': False,
                               'automatic_retry': False})
        except SubchatQueueRevisionConflict:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Queued input changed before this request could reserve it. '
                               'Read subchat_status and use its current queue_revision.',
                         data={'error_code': 'queue_revision_conflict',
                               'dispatched': False, 'automatic_retry': False})
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
            target_id = (send_operation_id if request.tool == 'subchat_send'
                         else request.arguments.get('operation_id'))
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Adapter preparation failed before dispatch. Check for an existing '
                               'draft, active generation, missing HTTP selection or unavailable '
                               'model in the dedicated Chat before retrying the same exact '
                               'request ID; '
                               'a new queue also requires its parent selection to match this '
                               'controller. '
                               'For an existing queued message, recover its operation instead.',
                         data={**(saved_receipt(target_id) if isinstance(target_id, str)
                                  else {}),
                               'error_code': 'preparation_failed', 'dispatched': False,
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
                         error='Submission unconfirmed. Use subchat_observe with '
                               'submission_operation_id; do not send again with a new ID.',
                         data={**saved_receipt(error.operation_id),
                               'send_worker': send_worker(error.operation_id),
                               'automatic_retry': False})
        except SubchatDeletionUnknown:
            return Reply(operation_id=request.operation_id, state='unknown',
                         error='Deletion outcome is unknown. Inspect the exact conversation; '
                               'do not repeat the PATCH automatically.',
                         data={'error_code': 'delete_unknown', 'automatic_retry': False})
        except Exception as error:
            # Never expose provider error text, invalid prompt contents or account data.
            if request.tool == 'subchat_send':
                return Reply(operation_id=request.operation_id, state='unknown',
                             error='Subchat send outcome is unconfirmed. Inspect subchat_list '
                                   'and subchat_status with the saved operation ID before '
                                   'considering another send.',
                             data={**saved_receipt(send_operation_id),
                                   'error_type': type(error).__name__,
                                   'dispatched': None, 'automatic_retry': False})
            if request.tool in {'subchat_wait', 'subchat_recover'}:
                target_id = request.arguments.get('operation_id')
                return Reply(operation_id=request.operation_id, state='failed',
                             error='Subchat observation failed. The saved submission may have '
                                   'been sent; inspect subchat_status and recover the same '
                                   'operation ID. Do not call subchat_send again.',
                             data={'error_type': type(error).__name__,
                                   'submission_operation_id': target_id,
                                   'dispatched': None, 'automatic_retry': False})
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Subchat call failed; inspect its saved status before retrying.',
                         data={'error_type': type(error).__name__})

    server = SubchatSession(
        catalog, execute, recoveries, sends,
        instructions=INSTRUCTIONS if instructions is None else instructions,
        require_send_intent=require_send_intent,
        live_transport=getattr(service.backend, 'has_live_generation', None),
        close_transport=getattr(service.backend, 'close_generations', None))
    if not read_only:
        for operation_id in service.store.active_auto_queues(owner=owner)[:8]:
            start_auto_queue(operation_id)
    return server
