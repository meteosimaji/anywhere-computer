"""Multiple prepared files share setup but retain exact, independent upload receipts."""

import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from anywhere_computer import subchat_library_batch as batches
from anywhere_computer import subchat_library_mcp, subchat_plugin
from anywhere_computer.models import Request
from anywhere_computer.subchat_library_upload import UploadPreflightError
from anywhere_computer.subchat_upload_state import LibraryUploadLedger


@pytest.fixture
def batch_environment(tmp_path, monkeypatch):
    if os.name != 'posix':
        pytest.skip('Library source confinement requires POSIX directory descriptors')
    state = tmp_path / 'state'
    state.mkdir()
    files = []
    connection = sqlite3.connect(state / 'library-uploads.sqlite3')
    ledger = LibraryUploadLedger(connection)
    for index in range(2):
        path = tmp_path / f'fixture-{index}.txt'
        payload = f'fixture {index}'.encode()
        path.write_bytes(payload)
        identity = str(index + 1) * 32
        ledger.reserve(identity, owner=None, account_id='account', file_name=path.name,
                       file_size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        ledger.authorize_mcp_upload(identity, owner=None, account_id='account',
                                    source_path=str(path))
        files.append({'operation_id': identity, 'path': str(path)})
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (tmp_path / 'profile', state))
    monkeypatch.setattr(subchat_plugin, 'selected_chrome_login',
                        lambda _state: (tmp_path / 'Default', 'account'))
    monkeypatch.setattr(batches, 'sys', SimpleNamespace(platform='darwin'))
    yield SimpleNamespace(ledger=ledger, connection=connection,
                          batch=batches.LibraryBatch.model_validate({'files': files}),
                          files=files, state=state)
    connection.close()


@pytest.fixture
def batch_transport(monkeypatch):
    from playwright import async_api

    from anywhere_computer import subchat_chrome_login, subchat_chrome_profile
    from anywhere_computer.subchat_browser import background

    observed = SimpleNamespace(sessions=0, snapshots=0, uploads=[], fail=None,
                               observed_account='account')

    @asynccontextmanager
    async def driver():
        yield object()

    @asynccontextmanager
    async def snapshot(source):
        observed.snapshots += 1
        yield source

    @asynccontextmanager
    async def context(_driver, _profile, _args):
        observed.sessions += 1
        yield object()

    class Page:
        async def close(self):
            pass

    async def page(_context):
        return Page()

    async def session(_context, _client, **_kwargs):
        return SimpleNamespace(account_id=observed.observed_account)

    async def upload(_page, ledger, identity, *, owner, account_id, name, payload,
                     require_prepared, source_path):
        assert require_prepared
        if not ledger.claim_ui_batch(identity, owner=owner, account_id=account_id,
                                      require_prepared=True, source_path=source_path):
            pytest.fail('A claimed upload reached the byte-sending boundary again')
        observed.uploads.append((identity, name, payload))
        if observed.fail == identity:
            raise TimeoutError('Response lost after dispatch')
        return ledger.checkpoint_file_id(identity, 'file_' + identity,
                                         owner=owner, account_id=account_id)

    async def ready(_client, _session, ledger, identity, *, owner, account_id):
        saved = ledger.get(identity, owner=owner, account_id=account_id)
        return ledger.reconcile_search(identity, json.dumps({'items': [{
            'file_id': saved.file_id, 'file_name': saved.file_name,
            'file_size_bytes': saved.file_size, 'state': 'ready',
            'id': 'libfile_' + identity}]}).encode(), owner=owner, account_id=account_id,
            observed_account_id=account_id)

    monkeypatch.setattr(async_api, 'async_playwright', driver)
    monkeypatch.setattr(subchat_chrome_profile, 'temporary_chrome_profile', snapshot)
    monkeypatch.setattr(background, 'background_chrome_context', context)
    monkeypatch.setattr(background, 'new_background_page', page)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', session)
    monkeypatch.setattr(batches, '_upload_on_page', upload)
    monkeypatch.setattr(batches, '_observe_ready_after_upload', ready)
    return observed


