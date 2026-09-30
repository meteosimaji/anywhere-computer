"""Actual HTTP socket and subprocess stdio media, including recovery after corruption."""

import base64
import copy
import json
import random
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from test_http_client import http_remote as http_remote
from test_plugin_image_results import image

from anywhere_computer.models import Request
from anywhere_computer.plugin_images import IMAGE_LIMIT
from anywhere_computer.ssh_client import SSHBackend

AUDIO = {'type': 'audio', 'mimeType': 'audio/wav',
         'data': base64.b64encode(b'RIFF\x04\x00\x00\x00WAVE').decode()}
MEDIA = [image(), AUDIO]
SCOPES = frozenset({'mcp_call', 'operations_get'})


@pytest.fixture(scope='module')
def noisy_video(tmp_path_factory):
    if shutil.which('ffmpeg') is None:
        pytest.skip('Real video acceptance requires FFmpeg')
    path = tmp_path_factory.mktemp('video-budget') / 'noise.mkv'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '960x1600', '-r', '1',
        '-i', 'pipe:0', '-frames:v', '1', '-c:v', 'ffv1', '-pix_fmt', 'yuv444p', str(path),
    ], input=random.Random(39).randbytes(960 * 1600 * 3), check=True, timeout=30)
    return path


@pytest.fixture
async def video_peer(request, http_remote, tmp_path, monkeypatch):
    if request.param == 'http':
        yield http_remote[0]
        return
    program = tmp_path / 'video-peer.py'
    program.write_text('''import asyncio, sys
from pathlib import Path
from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import MCPSession, serve_stdio
async def main():
    engine = Engine(Path(sys.argv[1]))
    async def catalog():
        return engine.catalog()
    try:
        await serve_stdio(MCPSession(catalog, engine.execute), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await engine.close()
asyncio.run(main())
''')
    monkeypatch.setattr('anywhere_computer.ssh_client.ssh_command', lambda host: [
        sys.executable, '-I', str(program), str(tmp_path / 'agent'),
    ])
    backend = SSHBackend('fixture')
    try:
        yield backend
    finally:
        await backend.close()


@pytest.mark.parametrize('http_remote', [frozenset({'media_video_frames', 'operations_get'})],
                         indirect=True)
@pytest.mark.parametrize('video_peer', ['http', 'ssh'], indirect=True)
@pytest.mark.parametrize('count', [2, 4])
async def test_real_noisy_video_frames_roundtrip_and_recovery(video_peer, noisy_video, count):
    request = operation('media_video_frames', path=str(noisy_video), timestamps_seconds=[0] * count)
    reply = await video_peer.execute(request)
    assert reply.state == 'completed', reply.error
    assert len(reply.data['content']) == len(reply.data['frames']) == count
    raw_sizes = [len(base64.b64decode(item['data'], validate=True))
                 for item in reply.data['content']]
    assert sum(raw_sizes) <= IMAGE_LIMIT
    assert raw_sizes == [frame['bytes'] for frame in reply.data['frames']]
    recovered = await video_peer.execute(operation(
        'operations_get', operation_id=request.operation_id))
    assert recovered.data['state'] == 'completed'
    assert recovered.data['data']['content'] == reply.data['content']


def operation(tool_name='mcp_call', **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool_name, arguments=arguments or {
        'session_id': 'c' * 32, 'name': 'fixture',
    })


def corrupt(result, fault):
    """Alter only the wire response after the engine committed its original reply."""
    if fault == 'missing':
        result['content'] = result['content'][:1]
    elif fault == 'tampered':
        result['content'][1]['data'] = base64.b64encode(b'\x89PNG\r\n\x1a\ntampered').decode()
    elif fault == 'mime':
        result['structuredContent']['data']['content'][0]['mimeType'] = 'image/jpeg'
    elif fault == 'bytes':
        result['structuredContent']['data']['content'][0]['bytes'] += 1
    elif fault == 'sha256':
        result['structuredContent']['data']['content'][0]['sha256'] = '0' * 64
    elif fault == 'malformed':
        result['content'][1]['data'] = 'private-invalid-base64'
    elif fault:
        raise AssertionError(fault)


