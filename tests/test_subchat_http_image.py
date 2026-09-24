"""History-bound image reads never accept caller-supplied assets or dispatch work."""
import base64
import json
import struct
import zlib

import httpx
import pytest
from test_subchat_http_download import streamed_content, streamed_json
from test_subchat_http_only import credentials, seed

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_browser.history import project_history
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_http_image import _image_dimensions
from anywhere_computer.subchat_mcp import session as mcp_session
from anywhere_computer.subchat_state import SubchatSubmissions

FILE_ID = 'file_' + 'a' * 32
CONTENT_URL = 'https://chatgpt.com/backend-api/estuary/content?id=' + FILE_ID + '&sig=fixture'


def png(width=1, height=1):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))

    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    pixels = b'\x00' + b'\x00\x00\xff' * width
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(pixels * height)) + chunk(b'IEND', b''))


def image_history(store, *, account='fixture-account', completed=False):
    submission, payload = seed(store, account=account)
    user = payload['messages'][0]
    tool = {'id': 'tool-image', 'author': {'role': 'tool'},
            'content': {'content_type': 'multimodal_text', 'parts': [{
                'content_type': 'image_asset_pointer',
                'asset_pointer': 'sediment://' + FILE_ID,
                'mime_type': 'image/png', 'size_bytes': len(png()),
                'width': 1, 'height': 1}]},
            'metadata': {**user['metadata'], 'request_id': 'async-request',
                         'async_source': 'image-generation'},
            'status': 'finished_successfully', 'channel': 'final'}
    payload['messages'] = [user, tool]
    if completed:
        store.complete(submission.operation_id, 'answer', 'done', owner=None)
    return submission, payload


async def test_image_download_accepts_finished_tool_before_final_answer(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved, payload = image_history(store)
        requests = []

        def serve(request):
            requests.append(request)
            assert request.method == 'GET' and request.url.host == 'chatgpt.com'
            assert (request.headers['authorization'] ==
                    credentials().authorization.get_secret_value())
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.startswith('/backend-api/files/download/'):
                assert request.url.path.endswith('/' + FILE_ID)
                assert request.url.params['conversation_id'] == saved.conversation_id
                assert request.url.params['inline'] == 'false'
                return streamed_json({
                    'status': 'success', 'download_url': CONTENT_URL,
                    'file_size_bytes': len(png()),
                })
            assert str(request.url) == CONTENT_URL
            return streamed_content(png(), content_type='image/png')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                     follow_redirects=False) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            result = await backend.download_image(saved.operation_id, max_bytes=2_000_000)
            assert result.content == png()
            assert (result.mime_type, result.width, result.height,
                    result.submission_state) == ('image/png', 1, 1, 'submitted')
            server = mcp_session(Subchats(store, backend), read_only=True,
                                 observe_http_catalog=backend.http_catalog)
            assert 'subchat_download_image' in {tool['name'] for tool in await server.catalog()}
            response = await server.execute(Request(operation_id='b' * 32,
                tool='subchat_download_image', arguments={'operation_id': saved.operation_id}))
            assert response.state == 'completed' and response.data is not None, response
            assert response.data['submission_state'] == 'submitted'
            assert response.data['final_answer_verified'] is False
            assert base64.b64decode(response.data['content_base64']) == png()
            assert len(requests) == 6
            chunk = await server.execute(Request(operation_id='d' * 32,
                tool='subchat_download_image', arguments={
                    'operation_id': saved.operation_id, 'offset': 2, 'chunk_bytes': 10}))
            assert chunk.state == 'completed' and chunk.data is not None
            assert chunk.data['offset'] == 2 and chunk.data['next_offset'] == 12
            assert base64.b64decode(chunk.data['content_base64']) == png()[2:12]
            assert len(requests) == 9
            invalid = await server.execute(Request(operation_id='c' * 32,
                tool='subchat_download_image', arguments={
                    'operation_id': saved.operation_id, 'asset_pointer': 'sediment://' + FILE_ID}))
            assert invalid.state == 'failed'
            assert len(requests) == 9
    finally:
        ledger.close()


async def test_image_download_after_image_only_final_is_still_bound(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved, payload = image_history(store)
        payload['messages'].append({
            'id': 'answer', 'author': {'role': 'assistant'},
            'content': {'content_type': 'text', 'parts': ['']},
            'metadata': {**payload['messages'][0]['metadata'],
                         'is_complete': True, 'finish_details': {'type': 'stop'}},
            'status': 'finished_successfully', 'channel': 'final', 'end_turn': True,
        })
        observed = project_history(json.dumps(payload).encode(), saved)
        assert observed is not None and observed.answer_type == 'image'
        completed = store.complete(saved.operation_id, observed.answer_message_id, observed.text,
                                   answer_type=observed.answer_type, owner=None)
        assert completed.answer is None and completed.answer_type == 'image'

        def serve(request):
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.startswith('/backend-api/files/download/'):
                return streamed_json({'status': 'success', 'download_url': CONTENT_URL,
                                      'file_size_bytes': len(png())})
            assert str(request.url) == CONTENT_URL
            return streamed_content(png(), content_type='image/png')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                     follow_redirects=False) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            result = await backend.download_image(saved.operation_id, max_bytes=2_000_000)
            assert result.content == png() and result.submission_state == 'completed'
            server = mcp_session(Subchats(store, backend), read_only=True,
                                 observe_http_catalog=backend.http_catalog)
            reply = await server.execute(Request(operation_id='e' * 32,
                tool='subchat_download_image', arguments={'operation_id': saved.operation_id}))
            assert reply.state == 'completed' and reply.data is not None
            assert reply.data['final_answer_verified'] is True
            assert base64.b64decode(reply.data['content_base64']) == png()
    finally:
        ledger.close()


