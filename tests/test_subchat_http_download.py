"""A sandbox download is bound to a saved, history-verified final answer."""
import base64
import gzip
import json

import httpx
import pytest
from test_subchat_http_only import credentials, seed

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAnswer, Subchats
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_http_download import download_verified_sandbox_file
from anywhere_computer.subchat_mcp import session as mcp_session
from anywhere_computer.subchat_state import SubchatAccountMismatch, SubchatSubmissions

LINK = 'sandbox:/mnt/data/report.csv'
CONTENT_URL = 'https://chatgpt.com/backend-api/estuary/content?file=fixture'


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


def streamed_json(body):
    return httpx.Response(200, stream=FixtureStream(json.dumps(body).encode()),
                          headers={'content-type': 'application/json'})


def streamed_content(content: bytes, *, content_type: str | None = None):
    headers = {'content-type': content_type} if content_type else {}
    return httpx.Response(200, stream=FixtureStream(content), headers=headers)


def completed(store: SubchatSubmissions, *, account: str = 'fixture-account'):
    submission, payload = seed(store, account=account)
    answer = f'Report: [download]({LINK})'
    payload['messages'][1]['content']['parts'] = [answer]
    store.complete(submission.operation_id, 'answer', answer, owner=None)
    return submission, payload


async def test_image_only_final_has_no_sandbox_text_link(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, _ = seed(store)
        saved = store.complete(submission.operation_id, 'answer', None,
                               answer_type='image', owner=None)
        answer = SubchatAnswer(conversation_id=saved.conversation_id,
                               user_message_id=saved.user_message_id,
                               prompt=saved.prompt, answer_message_id='answer',
                               text=None, answer_type='image')
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: pytest.fail('No download request expected'))) as client:
            with pytest.raises(ValueError, match='no text'):
                await download_verified_sandbox_file(saved, answer, LINK,
                                                     session=credentials(), client=client)
    finally:
        ledger.close()


@pytest.mark.parametrize('bad_url', [
    'https://evil.example/backend-api/estuary/content?file=fixture',
    'https://chatgpt.com.evil.example/backend-api/estuary/content?file=fixture',
    'https://chatgpt.com/other/path?file=fixture',
    'http://chatgpt.com/backend-api/estuary/content?file=fixture',
    'https://chatgpt.com:443/backend-api/estuary/content?file=fixture',
])
async def test_foreign_or_unexpected_content_url_is_never_fetched(tmp_path, bad_url):
    await _case(tmp_path, bad_url=bad_url)


async def _case(tmp_path, *, bad_url=None, size=3, link=LINK,
                account='fixture-account', history_final=True, content_type=None):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = completed(store, account=account)
        if not history_final:
            payload['messages'][1]['end_turn'] = False
        requests = []

        def serve(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers['authorization'] == (
                credentials().authorization.get_secret_value())
            if request.url.path == '/backend-api/conversations/' + submission.conversation_id:
                return httpx.Response(200, json=payload)
            if request.url.path.endswith('/interpreter/download'):
                assert request.url.params['message_id'] == 'answer'
                assert request.url.params['sandbox_path'] == '/mnt/data/report.csv'
                assert request.url.params['download_intent'] == 'true'
                return streamed_json({
                    'download_url': bad_url or CONTENT_URL, 'file_name': 'report.csv',
                    'file_size_bytes': size, 'mime_type': 'text/csv', 'status': 'ready'})
            assert str(request.url) == CONTENT_URL
            return streamed_content(b'abc', content_type=content_type)

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                     follow_redirects=False) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            if account != 'fixture-account':
                with pytest.raises(SubchatAccountMismatch):
                    await backend.download_sandbox_file(submission.operation_id, link)
                assert not requests
            elif not history_final or link != LINK:
                with pytest.raises(ValueError):
                    await backend.download_sandbox_file(submission.operation_id, link)
                assert len(requests) == 1
            elif bad_url or (size is not None and size > 3) or content_type == 'text/html':
                with pytest.raises(ValueError):
                    await backend.download_sandbox_file(submission.operation_id, link)
                assert len(requests) == (3 if content_type == 'text/html' else 2)
            else:
                result = await backend.download_sandbox_file(submission.operation_id, link)
                assert (result.content, result.file_name, result.mime_type,
                        result.file_size_bytes) == (b'abc', 'report.csv', 'text/csv', 3)
                assert [request.method for request in requests] == ['GET'] * 3
                assert all(request.url.host == 'chatgpt.com' for request in requests)
    finally:
        ledger.close()


async def test_download_success_and_rejects_missing_link_or_unverified_final(tmp_path):
    await _case(tmp_path / 'ok')
    await _case(tmp_path / 'unknown_declared_size', size=None)
    await _case(tmp_path / 'missing', link='sandbox:/mnt/data/other.csv')
    await _case(tmp_path / 'pending', history_final=False)
    await _case(tmp_path / 'html_response', content_type='text/html')


async def test_download_rejects_other_account_and_oversize(tmp_path):
    await _case(tmp_path / 'account', account='other-account')
    await _case(tmp_path / 'size', size=16 * 1024 * 1024 + 1)


