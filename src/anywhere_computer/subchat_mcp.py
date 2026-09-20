"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import Field, JsonValue, TypeAdapter

from .mcp_server import MCPSession
from .models import Contract, OperationId, Reply, Request
from .subchat import SubchatOutcomeUnknown, Subchats


class Send(Contract):
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    conversation_id: str | None = None


class Catalog(Contract):
    model: str | None = Field(default=None, min_length=1, max_length=256)


class Wait(OperationId):
    wait_ms: int = Field(default=10_000, ge=0, le=60_000)


INSTRUCTIONS = (
    'Ordinary Chat subchats. Use an observed model and effort; never silently substitute. '
    'Choose request_id before subchat_send. Its response is a submission receipt, not a '
    'finished answer. Poll subchat_recover with operation_id equal to that send request_id. '
    'Thinking is pending, not failure. Never repeat an uncertain send with a new ID. '
    'subchat_wait waits for a bounded interval and returns the current saved state; '
    'a pending result can be waited on again without stopping generation. '
    'subchat_status reads the saved record without browser interaction. '
    'This local stdio process uses the dedicated profile chosen by its operator; '
    'it does not establish shared workspace access or grant tools to the Chat.'
)


def session(service: Subchats, *,
            observe_catalog: Callable[[str | None], Awaitable[dict[str, object]]] | None = None,
            ) -> MCPSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    definitions: dict[str, tuple[type[Contract], str]] = {
        'subchat_send': (Send, 'Send one ordinary Chat message with exact model/effort labels.'),
        'subchat_recover': (OperationId, 'Recover a submission and answer; never resend.'),
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
                                                conversation_id=args.conversation_id)
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