@pytest.fixture
async def media_peer(request, http_remote, tmp_path, monkeypatch):
    if request.param == 'http':
        backend, _, _, engine, calls, _ = http_remote
        invoked = []

        async def call(*args, **kwargs):
            invoked.append('effect')
            return {'content': copy.deepcopy(MEDIA), 'isError': True,
                    'structuredContent': {'preview': image()}}

        monkeypatch.setattr(engine.direct_mcp_sessions, 'call', call)

        def preview(args):
            invoked.append('preview')
            return {'content': [image()], 'rendered': True, 'mime_type': 'image/png',
                    'page': args.page, 'pages': 1, 'sha256': args.expected_sha256}

        monkeypatch.setattr('anywhere_computer.engine.preview_document', preview)
        wire = backend.wire
        fault = SimpleNamespace(value='')

        def faulty_wire(resource, method, packet, headers):
            response = wire(resource, method, packet, headers)
            if packet and packet.get('method') == 'tools/call' and (
                    packet['params']['name'] in {'mcp_call', 'documents_preview'}):
                corrupt(response.packet['result'], fault.value)
            return response

        backend.wire = faulty_wire
        yield SimpleNamespace(backend=backend, fault=fault,
                              invoked=lambda: len(invoked),
                              calls=lambda: [p['params']['name'] for p in calls
                                             if p and p.get('method') == 'tools/call'])
        return

    payload = tmp_path / 'media.json'
    payload.write_text(json.dumps(MEDIA))
    fault_file = tmp_path / 'fault.txt'
    fault_file.write_text('')
    invocations = tmp_path / 'invocations.txt'
    invocations.write_text('')
    calls = tmp_path / 'calls.txt'
    calls.write_text('')
    program = tmp_path / 'media-peer.py'
    program.write_text('''import asyncio, copy, json, sys
from pathlib import Path
import anywhere_computer.engine as engine_module
from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import MCPSession, serve_stdio
sys.path.insert(0, sys.argv[2])
from test_remote_media_transport import corrupt
async def main():
    root = Path(sys.argv[1])
    media = json.loads((root / 'media.json').read_text())
    engine = Engine(root / 'agent')
    async def call(*args, **kwargs):
        with (root / 'invocations.txt').open('a') as log:
            log.write('effect\\n')
        return {'content': copy.deepcopy(media), 'isError': True,
                'structuredContent': {'preview': copy.deepcopy(media[0])}}
    engine.direct_mcp_sessions.call = call
    def preview(args):
        with (root / 'invocations.txt').open('a') as log:
            log.write('preview\\n')
        return {'content': [copy.deepcopy(media[0])], 'rendered': True, 'mime_type': 'image/png',
                'page': args.page, 'pages': 1, 'sha256': args.expected_sha256}
    engine_module.preview_document = preview
    async def catalog():
        return engine.catalog()
    class Session(MCPSession):
        async def handle(self, packet):
            result = await super().handle(packet)
            if packet.get('method') == 'tools/call':
                name = packet['params']['name']
                with (root / 'calls.txt').open('a') as log:
                    log.write(name + '\\n')
                if name in {'mcp_call', 'documents_preview'}:
                    corrupt(result['result'], (root / 'fault.txt').read_text())
            return result
    try:
        await serve_stdio(Session(catalog, engine.execute), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await engine.close()
asyncio.run(main())
''')
    from pathlib import Path

    monkeypatch.setattr('anywhere_computer.ssh_client.ssh_command', lambda host: [
        sys.executable, '-I', str(program), str(tmp_path), str(Path(__file__).parent),
    ])
    backend = SSHBackend('fixture')

    class Fault:
        @property
        def value(self):
            return fault_file.read_text()

        @value.setter
        def value(self, value):
            fault_file.write_text(value)

    try:
        yield SimpleNamespace(backend=backend, fault=Fault(),
                              invoked=lambda: len(invocations.read_text().splitlines()),
                              calls=lambda: calls.read_text().splitlines())
    finally:
        await backend.close()


