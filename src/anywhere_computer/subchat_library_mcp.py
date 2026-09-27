"""Local, explicitly invoked Library upload tools for the selected macOS account."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import sys
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .mcp_server import REQUEST_ID_SCHEMA, MCPSession, rpc_error, serve_stdio
from .models import Reply, Request
from .subchat_content import SubchatAttachment
from .subchat_library_upload import (
    UploadPreflightError,
    status_local_upload,
    upload_local_file,
)


class UploadFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(min_length=1, max_length=4096)


class UploadStatus(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')


async def _catalog() -> list[JsonValue]:
    upload_schema = UploadFile.model_json_schema()
    upload_schema['properties']['request_id'] = dict(REQUEST_ID_SCHEMA)
    upload_schema['required'].append('request_id')
    return [
        {
            'name': 'subchat_upload_library',
            'description': 'Upload one explicit local file to the selected macOS ChatGPT '
                           'Library account after local --prepare approval of the exact '
                           'file and request_id. Supply that stable ID before calling. '
                           'An interrupted upload is never automatically resent; inspect '
                           'the same operation with subchat_upload_status.',
            'inputSchema': upload_schema,
            'annotations': {'readOnlyHint': False, 'openWorldHint': True},
        },
        {
            'name': 'subchat_upload_status',
            'description': 'Read and reconcile one saved Library upload by its original '
                           'operation_id without reading or resending local file bytes.',
            'inputSchema': UploadStatus.model_json_schema(),
            'annotations': {'readOnlyHint': True, 'openWorldHint': True},
        },
    ]


class LibrarySession(MCPSession):
    async def handle(self, packet: JsonValue) -> dict[str, JsonValue] | None:
        if isinstance(packet, dict) and packet.get('method') == 'tools/call':
            params = packet.get('params')
            if isinstance(params, dict) and params.get('name') == 'subchat_upload_library':
                arguments = params.get('arguments')
                supplied = arguments.get('request_id') if isinstance(arguments, dict) else None
                if (not isinstance(supplied, str)
                        or re.fullmatch(r'[a-f0-9]{32}', supplied) is None):
                    return rpc_error(packet.get('id'), -32602,
                                     'A stable request_id is required before uploading')
        return await super().handle(packet)


def _attachment(saved: object) -> dict[str, JsonValue] | None:
    """Return an exact send-ready descriptor only after Library readiness."""
    if getattr(saved, 'state', None) != 'ready':
        return None
    file_id = getattr(saved, 'file_id', None)
    library_id = getattr(saved, 'library_item_id', None)
    name = getattr(saved, 'file_name', None)
    size = getattr(saved, 'file_size', None)
    if not isinstance(name, str):
        return None
    mime_type, _ = mimetypes.guess_type(name)
    if mime_type is None:
        return None
    try:
        attachment = SubchatAttachment.model_validate({
            'id': file_id, 'library_file_id': library_id, 'name': name,
            'mime_type': mime_type, 'size': size,
        })
    except ValidationError:
        return None
    return attachment.model_dump(mode='json')


async def _execute(request: Request) -> Reply:
    try:
        if request.tool == 'subchat_upload_library':
            upload_arguments = UploadFile.model_validate(request.arguments)
            saved = await upload_local_file(
                Path(upload_arguments.path), operation_id=request.operation_id,
                require_prepared=True)
        elif request.tool == 'subchat_upload_status':
            status_arguments = UploadStatus.model_validate(request.arguments)
            saved = await status_local_upload(status_arguments.operation_id)
        else:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Unknown Library upload tool')
    except ValidationError as error:
        return Reply(operation_id=request.operation_id, state='failed',
                     error='Invalid Library upload arguments: ' + error.errors()[0]['msg'])
    except UploadPreflightError as error:
        return Reply(operation_id=request.operation_id, state='failed',
                     data={'dispatched': False}, error=str(error))
    except ValueError as error:
        if request.tool == 'subchat_upload_library':
            return Reply(operation_id=request.operation_id, state='unknown',
                         error=str(error) + '. Inspect the original operation_id with '
                               'subchat_upload_status before another upload decision.')
        return Reply(operation_id=request.operation_id, state='failed', error=str(error))
    except (httpx.HTTPError, OSError, TimeoutError):
        return Reply(operation_id=request.operation_id, state='unknown',
                     error='Library upload outcome is unverified. Inspect the original '
                           'operation_id with subchat_upload_status; do not upload again.')
    return Reply(operation_id=request.operation_id,
                 state='completed' if saved.state == 'ready' else 'unknown',
                 error=None if saved.state == 'ready' else (
                     'Library upload is not confirmed ready. Inspect the original '
                     'operation_id with subchat_upload_status; do not upload again.'),
                 data={
        'upload_operation_id': saved.operation_id,
        'state': saved.state,
        'file_id': saved.file_id,
        'library_item_id': saved.library_item_id,
        'attachment': _attachment(saved),
        'automatic_retry': saved.automatic_retry,
    })


def main() -> None:
    server = LibrarySession(_catalog, _execute, instructions=(
        'Local macOS Library upload for the explicitly selected Chat account. '
        'The owner must first run anywhere-subchat-upload --prepare for the exact '
        'absolute file path and request_id. Calls without a matching local '
        'preparation are refused before browser dispatch. '
        'Choose and retain one request_id per intended upload. If a response is '
        'missing or an outcome is unknown, use subchat_upload_status with the '
        'original operation ID. Never create another ID to retry an uncertain upload.'
    ))
    asyncio.run(serve_stdio(server, sys.stdin.buffer, sys.stdout.buffer))


if __name__ == '__main__':
    main()
