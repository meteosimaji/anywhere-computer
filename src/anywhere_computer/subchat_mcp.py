"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Literal, cast

from pydantic import Field, JsonValue, TypeAdapter

from .mcp_server import Catalog as ToolCatalog
from .mcp_server import Execute, MCPSession
from .models import Contract, OperationId, Reply, Request
from .subchat import (
    SubchatAccessError,
    SubchatBrowserClosed,
    SubchatInterrupted,
    SubchatObservedSubmission,
    SubchatOutcomeUnknown,
    SubchatPreparationFailed,
    Subchats,
    SubchatStaleTarget,
    SubchatUnsupported,
)
from .subchat_content import SubchatResources
from .subchat_delete import DeleteRequest, SubchatDeletionUnknown, delete_saved
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatHTTPSelection,
    SubchatList,
    SubchatSubmission,
    SubchatWorkContext,
)


class Send(Contract):
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


class Catalog(Contract):
    model: str | None = Field(default=None, min_length=1, max_length=256)
    source: Literal['ui', 'http'] = 'ui'


class Wait(OperationId):
    # Leave ample transport/cleanup headroom under direct MCP's 30-second deadline.
    wait_ms: int = Field(default=1000, ge=0, le=10_000)


class QueueWatch(OperationId):
    enabled: bool = True
    lease_seconds: int = Field(default=900, ge=30, le=1800)


QUEUE_WATCH_INTERVAL = 5.0


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
    'generation_transport=browser_prepared still requires the dedicated browser for sends, '
    'including with --http-read. Independent HTTP generation is not supported. '
    'Keep its version_id, preset_id, model_slug and explicit '
    'thinking_effort (including null); still provide observed UI model/effort labels. '
    'The adapter rechecks availability and rejects a different wire model or effort before '
    'forwarding. Queue follow-ups inherit the selection; never guess IDs from labels. '
    'Choose request_id before subchat_send. Its response is a submission receipt, not a '
    'finished answer. Poll subchat_recover with operation_id equal to that send request_id. '
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
    'current saved state; '
    'a pending result can be waited on again without stopping generation. '
    'subchat_status reads the saved record without browser interaction. '
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
    'Resource sends require the HTTP-read browser adapter. A queue follow-up does '
    'not implicitly reattach resources. Reference local files with device ID and '
    'absolute path in the prompt and use the selected computer plugin to read them.'
)


class SubchatSession(MCPSession):
    def __init__(self, catalog: ToolCatalog, execute: Execute,
                 tasks: dict[str, asyncio.Task[SubchatSubmission]],
                 *, instructions: str = INSTRUCTIONS) -> None:
        self.recoveries = tasks
        self.closed = False
        self.calls: dict[asyncio.Task[Reply], Request] = {}
        self.queue_watches: dict[str, asyncio.Task[None]] = {}
        self.queue_watch_states: dict[str, dict[str, JsonValue]] = {}
        self.queue_watch_deadlines: dict[str, float] = {}

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
        tasks = [*self.queue_watches.values(), *self.recoveries.values(), *self.calls]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.recoveries.clear()
        self.calls.clear()
        self.queue_watches.clear()
        self.queue_watch_states.clear()
        self.queue_watch_deadlines.clear()