@pytest.mark.parametrize('http_remote', [SCOPES], indirect=True)
@pytest.mark.parametrize('media_peer', ['http', 'ssh'], indirect=True)
async def test_wire_media_is_restored_and_recovered_without_expanding_metadata(media_peer):
    request = operation()
    reply = await media_peer.backend.execute(request)
    assert reply.state == 'completed' and reply.operation_id == request.operation_id
    assert reply.data['content'] == MEDIA
    assert reply.data['is_error'] is True
    assert 'data' not in reply.data['structured_content']['preview']
    recovered = await media_peer.backend.execute(operation(
        'operations_get', operation_id=request.operation_id))
    assert recovered.data['data']['content'] == MEDIA
    assert recovered.data['data']['is_error'] is True
    assert media_peer.invoked() == 1
    assert media_peer.calls() == ['mcp_call', 'operations_get']


@pytest.mark.parametrize('http_remote', [SCOPES], indirect=True)
@pytest.mark.parametrize('media_peer', ['http', 'ssh'], indirect=True)
@pytest.mark.parametrize('fault', ['missing', 'tampered', 'mime', 'bytes', 'sha256', 'malformed'])
async def test_invalid_wire_media_never_replays_and_valid_lookup_recovers(media_peer, fault):
    media_peer.fault.value = fault
    request = operation()
    if isinstance(media_peer.backend, SSHBackend):
        with pytest.raises(ValueError, match='media') as raised:
            await media_peer.backend.execute(request)
        assert 'private-invalid-base64' not in str(raised.value)
    else:
        reply = await media_peer.backend.execute(request)
        assert reply.state == 'unknown' and reply.operation_id == request.operation_id
        assert 'do not repeat' in reply.error
        assert 'private-invalid-base64' not in reply.error
    assert media_peer.invoked() == 1
    assert media_peer.calls() == ['mcp_call']
    recovered = await media_peer.backend.execute(operation(
        'operations_get', operation_id=request.operation_id))
    assert recovered.state == 'completed' and recovered.data['state'] == 'completed'
    assert recovered.data['data']['content'] == MEDIA
    assert media_peer.invoked() == 1
    assert media_peer.calls() == ['mcp_call', 'operations_get']


