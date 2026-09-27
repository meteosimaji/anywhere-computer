"""The local Library MCP boundary requires a recoverable upload identity."""

import uuid
from types import SimpleNamespace

import pytest

from anywhere_computer import subchat_library_mcp as library_mcp
from anywhere_computer.models import Request


@pytest.mark.asyncio
async def test_library_mcp_requires_stable_upload_id_before_dispatch(monkeypatch):
    calls = []

    async def upload(path, *, operation_id, require_prepared):
        calls.append((path, operation_id, require_prepared))
        return SimpleNamespace(operation_id=operation_id, state='ready',
                               file_id='file_test', library_item_id='libfile_test',
                               file_name='fixture.txt', file_size=12,
                               automatic_retry=False)

    monkeypatch.setattr(library_mcp, 'upload_local_file', upload)
    server = library_mcp.LibrarySession(library_mcp._catalog, library_mcp._execute)
    initialized = await server.handle({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                   'clientInfo': {'name': 'test', 'version': '1'}},
    })
    assert initialized is not None and 'result' in initialized
    await server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    listing = await server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert listing is not None
    upload_tool = next(tool for tool in listing['result']['tools']
                       if tool['name'] == 'subchat_upload_library')
    assert 'request_id' in upload_tool['inputSchema']['required']
    missing = await server.handle({
        'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {'name': 'subchat_upload_library',
                   'arguments': {'path': '/private/tmp/test.txt'}},
    })
    assert missing is not None and missing['error']['code'] == -32602
    assert calls == []
    operation_id = uuid.uuid4().hex
    accepted = await server.handle({
        'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
        'params': {'name': 'subchat_upload_library',
                   'arguments': {'path': '/private/tmp/test.txt',
                                 'request_id': operation_id}},
    })
    assert accepted is not None
    assert accepted['result']['structuredContent']['state'] == 'completed'
    assert accepted['result']['structuredContent']['data']['upload_operation_id'] == operation_id
    assert accepted['result']['structuredContent']['data']['attachment'] == {
        'id': 'file_test', 'library_file_id': 'libfile_test', 'name': 'fixture.txt',
        'mime_type': 'text/plain', 'size': 12,
    }
    assert calls == [(library_mcp.Path('/private/tmp/test.txt'), operation_id, True)]


@pytest.mark.asyncio
async def test_library_mcp_status_does_not_call_upload(monkeypatch):
    operation_id = uuid.uuid4().hex
    async def status(identity):
        assert identity == operation_id
        return SimpleNamespace(operation_id=identity, state='unknown',
                               file_id='file_test', library_item_id=None,
                               file_name='fixture.txt', file_size=12,
                               automatic_retry=False)
    async def upload(_path, *, operation_id, require_prepared):
        pytest.fail('Status must not send upload bytes')
    monkeypatch.setattr(library_mcp, 'status_local_upload', status)
    monkeypatch.setattr(library_mcp, 'upload_local_file', upload)
    reply = await library_mcp._execute(Request(
        operation_id=uuid.uuid4().hex, tool='subchat_upload_status',
        arguments={'operation_id': operation_id}))
    assert reply.state == 'unknown'
    assert reply.data['state'] == 'unknown'
    assert reply.data['automatic_retry'] is False
    assert reply.data['attachment'] is None
    assert 'subchat_upload_status' in reply.error


def test_attachment_descriptor_needs_ready_and_valid_saved_metadata():
    saved = SimpleNamespace(state='ready', file_id='file_test',
                            library_item_id='libfile_test', file_name='fixture.txt',
                            file_size=109)
    assert library_mcp._attachment(saved)['size'] == 109
    saved.file_size = -1
    assert library_mcp._attachment(saved) is None
    saved.file_size = 109
    saved.state = 'unknown'
    assert library_mcp._attachment(saved) is None


@pytest.mark.asyncio
async def test_library_mcp_ambiguous_upload_error_requires_status(monkeypatch):
    async def uncertain(_path, *, operation_id, require_prepared):
        raise ValueError('Processing receipt did not match')

    monkeypatch.setattr(library_mcp, 'upload_local_file', uncertain)
    identity = uuid.uuid4().hex
    reply = await library_mcp._execute(Request(
        operation_id=identity, tool='subchat_upload_library',
        arguments={'path': '/private/tmp/test.txt'}))
    assert reply.operation_id == identity
    assert reply.state == 'unknown'
    assert 'subchat_upload_status' in reply.error


@pytest.mark.asyncio
async def test_library_mcp_preflight_rejection_is_not_an_unknown_send(monkeypatch):
    async def reject(_path, *, operation_id, require_prepared):
        raise library_mcp.UploadPreflightError('Upload source is unavailable')

    monkeypatch.setattr(library_mcp, 'upload_local_file', reject)
    reply = await library_mcp._execute(Request(
        operation_id=uuid.uuid4().hex, tool='subchat_upload_library',
        arguments={'path': '/private/tmp/absent.txt'}))
    assert reply.state == 'failed'
    assert reply.data == {'dispatched': False}