async def test_two_file_handoff_shares_session_and_returns_exact_resources(
        batch_environment, batch_transport):
    reply = await subchat_library_mcp._execute(Request(
        operation_id='a' * 32, tool='subchat_upload_library_batch',
        arguments={'files': batch_environment.files}))
    assert reply.state == 'completed'
    assert all(item['provider_receipt'] == 'confirmed' for item in reply.data['uploads'])
    assert batch_transport.sessions == batch_transport.snapshots == 1
    assert len(batch_transport.uploads) == 2
    assert reply.data['resources']['attachments'] == [
        {'id': 'file_' + item['operation_id'],
         'library_file_id': 'libfile_' + item['operation_id'],
         'name': f'fixture-{index}.txt', 'mime_type': 'text/plain', 'size': 9}
        for index, item in enumerate(batch_environment.files)]
    replay = await batches.run_local_batch(batch=batch_environment.batch)
    assert all(item.state == 'ready' for item in replay.uploads)
    assert len(batch_transport.uploads) == 2
    assert batch_transport.sessions == 1


async def test_partial_handoff_receipts_distinguish_claimed_and_unsent(
        batch_environment, batch_transport):
    batch_transport.fail = batch_environment.batch.files[0].operation_id
    reply = await subchat_library_mcp._execute(Request(
        operation_id='a' * 32, tool='subchat_upload_library_batch',
        arguments={'files': batch_environment.files}))
    assert reply.state == 'unknown'
    assert reply.data['resources'] is None
    assert [item['provider_receipt'] for item in reply.data['uploads']] == [
        'unconfirmed', 'not_sent']
    assert [item['dispatch_claimed'] for item in reply.data['uploads']] == [True, False]


async def test_all_files_checked_before_first_upload(batch_environment, batch_transport):
    from pathlib import Path

    Path(batch_environment.files[1]['path']).write_bytes(b'changed')
    with pytest.raises(UploadPreflightError, match='file changed'):
        await batches.run_local_batch(batch=batch_environment.batch)
    assert batch_transport.uploads == []
    assert batch_transport.sessions == 0
    assert all(not batch_environment.ledger.get(item.operation_id, owner=None,
                                                account_id='account').create_claimed
               for item in batch_environment.batch.files)


async def test_uncertain_first_file_stops_second_and_status_never_reads_or_uploads(
        batch_environment, batch_transport, monkeypatch):
    first, second = batch_environment.batch.files
    batch_transport.fail = first.operation_id
    saved = await batches.run_local_batch(batch=batch_environment.batch)
    assert saved.uploads[0].create_claimed and not saved.uploads[1].create_claimed
    assert saved.failed_operation_id == first.operation_id
    assert saved.error_code == 'transport_unverified'
    assert len(batch_transport.uploads) == 1
    monkeypatch.setattr(batches, '_read_source', lambda _path: pytest.fail('Status read bytes'))
    status = batches.LibraryBatchStatus(operation_ids=(first.operation_id, second.operation_id))
    recovered = await batches.run_local_batch(status=status)
    assert recovered.uploads == saved.uploads
    assert len(batch_transport.uploads) == 1
    # No provider identity exists after the lost create; status has nothing safe to search.
    assert batch_transport.sessions == 1


async def test_selected_account_change_cannot_start_batch(batch_environment, batch_transport):
    batch_transport.observed_account = 'other'
    with pytest.raises(UploadPreflightError, match='account changed'):
        await batches.run_local_batch(batch=batch_environment.batch)
    assert batch_transport.uploads == []


async def test_partial_result_refreshes_concurrent_later_dispatch(
        batch_environment, batch_transport, monkeypatch):
    first, second = batch_environment.batch.files
    original = batches._upload_on_page

    async def concurrent(*args, **kwargs):
        # A different process claims the second file while this call awaits the first.
        batch_environment.ledger.claim_ui_batch(
            second.operation_id, owner=None, account_id='account',
            require_prepared=True, source_path=second.path)
        return await original(*args, **kwargs)

    batch_transport.fail = first.operation_id
    monkeypatch.setattr(batches, '_upload_on_page', concurrent)
    result = await batches.run_local_batch(batch=batch_environment.batch)
    assert all(item.create_claimed for item in result.uploads)
    assert len(batch_transport.uploads) == 1