@pytest.mark.parametrize('placement', ['final_only', 'tool_with_caption'])
async def test_completed_image_projection_matches_download_source(tmp_path, placement):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved, payload = image_history(store)
        user, tool = payload['messages']
        final = {'id': 'answer', 'author': {'role': 'assistant'},
                 'content': {'content_type': 'text', 'parts': ['Caption']},
                 'metadata': {**user['metadata'], 'is_complete': True,
                              'finish_details': {'type': 'stop'}},
                 'status': 'finished_successfully', 'channel': 'final', 'end_turn': True}
        if placement == 'final_only':
            final['content'] = {'content_type': 'multimodal_text',
                                'parts': [tool['content']['parts'][0].copy()]}
            payload['messages'] = [user, final]
        else:
            payload['messages'].append(final)
        observed = project_history(json.dumps(payload).encode(), saved)
        assert observed is not None
        assert observed.answer_type == ('image' if placement == 'final_only' else 'multimodal')
        completed = store.complete(saved.operation_id, observed.answer_message_id, observed.text,
                                   answer_type=observed.answer_type, owner=None)

        def serve(request):
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.startswith('/backend-api/files/download/'):
                return streamed_json({'status': 'success', 'download_url': CONTENT_URL,
                                      'file_size_bytes': len(png())})
            assert str(request.url) == CONTENT_URL
            return streamed_content(png(), content_type='image/png')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                     follow_redirects=False) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            result = await backend.download_image(completed.operation_id, max_bytes=2_000_000)
            assert result.content == png() and result.submission_state == 'completed'
    finally:
        ledger.close()


@pytest.mark.parametrize('case', ['two_final_images', 'tool_and_final_image',
                                  'missing_metadata', 'changed_final'])
async def test_image_completion_and_download_reject_ambiguous_or_changed_history(tmp_path, case):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved, payload = image_history(store)
        user, tool = payload['messages']
        final = {'id': 'answer', 'author': {'role': 'assistant'},
                 'content': {'content_type': 'text', 'parts': ['']},
                 'metadata': {**user['metadata'], 'is_complete': True,
                              'finish_details': {'type': 'stop'}},
                 'status': 'finished_successfully', 'channel': 'final', 'end_turn': True}
        payload['messages'].append(final)
        if case == 'two_final_images':
            payload['messages'] = [user, final]
            final['content'] = {'content_type': 'multimodal_text',
                                'parts': [tool['content']['parts'][0].copy()] * 2}
        elif case == 'tool_and_final_image':
            final['content'] = {'content_type': 'multimodal_text',
                                'parts': [tool['content']['parts'][0].copy()]}
        elif case == 'missing_metadata':
            del tool['content']['parts'][0]['width']
        else:
            observed = project_history(json.dumps(payload).encode(), saved)
            assert observed is not None and observed.answer_type == 'image'
            store.complete(saved.operation_id, observed.answer_message_id, observed.text,
                           answer_type=observed.answer_type, owner=None)
            final['content'] = {'content_type': 'text', 'parts': ['Changed']}
        if case != 'changed_final':
            assert project_history(json.dumps(payload).encode(), saved) is None
        requests = []

        def serve(request):
            requests.append(request)
            assert request.url.path.startswith('/backend-api/conversations/')
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                     follow_redirects=False) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            with pytest.raises(ValueError):
                await backend.download_image(saved.operation_id, max_bytes=2_000_000)
            assert len(requests) == 1
    finally:
        ledger.close()


