"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from pydantic import Field, JsonValue, TypeAdapter

from .mcp_server import Catalog as ToolCatalog
from .mcp_server import Execute, MCPSession
from .models import Contract, OperationId, Reply, Request
from .subchat import (
    SubchatInterrupted,
    SubchatOutcomeUnknown,
    SubchatPreparationFailed,
    Subchats,
    SubchatStaleTarget,
)
from .subchat_state import SubchatList, SubchatSubmission, SubchatWorkContext


class Send(Contract):
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    conversation_id: str | None = None
    work_context: SubchatWorkContext | None = None


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


INSTRUCTIONS = (
    'Ordinary Chat subchats. Use an observed model and effort; never silently substitute. '
    'Choose request_id before subchat_send. Its response is a submission receipt, not a '
    'finished answer. Poll subchat_recover with operation_id equal to that send request_id. '
    'subchat_message mode=queue persists a follow-up bound to the target operation; '
    'recover/wait on its message operation dispatches only after that target completes. '
    'subchat_cancel cancels only a local queued/prepared input, never generation. '
    'No background dispatcher is implied. queued is local acceptance, not delivery. '
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
    'the prompt: explicitly describe relevant work in the exact prompt you send.'
)


class SubchatSession(MCPSession):
    def __init__(self, catalog: ToolCatalog, execute: Execute,
                 tasks: dict[str, asyncio.Task[SubchatSubmission]]) -> None:
        self.recoveries = tasks
        self.closed = False
        self.calls: dict[asyncio.Task[Reply], Request] = {}

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

        super().__init__(catalog, managed, instructions=INSTRUCTIONS)

    async def close(self) -> None:
        self.closed = True
        tasks = [*self.recoveries.values(), *self.calls]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.recoveries.clear()
        self.calls.clear()


def session(service: Subchats, *,
            observe_catalog: Callable[[str | None], Awaitable[dict[str, object]]] | None = None,
            observe_http_catalog: Callable[[], Awaitable[dict[str, object]]] | None = None,
            ) -> SubchatSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    recoveries: dict[str, asyncio.Task[SubchatSubmission]] = {}

    async def observe(operation_id: str) -> SubchatSubmission:
        if server.closed:
            raise RuntimeError('Subchat session is closed')
        current = service.store.get(operation_id, owner=None)
        task = recoveries.get(operation_id)
        if task is None and current.state == 'queued':
            if len(recoveries) >= 8:
                raise RuntimeError('Queued recovery limit reached')

            async def run() -> SubchatSubmission:
                async with browser_lock:
                    return await service.recover(operation_id, owner=None)

            task = asyncio.create_task(run())
            recoveries[operation_id] = task

            def completed(done: asyncio.Task[SubchatSubmission]) -> None:
                if recoveries.get(operation_id) is done:
                    recoveries.pop(operation_id)
                if not done.cancelled():
                    # A timed-out observer may no longer await this result.
                    done.exception()

            task.add_done_callback(completed)
        if task is not None:
            # An observation timeout must not cancel preparation or release its input lock.
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                current = service.store.get(operation_id, owner=None)
                if not server.closed and task.cancelled() and current.state == 'cancelled':
                    return current
                raise
        async with browser_lock:
            return await service.recover(operation_id, owner=None)

    definitions: dict[str, tuple[type[Contract], str]] = {
        'subchat_list': (SubchatList, 'List saved submission summaries without opening Chrome.'),
        'subchat_cancel': (OperationId, 'Cancel an unsent queued/prepared input; never stop Chat.'),
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

    if observe_catalog is not None:
        definitions['subchat_catalog'] = (
            Catalog, 'Observe model labels and effort without sending. Optionally select an '
            'exact observed model in the dedicated empty tab to discover its effort choices; '
            'this can change the dedicated profile default. '
            'A partial catalog preserves known models; never infer missing effort choices. '
            'source=http observes the dedicated browser app catalog without picker interaction; '
            'model must be omitted. Returned transport IDs are not UI labels for subchat_send. '
            'This still requires the dedicated browser, not independent HTTP login.')

    async def catalog() -> list[JsonValue]:
        return [cast(JsonValue, {
            'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
            'annotations': {'readOnlyHint': name in {'subchat_status', 'subchat_list'},
                            'destructiveHint': False, 'openWorldHint': True},
        }) for name, (schema, description) in definitions.items()]

    async def execute(request: Request) -> Reply:
        try:
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
                return Reply(operation_id=request.operation_id, state='completed',
                             data=result.model_dump(mode='json'))
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
                        while result.state not in {'prepared', 'completed', 'cancelled'}:
                            result = await observe(wait.operation_id)
                            if result.state not in {'prepared', 'completed', 'cancelled'}:
                                # Never hold the browser input lock while the model thinks.
                                await asyncio.sleep(.5)
                except TimeoutError:
                    if not deadline.expired():
                        raise
                    result = service.store.get(wait.operation_id, owner=None)
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
                                                work_context=args.work_context)
            elif request.tool in {'subchat_recover', 'subchat_status'}:
                target = OperationId.model_validate(request.arguments)
                if request.tool == 'subchat_status':
                    result = service.store.get(target.operation_id, owner=None)
                else:
                    result = await observe(target.operation_id)
            else:
                raise ValueError('Unknown subchat tool')
            return Reply(operation_id=request.operation_id, state='completed',
                         data=result.model_dump(mode='json'))
        except asyncio.CancelledError:
            if request.tool == 'subchat_send' and not server.closed:
                current = service.store.get(request.operation_id, owner=None)
                if current.state == 'cancelled':
                    return Reply(operation_id=request.operation_id, state='completed',
                                 data=current.model_dump(mode='json'))
            raise
        except SubchatInterrupted:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='The provider recorded an interrupted answer. Inspect the '
                               'conversation; do not automatically resend or advance its queue.',
                         data={'error_code': 'reply_interrupted', 'automatic_retry': False})
        except SubchatStaleTarget:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Queue target is stale; inspect the conversation before continuing.',
                         data={'error_code': 'stale_target', 'dispatched': False})
        except SubchatPreparationFailed:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Adapter preparation failed before dispatch. Inspect the existing '
                               'Chat before retrying the same exact request ID; '
                               'for a queued message, recover its existing operation instead.',
                         data={'error_code': 'preparation_failed', 'dispatched': False})
        except SubchatOutcomeUnknown as error:
            return Reply(operation_id=request.operation_id, state='unknown',
                         error='Submission unconfirmed. Use subchat_recover with '
                               'submission_operation_id; do not send again with a new ID.',
                         data={'submission_operation_id': error.operation_id})
        except Exception as error:
            # Never expose provider error text, invalid prompt contents or account data.
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Subchat call failed; inspect its saved status before retrying.',
                         data={'error_type': type(error).__name__})

    server = SubchatSession(catalog, execute, recoveries)
    return server