async def test_streamed_content_exceeding_declared_size_is_rejected(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = completed(store)

        def serve(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.endswith('/interpreter/download'):
                return streamed_json({
                    'download_url': CONTENT_URL, 'file_name': 'report.csv',
                    'file_size_bytes': 2, 'mime_type': 'text/csv', 'status': 'ready'})
            return streamed_content(b'abc')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            async def factory():
                return client

            with pytest.raises(ValueError, match='size does not match'):
                backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
                await backend.download_sandbox_file(submission.operation_id, LINK)
    finally:
        ledger.close()


async def test_oversized_metadata_stops_reading_before_file_request(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = completed(store)
        chunks_read = 0
        file_requested = False

        class MetadataStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal chunks_read
                for _ in range(4):
                    chunks_read += 1
                    yield b'x' * 40_000

        def serve(request: httpx.Request) -> httpx.Response:
            nonlocal file_requested
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.endswith('/interpreter/download'):
                return httpx.Response(200, stream=MetadataStream(),
                                      headers={'content-type': 'application/json'})
            file_requested = True
            raise AssertionError('File content must not be requested')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            with pytest.raises(ValueError, match='metadata is too large'):
                await backend.download_sandbox_file(submission.operation_id, LINK)
        assert chunks_read == 2
        assert not file_requested
    finally:
        ledger.close()


@pytest.mark.parametrize('compressed_stage', ['metadata', 'file'])
async def test_compressed_http_response_is_rejected_without_decoding(tmp_path,
                                                                    compressed_stage):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = completed(store)
        requests = []
        compressed = gzip.compress(b'x' * (8 * 1024 * 1024))

        def serve(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            assert request.headers['accept-encoding'] == 'identity'
            if request.url.path.endswith('/interpreter/download'):
                if compressed_stage == 'metadata':
                    return httpx.Response(200, content=compressed, headers={
                        'content-type': 'application/json', 'content-encoding': 'gzip'})
                return streamed_json({
                    'download_url': CONTENT_URL, 'file_name': 'report.csv',
                    'file_size_bytes': 3, 'mime_type': 'text/csv', 'status': 'ready'})
            return httpx.Response(200, content=compressed, headers={
                'content-type': 'text/csv', 'content-encoding': 'gzip'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            with pytest.raises(ValueError, match='Compressed Chat file'):
                await backend.download_sandbox_file(submission.operation_id, LINK)
        assert len(requests) == (2 if compressed_stage == 'metadata' else 3)
    finally:
        ledger.close()


async def test_read_only_mcp_exposes_one_verified_file_without_local_storage(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, payload = completed(store)
        requests = []

        def serve(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.startswith('/backend-api/conversations/'):
                return httpx.Response(200, json=payload)
            if request.url.path.endswith('/interpreter/download'):
                return streamed_json({
                    'download_url': CONTENT_URL, 'file_name': 'report.csv',
                    'file_size_bytes': 3, 'mime_type': 'text/csv', 'status': 'ready'})
            assert str(request.url) == CONTENT_URL
            return streamed_content(b'abc')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            async def factory():
                return client

            backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
            server = mcp_session(Subchats(store, backend), read_only=True,
                                 observe_http_catalog=backend.http_catalog)
            assert 'subchat_download_file' in {tool['name'] for tool in await server.catalog()}
            before = set(tmp_path.iterdir())
            result = await server.execute(Request(operation_id='1' * 32,
                tool='subchat_download_file', arguments={
                    'operation_id': submission.operation_id, 'sandbox_link': LINK}))
            assert result.state == 'completed'
            assert result.data is not None
            assert base64.b64decode(result.data['content_base64']) == b'abc'
            assert result.data['submission_operation_id'] == submission.operation_id
            assert result.data['file_name'] == 'report.csv'
            assert len(requests) == 3
            assert set(tmp_path.iterdir()) == before

            requests.clear()
            missing = await server.execute(Request(operation_id='2' * 32,
                tool='subchat_download_file', arguments={
                    'operation_id': submission.operation_id,
                    'sandbox_link': 'sandbox:/mnt/data/other.csv'}))
            assert missing.state == 'failed'
            assert len(requests) == 1  # Final answer check only; no file endpoint.

            requests.clear()
            oversized = await server.execute(Request(operation_id='3' * 32,
                tool='subchat_download_file', arguments={
                    'operation_id': submission.operation_id, 'sandbox_link': LINK,
                    'max_bytes': 2}))
            assert oversized.state == 'failed'
            assert oversized.data == {'error_code': 'file_too_large', 'max_bytes': 2,
                                      'automatic_retry': False}
            assert len(requests) == 2

            invalid = await server.execute(Request(operation_id='4' * 32,
                tool='subchat_download_file', arguments={
                    'operation_id': submission.operation_id, 'sandbox_link': LINK,
                    'max_bytes': 512 * 1024 + 1}))
            assert invalid.data is not None
            assert invalid.data['error_code'] == 'invalid_parameter'
    finally:
        ledger.close()
