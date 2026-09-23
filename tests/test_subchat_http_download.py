"""A sandbox download is bound to a saved, history-verified final answer."""
import httpx
import pytest
from test_subchat_http_only import credentials, seed

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_state import SubchatAccountMismatch, SubchatSubmissions

LINK = 'sandbox:/mnt/data/report.csv'
CONTENT_URL = 'https://chatgpt.com/backend-api/estuary/content?file=fixture'


def completed(store: SubchatSubmissions, *, account: str = 'fixture-account'):
    submission, payload = seed(store, account=account)
    answer = f'Report: [download]({LINK})'
    payload['messages'][1]['content']['parts'] = [answer]
    store.complete(submission.operation_id, 'answer', answer, owner=None)
    return submission, payload


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
                account='fixture-account', history_final=True):
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
                return httpx.Response(200, json={
                    'download_url': bad_url or CONTENT_URL, 'file_name': 'report.csv',
                    'file_size_bytes': size, 'mime_type': 'text/csv', 'status': 'ready'})
            assert str(request.url) == CONTENT_URL
            return httpx.Response(200, content=b'abc')

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
            elif bad_url or (size is not None and size > 3):
                with pytest.raises(ValueError):
                    await backend.download_sandbox_file(submission.operation_id, link)
                assert len(requests) == 2
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
                return httpx.Response(200, json={
                    'download_url': CONTENT_URL, 'file_name': 'report.csv',
                    'file_size_bytes': 2, 'mime_type': 'text/csv', 'status': 'ready'})
            return httpx.Response(200, content=b'abc')

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            async def factory():
                return client

            with pytest.raises(ValueError, match='size does not match'):
                backend = HTTPOnlySubchatBackend(factory, credentials(), store=store)
                await backend.download_sandbox_file(submission.operation_id, LINK)
    finally:
        ledger.close()