async def test_approval_rechecked_before_later_file_dispatch(
        batch_environment, batch_transport, monkeypatch):
    first, second = batch_environment.batch.files
    original = batches._observe_ready_after_upload

    async def expire(*args, **kwargs):
        with batch_environment.connection:
            batch_environment.connection.execute(
                'UPDATE subchat_library_uploads SET mcp_prepared_until=0 WHERE operation_id=?',
                (second.operation_id,))
        return await original(*args, **kwargs)

    monkeypatch.setattr(batches, '_observe_ready_after_upload', expire)
    result = await batches.run_local_batch(batch=batch_environment.batch)
    assert result.uploads[0].state == 'ready'
    assert result.uploads[1].create_claimed is False
    assert result.failed_operation_id == second.operation_id
    assert batch_transport.uploads[0][0] == first.operation_id
    assert len(batch_transport.uploads) == 1


@pytest.mark.parametrize('owner,account', [('other', 'account'), (None, 'other')])
async def test_cross_owner_or_account_batch_rejected_before_browser(
        batch_environment, batch_transport, monkeypatch, owner, account):
    monkeypatch.setattr(subchat_plugin, 'selected_chrome_login',
                        lambda _state: (batch_environment.state / 'Default', account))
    with pytest.raises(ValueError, match='unavailable'):
        await batches.run_local_batch(batch=batch_environment.batch, owner=owner)
    assert batch_transport.sessions == 0


def test_expired_preparation_and_total_limit_are_checked_before_dispatch(
        batch_environment, monkeypatch):
    env = batch_environment
    monkeypatch.setattr(batches, 'MAX_BATCH_BYTES', 10)
    with pytest.raises(UploadPreflightError, match='40 MiB'):
        batches._prepared_payloads(env.ledger, env.batch, owner=None, account_id='account')
    monkeypatch.setattr(batches, 'MAX_BATCH_BYTES', 40 * 1024 * 1024)
    with env.connection:
        env.connection.execute('UPDATE subchat_library_uploads SET mcp_prepared_until=0')
    with pytest.raises(UploadPreflightError, match='expired'):
        batches._prepared_payloads(env.ledger, env.batch, owner=None, account_id='account')


@pytest.mark.parametrize('files', [[], [{'operation_id': 'a' * 32, 'path': 'relative'}],
    [{'operation_id': 'a' * 32, 'path': '/one'}, {'operation_id': 'a' * 32, 'path': '/two'}],
    [{'operation_id': 'a' * 32, 'path': '/one'}, {'operation_id': 'b' * 32, 'path': '/one'}]])
def test_batch_rejects_empty_relative_and_duplicate_intents(files):
    with pytest.raises(ValidationError):
        batches.LibraryBatch.model_validate({'files': files})


async def test_mcp_batch_requires_every_stable_file_id_before_dispatch(monkeypatch):
    async def unexpected(**_kwargs):
        pytest.fail('Invalid per-file identity reached the upload runner')

    monkeypatch.setattr(subchat_library_mcp, 'run_local_batch', unexpected)
    reply = await subchat_library_mcp._execute(Request(
        operation_id='f' * 32, tool='subchat_upload_library_batch',
        arguments={'files': [{'path': '/absolute/file.txt'}]}))
    assert reply.state == 'failed'
    assert 'Field required' in reply.error


def test_batch_preparation_cli_emits_exact_saved_ids(batch_environment, monkeypatch, capsys):
    from anywhere_computer import subchat_library_upload

    env = batch_environment
    manifest = env.state / 'manifest.json'
    manifest.write_text(json.dumps({'files': env.files}))
    monkeypatch.setattr(subchat_library_upload, 'sys', SimpleNamespace(platform='darwin'))
    subchat_library_upload.main(['--batch', str(manifest), '--prepare'])
    result = json.loads(capsys.readouterr().out)
    assert result['outcome'] == 'prepared'
    assert result['provider_dispatched'] is False
    assert [(item['operation_id'], item['path']) for item in result['files']] == [
        (item['operation_id'], item['path']) for item in env.files]
    assert all(item['prepared_until'] is not None for item in result['files'])
