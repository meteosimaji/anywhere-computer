"""Local stdio adapter, reusable through Anywhere's existing direct-MCP sessions."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import Field, JsonValue, TypeAdapter

from .mcp_server import MCPSession
from .models import Contract, Empty, OperationId, Reply, Request
from .subchat import SubchatOutcomeUnknown, Subchats


class Send(Contract):
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    conversation_id: str | None = None


INSTRUCTIONS = (
    'Ordinary Chat subchats. Use an observed model and effort; never silently substitute. '
    'Choose request_id before subchat_send. Its response is a submission receipt, not a '
    'finished answer. Poll subchat_recover with operation_id equal to that send request_id. '
    'Thinking is pending, not failure. Never repeat an uncertain send with a new ID. '
    'subchat_status reads the saved record without browser interaction. '
    'This local stdio process uses the dedicated profile chosen by its operator; '
    'it does not establish shared workspace access or grant tools to the Chat.'
)


def session(service: Subchats, *,
            observe_catalog: Callable[[], Awaitable[dict[str, object]]] | None = None,
            ) -> MCPSession:
    # Clipboard interception and draft preparation must not interleave across calls.
    browser_lock = asyncio.Lock()
    definitions: dict[str, tuple[type[Contract], str]] = {
        'subchat_send': (Send, 'Send one ordinary Chat message with exact model/effort labels.'),
        'subchat_recover': (OperationId, 'Recover a submission and answer; never resend.'),
        'subchat_status': (OperationId, 'Read the saved submission without browser interaction.'),
    }

    if observe_catalog is not None:
        definitions['subchat_catalog'] = (
            Empty, 'Observe model labels and effort for the selected model without sending. '
            'A partial catalog preserves known models; never infer missing effort choices.')

    async def catalog() -> list[JsonValue]:
        return [cast(JsonValue, {
            'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
            'annotations': {'readOnlyHint': name == 'subchat_status',
                            'destructiveHint': False, 'openWorldHint': True},
        }) for name, (schema, description) in definitions.items()]

    async def execute(request: Request) -> Reply:
        try:
            if request.tool == 'subchat_catalog' and observe_catalog is not None:
                Empty.model_validate(request.arguments)
                async with browser_lock:
                    observed = await observe_catalog()
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