@pytest.mark.parametrize('http_remote', [SCOPES], indirect=True)
@pytest.mark.parametrize('media_peer', ['http'], indirect=True)
@pytest.mark.parametrize('local', [False, True], ids=['two-http-hops', 'local-device'])
async def test_sdk_receives_native_media_through_device_gateway(
    media_peer, http_remote, tmp_path, local,
):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import AudioContent, ImageContent

    from anywhere_computer.device_router import DeviceRouter
    from anywhere_computer.http_client import HTTPBackend
    from anywhere_computer.http_mcp import HTTPMCP
    from anywhere_computer.mcp_server import MCPSession

    target, _, _, engine, _, _ = http_remote

    async def catalog():
        return engine.catalog()

    router = DeviceRouter(tmp_path / 'gateway', catalog, engine.execute,
                          backend_factory=lambda device: HTTPBackend(
                              target.tokens, wire=target.wire))
    device_id = 'local' if local else router.store.add_http(
        'Media target', target.tokens.resource, target.tokens.client, 'media-fixture',
    )['device_id']

    async def authenticate(token):
        return 'fixture-owner' if token == 'fixture-only-token' else None

    adapter = HTTPMCP(authenticate, lambda owner: MCPSession(router.catalog, router.execute))
    port = await adapter.start()
    request_id = uuid.uuid4().hex
    try:
        # HTTPMCP permits 60 seconds for dispatch; a custom SDK client must
        # outlive that response budget instead of inheriting HTTPX's five seconds.
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer fixture-only-token'},
                                     trust_env=False, timeout=httpx.Timeout(5, read=65)) as http:
            async with streamable_http_client(
                f'http://127.0.0.1:{port}/mcp', http_client=http,
            ) as (reader, writer, _):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    first = await client.call_tool('devices_call', {
                        'device_id': device_id, 'tool': 'mcp_call',
                        'arguments': {'session_id': 'c' * 32, 'name': 'fixture'},
                        'request_id': request_id,
                    })
                    assert first.isError is True
                    assert isinstance(first.content[1], ImageContent)
                    assert isinstance(first.content[2], AudioContent)
                    assert first.content[1].data == image()['data']
                    assert first.content[2].data == AUDIO['data']
                    assert first.structuredContent['data']['tool'] == 'mcp_call'
                    assert image()['data'] not in json.dumps(first.structuredContent)
                    assert image()['data'] not in first.content[0].text
                    recovered = await client.call_tool('devices_call', {
                        'device_id': device_id, 'tool': 'operations_get',
                        'arguments': {'operation_id': request_id},
                        'request_id': uuid.uuid4().hex,
                    })
                    assert recovered.isError is False
                    assert isinstance(recovered.content[1], ImageContent)
                    assert isinstance(recovered.content[2], AudioContent)
                    inner = recovered.structuredContent['data']['result']
                    assert inner['state'] == 'completed' and inner['data']['is_error'] is True
                    assert recovered.content[1].data == image()['data']
                    assert recovered.content[2].data == AUDIO['data']
        assert media_peer.invoked() == 1
    finally:
        await adapter.close()
        router.close()


@pytest.mark.parametrize('http_remote', [SCOPES], indirect=True)
@pytest.mark.parametrize('media_peer', ['http', 'ssh'], indirect=True)
async def test_router_keeps_invalid_media_unknown_and_recovers_without_reexecution(
    media_peer, tmp_path,
):
    from anywhere_computer.device_router import DeviceRouter

    async def catalog():
        return []

    async def unexpected_local(request):
        raise AssertionError('Remote media must not execute locally')

    router = DeviceRouter(tmp_path / 'routes', catalog, unexpected_local,
                          backend_factory=lambda device: media_peer.backend)
    target = router.store.add('Fixture', 'fixture')['device_id']
    media_peer.fault.value = 'missing'
    original = operation('devices_call', device_id=target, tool='mcp_call', arguments={
        'session_id': 'c' * 32, 'name': 'fixture',
    })
    try:
        unknown = await router.execute(original)
        assert unknown.state == 'unknown' and unknown.operation_id == original.operation_id
        assert 'operations_get' in unknown.error and 'do not repeat' in unknown.error
        assert unknown.data['device_id'] == target
        replay = await router.execute(original)
        assert replay.state == 'unknown' and replay.data['previously_forwarded'] is True
        assert media_peer.invoked() == 1
        assert media_peer.calls() == ['mcp_call']
        recovered = await router.execute(operation('devices_call', device_id=target,
            tool='operations_get', arguments={'operation_id': original.operation_id}))
        assert recovered.state == 'completed'
        assert recovered.data['result']['data']['content'] == MEDIA
        assert media_peer.invoked() == 1
        assert media_peer.calls() == ['mcp_call', 'operations_get']
    finally:
        router.close()


@pytest.mark.parametrize('http_remote', [frozenset({'documents_preview', 'operations_get'})],
                         indirect=True)