async def test_browser_send_backend_exposes_same_bound_image_download(
    tmp_path, monkeypatch,
):
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as browser_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved, payload = image_history(store)
        observed = []

        def serve(request):
            observed.append(request.url.path)
            if request.url.path.startswith('/backend-api/files/download/'):
                return streamed_json({
                    'status': 'success', 'download_url': CONTENT_URL,
                    'file_size_bytes': len(png()),
                })
            assert str(request.url) == CONTENT_URL
            return streamed_content(png(), content_type='image/png')

        original_client = httpx.AsyncClient
        monkeypatch.setattr(
            browser_module.httpx, 'AsyncClient',
            lambda **kwargs: original_client(
                transport=httpx.MockTransport(serve), follow_redirects=False))
        context = object()

        async def browser():
            return context

        async def chrome_session(_context, _client, **kwargs):
            assert _context is context
            return credentials()

        backend = BrowserSubchatBackend(
            browser, http_read=True, httpx_generation=True,
            background_pages=True, store=store)
        monkeypatch.setattr(backend, '_browser', browser)
        monkeypatch.setattr(backend, '_read_context', browser)
        monkeypatch.setattr(backend._http_reader, '_history_payload',
                            lambda _context, _saved: bytes_result(payload))
        monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', chrome_session)
        result = await backend.download_image(saved.operation_id, max_bytes=2_000_000)
        assert result.content == png()
        assert len(observed) == 2
        assert 'subchat_download_image' in {
            tool['name'] for tool in await mcp_session(
                Subchats(store, backend)).catalog()}
    finally:
        ledger.close()


async def bytes_result(value):
    import json

    return json.dumps(value).encode()


@pytest.mark.parametrize('case', [
    'other_account', 'wrong_prompt', 'prior_tool', 'other_turn', 'ambiguous_turn',
    'two_images',
    'bad_pointer', 'oversize_history', 'foreign_url', 'different_asset',
    'oversize_metadata', 'redirect_metadata', 'redirect_content',
    'wrong_type', 'bad_png', 'truncated',
])
async def test_image_download_rejects_unbound_or_invalid_assets(tmp_path, case):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved, payload = image_history(store, account=(
            'another-account' if case == 'other_account' else 'fixture-account'))
        tool = payload['messages'][1]
        image = tool['content']['parts'][0]
        if case == 'wrong_prompt':
            payload['messages'][0]['content']['parts'] = ['different']
        elif case == 'prior_tool':
            payload['messages'].reverse()
        elif case == 'other_turn':
            tool['metadata']['working_turn_id'] = 'other-turn'
        elif case == 'ambiguous_turn':
            payload['messages'].append({**payload['messages'][0], 'id': 'another-user'})
        elif case == 'two_images':
            tool['content']['parts'].append(image.copy())
        elif case == 'bad_pointer':
            image['asset_pointer'] = 'sediment://file_other'
        elif case == 'oversize_history':
            image['size_bytes'] = 9 * 1024 * 1024
        requests = []

        def serve(request):
            requests.append(request)
            assert request.method == 'GET' and request.url.host == 'chatgpt.com'
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.startswith('/backend-api/files/download/'):
                if case == 'redirect_metadata':
                    return httpx.Response(302, headers={'location': CONTENT_URL})
                url = CONTENT_URL
                if case == 'foreign_url':
                    url = 'https://other.example/backend-api/estuary/content?id=' + FILE_ID
                elif case == 'different_asset':
                    url = CONTENT_URL.replace(FILE_ID, 'file_' + 'b' * 32)
                size = len(png()) + (1 if case == 'oversize_metadata' else 0)
                return streamed_json({'status': 'success', 'download_url': url,
                                      'file_size_bytes': size})
            assert str(request.url) == CONTENT_URL
            if case == 'redirect_content':
                return httpx.Response(302, headers={'location': CONTENT_URL})
            if case == 'wrong_type':
                return streamed_content(png(), content_type='text/html')
            if case == 'bad_png':
                return streamed_content(b'not a PNG' + b'0' * (len(png()) - 9),
                                        content_type='image/png')
            if case == 'truncated':
                return streamed_content(png()[:-1], content_type='image/png')
            raise AssertionError('Unexpected image download')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                     follow_redirects=False) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            with pytest.raises((ValueError, ConnectionError), match='|'.join((
                    'match', 'image', 'Image', 'PNG', 'Chat', 'another account'))):
                await backend.download_image(saved.operation_id, max_bytes=8 * 1024 * 1024)
            expected = (0 if case == 'other_account' else
                        1 if case in {'wrong_prompt', 'prior_tool', 'other_turn',
                                      'ambiguous_turn', 'two_images', 'bad_pointer',
                                      'oversize_history'} else
                        2 if case in {'foreign_url', 'different_asset', 'oversize_metadata',
                                      'redirect_metadata'} else 3)
            assert len(requests) == expected
    finally:
        ledger.close()


def test_image_byte_signatures_cover_jpeg_and_webp():
    jpeg = (b'\xff\xd8\xff\xc0' + struct.pack('>H', 11) +
            b'\x08' + struct.pack('>HH', 3, 4) + b'\x01\x01\x11\x00' +
            b'\xff\xd9')
    assert _image_dimensions(jpeg, 'image/jpeg') == (4, 3)
    webp_data = b'\x2f' + ((3 << 14) | 4).to_bytes(4, 'little')
    webp = b'RIFF' + struct.pack('<I', 4 + 8 + 6) + b'WEBP' + b'VP8L' + struct.pack(
        '<I', 5) + webp_data + b'\x00'
    assert _image_dimensions(webp, 'image/webp') == (5, 4)
    with pytest.raises(ValueError):
        _image_dimensions(webp[:-1], 'image/webp')
