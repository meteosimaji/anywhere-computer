"""Library recovery pages only through one account-pinned, bounded read path."""

import json
import sqlite3

import httpx
import pytest

from anywhere_computer.subchat_http_library import reconcile_library_upload
from anywhere_computer.subchat_http_session import ObservedHTTPSession
from anywhere_computer.subchat_state import SubchatAccountMismatch
from anywhere_computer.subchat_upload_state import LibraryUploadLedger

OPERATION = 'a' * 32
FILE_ID = 'file_fixture_123'


def _session(account='account'):
    return ObservedHTTPSession.model_validate({
        'authorization': 'Bearer fixturetoken', 'account_id': account,
        'catalog_url': 'https://chatgpt.com/backend-api/models',
    })


def _ledger(connection, *, checkpoint=True):
    ledger = LibraryUploadLedger(connection)
    ledger.reserve(OPERATION, owner='owner', account_id='account',
                   file_name='fixture.txt', file_size=44, sha256='b' * 64)
    if checkpoint:
        assert ledger.claim_create(OPERATION, owner='owner', account_id='account')
        ledger.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
    return ledger


async def test_exact_file_recovery_walks_cursor_without_upload(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        cursors = []

        def respond(request):
            assert request.url == 'https://chatgpt.com/backend-api/files/library'
            assert request.method == 'POST'
            assert request.headers['chatgpt-account-id'] == 'account'
            payload = json.loads(request.content)
            assert payload['source'] == 'uploaded'
            assert payload['ranking'] == 'suggested'
            cursors.append(payload['cursor'])
            if payload['cursor'] is None:
                return httpx.Response(200, json={'items': [
                    {'file_id': 'file_other', 'state': 'ready'}], 'cursor': 'next'})
            return httpx.Response(200, json={'items': [{
                'file_id': FILE_ID, 'file_name': 'fixture.txt',
                'file_size_bytes': 44, 'state': 'ready', 'id': 'libfile_fixture',
            }], 'cursor': None})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await reconcile_library_upload(
                client, _session(), ledger, OPERATION, owner='owner',
                account_id='account')
            assert result.state == 'ready'
            assert result.library_item_id == 'libfile_fixture'
            assert cursors == [None, 'next']
            await reconcile_library_upload(client, _session(), ledger, OPERATION,
                                           owner='owner', account_id='account')
            assert cursors == [None, 'next']


async def test_missing_file_id_and_account_mismatch_make_no_request(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection, checkpoint=False)

        def reject(_request):
            raise AssertionError('No Library request expected')

        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            result = await reconcile_library_upload(
                client, _session(), ledger, OPERATION, owner='owner',
                account_id='account')
            assert result.state == 'unknown'
            with pytest.raises(SubchatAccountMismatch):
                await reconcile_library_upload(client, _session('other'), ledger,
                                               OPERATION, owner='owner',
                                               account_id='account')
            with pytest.raises(ValueError, match='unavailable'):
                await reconcile_library_upload(client, _session(), ledger,
                                               OPERATION, owner='another',
                                               account_id='account')


@pytest.mark.parametrize('response,expected', [
    (httpx.Response(302, headers={'location': 'https://other.example/'}),
     'did not succeed'),
    (httpx.Response(200, text='not json'), 'Unexpected Library'),
    (httpx.Response(200, json={'items': [], 'cursor': 42}), 'Invalid Library'),
    (httpx.Response(200, json={'items': [], 'cursor': 'same'}), 'cursor repeated'),
])
async def test_library_recovery_rejects_bad_transport_or_cursor(tmp_path,
                                                                 response, expected):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: response)) as client:
            with pytest.raises((ConnectionError, ValueError), match=expected):
                await reconcile_library_upload(client, _session(), ledger,
                                               OPERATION, owner='owner',
                                               account_id='account')
