"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from pydantic import Field, JsonValue, TypeAdapter

from .mcp_server import MCPSession
from .models import Contract, OperationId, Reply, Request
from .subchat import SubchatOutcomeUnknown, Subchats, SubchatStaleTarget
from .subchat_state import SubchatWorkContext


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


class Wait(OperationId):
    # Leave ample transport/cleanup headroom under direct MCP's 30-second deadline.
    wait_ms: int = Field(default=1000, ge=0, le=10_000)


INSTRUCTIONS = (
    'Ordinary Chat subchats. Use an observed model and effort; never silently substitute. '
    'Choose request_id before subchat_send. Its response is a submission receipt, not a '
    'finished answer. Poll subchat_recover with operation_id equal to that send request_id. '
    'subchat_message mode=queue persists a follow-up bound to the target operation; '
    'recover/wait on its message operation dispatches only after that target completes. '
    'No background dispatcher is implied. queued is local acceptance, not delivery. '
    'mode=steer is currently unsupported by this ordinary Chat adapter; it never falls '
    'back to queue or Stop. Submitted is a receipt, not proof of consumption. '
    'Thinking is pending, not failure. Never repeat an uncertain send with a new ID. '
    'subchat_wait defaults to one second, allows at most ten seconds, and returns the '
    'current saved state; '
    'a pending result can be waited on again without stopping generation. '
    'subchat_status reads the saved record without browser interaction. '
    'If a new Chat remains sending without a conversation_id after process loss, '
    'automatic recovery may be impossible: preserve unknown and reconcile manually; '
    'never scan unrelated history or resend to manufacture a receipt. '
    'This local stdio process uses the dedicated profile chosen by its operator; '
    'it does not establish shared workspace access or grant tools to the Chat.'
    ' Optional work_context is caller-supplied provenance saved with the receipt, '
    'not a grant or verified file snapshot. It is not automatically inserted into '
    'the prompt: explicitly describe relevant work in the exact prompt you send.'
)


def session(service: Subchats, *,
            observe_catalog: Callable[[str | None], Awaitable[dict[str, object]]] | None = None,
            ) -> MCPSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    definitions: dict[str, tuple[type[Contract], str]] = {
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
            'A partial catalog preserves known models; never infer missing effort choices.')

    async def catalog() -> list[JsonValue]:
        return [cast(JsonValue, {
            'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
            'annotations': {'readOnlyHint': name == 'subchat_status',
                            'destructiveHint': False, 'openWorldHint': True},
        }) for name, (schema, description) in definitions.items()]

    async def execute(request: Request) -> Reply:
        try:
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
                        while result.state not in {'prepared', 'completed'}:
                            async with browser_lock:
                                result = await service.recover(wait.operation_id, owner=None)
                            if result.state not in {'prepared', 'completed'}:
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
                    async with browser_lock:
                        result = await service.recover(target.operation_id, owner=None)
            else:
                raise ValueError('Unknown subchat tool')
            return Reply(operation_id=request.operation_id, state='completed',
                         data=result.model_dump(mode='json'))
        except SubchatStaleTarget:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Queue target is stale; inspect the conversation before continuing.',
                         data={'error_code': 'stale_target', 'dispatched': False})
        except SubchatOutcomeUnknown:
            return Reply(operation_id=request.operation_id, state='unknown',
                         error='Submission unconfirmed. Use subchat_recover with this ID; '
                               'do not send again with a new ID.')
        except Exception as error:
            # Never expose provider error text, invalid prompt contents or account data.
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Subchat call failed; inspect its saved status before retrying.',
                         data={'error_type': type(error).__name__})

    return MCPSession(catalog, execute, instructions=INSTRUCTIONS)