@pytest.mark.parametrize('media_peer', ['http', 'ssh'], indirect=True)
@pytest.mark.parametrize('routed', [False, True], ids=['direct', 'device'])
@pytest.mark.parametrize('peer_delay', [0, 5.1], ids=['normal', 'slow-peer'])
async def test_document_preview_native_image_and_recovery_cross_real_wire(
    media_peer, tmp_path, routed, peer_delay,
):
    import asyncio

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import ImageContent

    from anywhere_computer.device_router import DeviceRouter
    from anywhere_computer.http_mcp import HTTPMCP
    from anywhere_computer.mcp_server import MCPSession

    execute = media_peer.backend.execute
    executions = 0

    async def delayed_execute(request):
        nonlocal executions
        executions += 1
        if executions == 1:
            await asyncio.sleep(peer_delay)
        return await execute(request)

    media_peer.backend.execute = delayed_execute

    async def no_local_catalog():
        return []

    async def unexpected_local(_request):
        raise AssertionError('A remote document must not render in the gateway')

    router = DeviceRouter(tmp_path / 'document-gateway', no_local_catalog, unexpected_local,
                          backend_factory=lambda device: media_peer.backend)
    target = router.store.add('Document fixture', 'fixture')['device_id']
    backend = router if routed else media_peer.backend

    async def authenticate(token):
        return 'fixture-owner' if token == 'fixture-document-token' else None

    adapter = HTTPMCP(authenticate, lambda owner: MCPSession(backend.catalog, backend.execute))
    port = await adapter.start()
    request_id = uuid.uuid4().hex
    preview_args = {'path': str(tmp_path / 'document.docx'), 'expected_sha256': 'a' * 64, 'page': 1}
    try:
        # Retain bounded connect/write waits while allowing the server's full
        # 60-second dispatch budget, including cold remote subprocess startup.
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer fixture-document-token'},
                                     trust_env=False, timeout=httpx.Timeout(5, read=65)) as http:
            async with streamable_http_client(
                f'http://127.0.0.1:{port}/mcp', http_client=http,
            ) as (reader, writer, _):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    for name, args, identity in [
                        ('documents_preview', preview_args, request_id),
                        ('operations_get', {'operation_id': request_id}, uuid.uuid4().hex),
                    ]:
                        arguments = ({'device_id': target, 'tool': name, 'arguments': args}
                                     if routed else args)
                        result = await client.call_tool('devices_call' if routed else name,
                                                        {**arguments, 'request_id': identity})
                        assert not result.isError
                        images = [item for item in result.content if isinstance(item, ImageContent)]
                        assert len(images) == 1 and images[0].data == image()['data']
                        data = result.structuredContent['data']
                        if routed:
                            assert data['device_id'] == target and data['tool'] == name
                            data = data['result']
                        if name == 'operations_get':
                            assert data['operation_id'] == request_id
                            assert data['state'] == 'completed'
                            data = data['data']
                        assert data['sha256'] == preview_args['expected_sha256']
                        assert data['page'] == data['pages'] == 1
                        assert 'data_base64' not in data
                        assert image()['data'] not in json.dumps(result.structuredContent)
                        assert image()['data'] not in result.content[0].text
        assert media_peer.invoked() == 1
        assert executions == 2
        assert media_peer.calls() == ['documents_preview', 'operations_get']
    finally:
        await adapter.close()
        router.close()


@pytest.mark.parametrize('http_remote', [frozenset({'documents_preview', 'operations_get'})],
                         indirect=True)
@pytest.mark.parametrize('media_peer', ['http', 'ssh'], indirect=True)
@pytest.mark.parametrize('fault', ['missing', 'tampered'])
async def test_document_preview_corrupt_wire_recovers_without_rendering_again(
    media_peer, tmp_path, fault,
):
    media_peer.fault.value = fault
    request = operation('documents_preview', path=str(tmp_path / 'document.docx'),
                        expected_sha256='a' * 64)
    if isinstance(media_peer.backend, SSHBackend):
        with pytest.raises(ValueError, match='media'):
            await media_peer.backend.execute(request)
    else:
        reply = await media_peer.backend.execute(request)
        assert reply.state == 'unknown' and reply.operation_id == request.operation_id
    recovered = await media_peer.backend.execute(operation(
        'operations_get', operation_id=request.operation_id))
    assert recovered.state == 'completed' and recovered.data['state'] == 'completed'
    assert recovered.data['data']['content'] == [image()]
    assert media_peer.invoked() == 1
    assert media_peer.calls() == ['documents_preview', 'operations_get']