def session(service: Subchats, *,
            observe_catalog: Callable[[str | None], Awaitable[dict[str, object]]] | None = None,
            observe_http_catalog: Callable[[], Awaitable[dict[str, object]]] | None = None,
            instructions: str | None = None,
            serialize_recovery: bool = False,
            ) -> SubchatSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    recoveries: dict[str, asyncio.Task[SubchatSubmission]] = {}

    async def watch_queue(operation_id: str) -> None:
        try:
            while not server.closed:
                if time.monotonic() >= server.queue_watch_deadlines[operation_id]:
                    server.queue_watch_states[operation_id] = {
                        'state': 'stopped', 'reason': 'lease_expired'}
                    return
                current = service.store.get(operation_id, owner=None)
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
                if service.store.get(operation_id, owner=None).state in {
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
        current = service.store.get(operation_id, owner=None)
        if current.state == 'interrupted':
            raise SubchatInterrupted('Provider interruption is saved; do not resend')
        task = recoveries.get(operation_id)
        if task is None and current.state in {'queued', 'sending', 'submitted'}:
            if len(recoveries) >= 8:
                raise RuntimeError('Recovery limit reached; collect existing observations first')

            async def run() -> SubchatSubmission:
                lock = browser_lock if serialize_recovery or current.state == 'queued' else (
                    nullcontext())
                async with lock:
                    if current.state == 'queued':
                        return await service.recover(operation_id, owner=None)
                    async with asyncio.timeout(25):
                        return await service.recover(operation_id, owner=None)

            task = asyncio.create_task(run())
            recoveries[operation_id] = task

            def completed(done: asyncio.Task[SubchatSubmission]) -> None:
                if not done.cancelled():
                    # Retain late failures for the next observer, not just logs.
                    error = done.exception()
                    if error is None and recoveries.get(operation_id) is done:
                        # A successful observation is durable even when still pending.
                        # Retain only late failures for the next explicit observer.
                        recoveries.pop(operation_id)

            task.add_done_callback(completed)
        if task is not None:
            # An observation timeout must not cancel preparation or release its input lock.
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                current = service.store.get(operation_id, owner=None)
                if not server.closed and task.cancelled() and current.state == 'cancelled':
                    return current
                raise
            except Exception:
                if recoveries.get(operation_id) is task:
                    recoveries.pop(operation_id)
                raise
            else:
                if recoveries.get(operation_id) is task:
                    recoveries.pop(operation_id)
                return result
        return current

    definitions: dict[str, tuple[type[Contract], str]] = {
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
                            'Steer returns unsupported without sending or queueing.'),
        'subchat_send': (Send, 'Send one ordinary Chat message with exact model/effort labels.'),
        'subchat_recover': (OperationId, 'Recover receipt/answer or progress a queued follow-up; '
                            'never replay an uncertain send.'),
        'subchat_status': (OperationId, 'Read the saved submission without browser interaction.'),
        'subchat_wait': (Wait, 'Wait for an answer without stopping generation or resending. '
                         'Other subchats can progress between observations. Timeout returns '
                         'the current saved state, not a failed generation.'),
    }

    capabilities = getattr(service.backend, 'capabilities', None)
    if capabilities is not None:
        definitions['subchat_capabilities'] = (
            Contract, 'Read configured transport capabilities without network or browser work.')

    if observe_catalog is not None:
        definitions['subchat_catalog'] = (
            Catalog, 'Observe model labels and effort without sending. Optionally select an '
            'exact observed model in the dedicated empty tab to discover its effort choices; '
            'this can change the dedicated profile default. '
            'A partial catalog preserves known models; never infer missing effort choices. '
            'source=http observes the dedicated browser app catalog without picker interaction; '
            'model must be omitted. Returned transport IDs are not UI labels for subchat_send. '
            'Uses the configured transport; inspect subchat_capabilities when available. '
            'An HTTP catalog does not establish independent login or generation support.')

    async def catalog() -> list[JsonValue]:
        return [cast(JsonValue, {
            'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
            'annotations': {'readOnlyHint': name in {'subchat_status', 'subchat_list'},
                            'destructiveHint': name == 'subchat_delete', 'openWorldHint': True},
        }) for name, (schema, description) in definitions.items()]

    async def execute(request: Request) -> Reply:
        try:
            if request.tool == 'subchat_queue_watch':
                watch = QueueWatch.model_validate(request.arguments)
                current = service.store.get(watch.operation_id, owner=None)
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
            if request.tool == 'subchat_capabilities' and capabilities is not None:
                Contract.model_validate(request.arguments)
                data = TypeAdapter(dict[str, JsonValue]).validate_python({
                    **capabilities(),
                    'queue_watch_supported': hasattr(service.backend, 'queue_watch_ready'),
                })
                return Reply(operation_id=request.operation_id, state='completed', data=data)
            if request.tool == 'subchat_list':
                page = service.store.list(SubchatList.model_validate(request.arguments), owner=None)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=page.model_dump(mode='json'))
            if request.tool == 'subchat_cancel':
                target = OperationId.model_validate(request.arguments)
                result = service.store.cancel(target.operation_id, owner=None)
                pending: list[asyncio.Task[Reply] | asyncio.Task[SubchatSubmission]] = [
                    task for task, call in server.calls.items()
                           if call.tool == 'subchat_send'
                           and call.operation_id == target.operation_id]
                recovery = recoveries.get(target.operation_id)
                if recovery is not None:
                    pending.append(recovery)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                recoveries.pop(target.operation_id, None)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=result.model_dump(mode='json'))
            if request.tool == 'subchat_delete':
                target_delete = DeleteRequest.model_validate(request.arguments)
                async with browser_lock:
                    result_delete = await delete_saved(service, target_delete, owner=None)
                return Reply(operation_id=request.operation_id,
                             state='completed' if result_delete.state == 'deleted' else 'unknown',
                             data=result_delete.model_dump(mode='json'))
            if request.tool == 'subchat_message':
                message = Message.model_validate(request.arguments)
                service.store.get(message.target_operation_id, owner=None)
                if message.mode == 'steer':
                    return Reply(operation_id=request.operation_id, state='failed',
                                 error='Immediate steer is not supported by this adapter.',
                                 data={'error_code': 'unsupported', 'mode': 'steer',
                                       'dispatched': False, 'queued': False})
                result = service.queue(request.operation_id, message.target_operation_id,
                                       message.prompt, owner=None)
                return Reply(operation_id=request.operation_id, state='completed',
                             data=result.model_dump(mode='json'))
            if request.tool == 'subchat_wait':
                wait = Wait.model_validate(request.arguments)
                result = service.store.get(wait.operation_id, owner=None)
                deadline = asyncio.timeout(wait.wait_ms / 1000)
                try:
                    async with deadline:
                        while result.state not in {
                                'prepared', 'completed', 'cancelled', 'interrupted'}:
                            result = await observe(wait.operation_id)
                            if result.state not in {
                                    'prepared', 'completed', 'cancelled', 'interrupted'}:
                                # Never hold the browser input lock while the model thinks.
                                await asyncio.sleep(.5)
                except TimeoutError:
                    if not deadline.expired():
                        raise
                    current = service.store.get(wait.operation_id, owner=None)
                    if (current.state != result.state
                            or isinstance(result, SubchatObservedSubmission)
                            and service.store.get(result.observation.operation_id,
                                owner=None).state != 'submitted'):
                        result = current
                return Reply(operation_id=request.operation_id, state='completed',
                             data=result.model_dump(mode='json'))
            if request.tool == 'subchat_catalog' and observe_catalog is not None:
                args_catalog = Catalog.model_validate(request.arguments)
                async with browser_lock:
                    if args_catalog.source == 'http':
                        if args_catalog.model is not None or observe_http_catalog is None:
                            raise ValueError('HTTP catalog requires support and no model selection')
                        observed = await observe_http_catalog()
                    else:
                        observed = await observe_catalog(args_catalog.model)
                data = TypeAdapter(dict[str, JsonValue]).validate_python(observed)
                return Reply(operation_id=request.operation_id, state='completed', data=data)
            if request.tool == 'subchat_send':
                args = Send.model_validate(request.arguments)
                async with browser_lock:
                    result = await service.send(request.operation_id, args.prompt, args.model,
                                                args.effort, owner=None,
                                                conversation_id=args.conversation_id,
                                                work_context=args.work_context,
                                                resources=args.resources,
                                                http_selection=args.http_selection)
            elif request.tool in {'subchat_recover', 'subchat_status'}:
                target = OperationId.model_validate(request.arguments)
                if request.tool == 'subchat_status':
                    result = service.store.get(target.operation_id, owner=None)
                else:
                    result = await observe(target.operation_id)
            else:
                raise ValueError('Unknown subchat tool')
            return Reply(operation_id=request.operation_id, state='completed',
                         data={**result.model_dump(mode='json'),
                               **({'queue_watch': server.queue_watch_states[result.operation_id]}
                                  if result.operation_id in server.queue_watch_states else {})})
        except asyncio.CancelledError:
            if request.tool == 'subchat_send' and not server.closed:
                current = service.store.get(request.operation_id, owner=None)
                if current.state == 'cancelled':
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data=current.model_dump(mode='json'))
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
                         error='Check the dedicated Chat login and account access. Saved '
                               'submissions are preserved; recover their existing IDs after '
                               'restoring access and restarting the subchat controller with the '
                               'same profile and state directory, without sending them again.',
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
        except SubchatPreparationFailed:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Adapter preparation failed before dispatch. Check for an existing '
                               'draft, active generation, missing HTTP selection or unavailable '
                               'model in the dedicated Chat before retrying the same exact '
                               'request ID; '
                               'a new queue also requires its parent selection to match this '
                               'controller. '
                               'For an existing queued message, recover its operation instead.',
                         data={'error_code': 'preparation_failed', 'dispatched': False})
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

    server = SubchatSession(catalog, execute, recoveries,
                            instructions=INSTRUCTIONS if instructions is None else instructions)
    return server
