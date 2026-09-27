"""An interrupted Library upload never becomes an implicit second dispatch."""
import json
import sqlite3
import time

import pytest

from anywhere_computer import subchat_upload_state as upload_state_module
from anywhere_computer.subchat_upload_state import LibraryUploadLedger

OPERATION = 'a' * 32
FILE_ID = 'file_fixture_123'
SHA = 'b' * 64


def _item(*, file_id=FILE_ID, name='fixture.txt', size=44, state='ready',
          library_id='libfile_fixture'):
    return {'file_id': file_id, 'file_name': name, 'file_size_bytes': size,
            'state': state, 'library_item_id': library_id}


def _reserve(store):
    return store.reserve(OPERATION, owner='owner', account_id='account',
                         file_name='fixture.txt', file_size=44, sha256=SHA)


def test_lost_create_response_stays_unknown_across_restart(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        saved = _reserve(store)
        assert saved.state == 'unknown' and saved.automatic_retry is False
        assert saved.file_id is None
        assert store.claim_create(OPERATION, owner='owner', account_id='account')
        assert not store.claim_create(OPERATION, owner='owner', account_id='account')
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        assert _reserve(store).file_id is None
        assert not store.claim_create(OPERATION, owner='owner', account_id='account')
        # A same-name item cannot identify an upload whose create receipt was lost.
        result = store.reconcile_search(OPERATION,
            json.dumps({'results': [_item()]}).encode(), owner='owner',
            account_id='account', observed_account_id='account')
        assert result.state == 'unknown'
        with pytest.raises(ValueError, match='different input'):
            store.reserve(OPERATION, owner='owner', account_id='account',
                          file_name='changed.txt', file_size=44, sha256=SHA)
        with pytest.raises(ValueError, match='unavailable'):
            store.get(OPERATION, owner='another', account_id='account')
        with pytest.raises(ValueError, match='unavailable'):
            store.get(OPERATION, owner='owner', account_id='other')


def test_ui_batch_claim_is_durable_and_cannot_replay(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        assert store.claim_ui_batch(OPERATION, owner='owner', account_id='account')
        assert not store.claim_ui_batch(OPERATION, owner='owner', account_id='account')
        assert not store.claim_create(OPERATION, owner='owner', account_id='account')
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        saved = store.get(OPERATION, owner='owner', account_id='account')
        assert saved.create_claimed and saved.put_claimed and saved.process_claimed
        assert saved.file_id is None and saved.state == 'unknown'
        assert not store.claim_ui_batch(OPERATION, owner='owner', account_id='account')
        store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
        assert not store.claim_put(OPERATION, owner='owner', account_id='account')
        with pytest.raises(ValueError, match='unconfirmed'):
            store.claim_process(OPERATION, owner='owner', account_id='account')
        assert store.confirm_put(OPERATION, owner='owner', account_id='account').put_confirmed
        assert not store.claim_process(OPERATION, owner='owner', account_id='account')
        assert store.confirm_process(OPERATION, owner='owner',
                                     account_id='account').process_confirmed


def test_mcp_preparation_is_distinct_from_reservation_and_expires(tmp_path, monkeypatch):
    source_path = str(tmp_path / 'approved' / 'fixture.txt')
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        with pytest.raises(ValueError, match='preparation'):
            store.claim_ui_batch(OPERATION, owner='owner', account_id='account',
                                 require_prepared=True)
        prepared = store.authorize_mcp_upload(
            OPERATION, owner='owner', account_id='account', source_path=source_path)
        assert prepared.mcp_prepared_until is not None
        assert prepared.mcp_prepared_until > time.time()
        with monkeypatch.context() as later_clock:
            later_clock.setattr(upload_state_module.time, 'time',
                                lambda: prepared.mcp_prepared_until + 1)
            with pytest.raises(ValueError, match='expired'):
                store.claim_ui_batch(OPERATION, owner='owner', account_id='account',
                                     require_prepared=True, source_path=source_path)
        assert not store.get(OPERATION, owner='owner', account_id='account').create_claimed
        assert store.claim_ui_batch(OPERATION, owner='owner', account_id='account',
                                    require_prepared=True, source_path=source_path)
        with pytest.raises(ValueError, match='already started'):
            store.authorize_mcp_upload(OPERATION, owner='owner', account_id='account',
                                       source_path=source_path)


def test_mcp_preparation_binds_exact_path_and_legacy_approval_expires(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    source_path = str(tmp_path / 'approved' / 'fixture.txt')
    other_path = str(tmp_path / 'other' / 'fixture.txt')
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        store.authorize_mcp_upload(OPERATION, owner='owner', account_id='account',
                                   source_path=source_path)
        with pytest.raises(ValueError, match='preparation'):
            store.claim_ui_batch(OPERATION, owner='owner', account_id='account',
                                 require_prepared=True,
                                 source_path=other_path)
        assert not store.get(OPERATION, owner='owner', account_id='account').create_claimed
        connection.execute('ALTER TABLE subchat_library_uploads DROP COLUMN mcp_source_path')
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        saved = store.get(OPERATION, owner='owner', account_id='account')
        assert saved.mcp_prepared_until is None and saved.mcp_source_path is None
        with pytest.raises(ValueError, match='preparation'):
            store.claim_ui_batch(OPERATION, owner='owner', account_id='account',
                                 require_prepared=True,
                                 source_path=source_path)
        store.authorize_mcp_upload(OPERATION, owner='owner', account_id='account',
                                   source_path=source_path)
        assert store.claim_ui_batch(OPERATION, owner='owner', account_id='account',
                                    require_prepared=True,
                                    source_path=source_path)


def test_exact_ready_library_item_reconciles_without_dispatch(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        assert store.claim_create(OPERATION, owner='owner', account_id='account')
        assert store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner',
                                        account_id='account').state == 'unknown'
        assert store.reconcile_search(OPERATION,
            json.dumps({'results': [_item(state='processing')]}).encode(),
            owner='owner', account_id='account',
            observed_account_id='account').state == 'unknown'
        assert store.reconcile_search(OPERATION,
            json.dumps({'results': [_item(file_id='file_other')]}).encode(),
            owner='owner', account_id='account',
            observed_account_id='account').state == 'unknown'
        with pytest.raises(ValueError, match='account changed'):
            store.reconcile_search(OPERATION, b'{}', owner='owner',
                account_id='account', observed_account_id='other')
        result = store.reconcile_search(OPERATION,
            json.dumps({'results': [_item()]}).encode(), owner='owner',
            account_id='account', observed_account_id='account')
        assert (result.state, result.library_item_id, result.automatic_retry) == (
            'ready', 'libfile_fixture', False)
        assert store.reconcile_search(OPERATION,
            json.dumps({'results': [_item()]}).encode(), owner='owner',
            account_id='account', observed_account_id='account') == result


def test_observed_library_item_uses_id_field_and_rejects_conflicts(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        assert store.claim_create(OPERATION, owner='owner', account_id='account')
        store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
        item = _item()
        item['id'] = item.pop('library_item_id')
        ready = store.reconcile_search(OPERATION,
            json.dumps({'items': [item]}).encode(), owner='owner',
            account_id='account', observed_account_id='account')
        assert ready.state == 'ready' and ready.library_item_id == 'libfile_fixture'
        item['library_item_id'] = 'libfile_different'
        with pytest.raises(ValueError, match='identity'):
            store.reconcile_search(OPERATION,
                json.dumps({'items': [item]}).encode(), owner='owner',
                account_id='account', observed_account_id='account')


def test_put_and_processing_claims_survive_restart_without_replay(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        assert store.claim_create(OPERATION, owner='owner', account_id='account')
        with pytest.raises(ValueError, match='identity'):
            store.claim_put(OPERATION, owner='owner', account_id='account')
        store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
        with pytest.raises(ValueError, match='unconfirmed'):
            store.claim_process(OPERATION, owner='owner', account_id='account')
        assert store.claim_put(OPERATION, owner='owner', account_id='account')
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        assert not store.claim_put(OPERATION, owner='owner', account_id='account')
        assert not store.get(OPERATION, owner='owner', account_id='account').put_confirmed
        with pytest.raises(ValueError, match='unconfirmed'):
            store.claim_process(OPERATION, owner='owner', account_id='account')
        assert store.confirm_put(OPERATION, owner='owner', account_id='account').put_confirmed
        assert store.claim_process(OPERATION, owner='owner', account_id='account')
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        assert not store.claim_process(OPERATION, owner='owner', account_id='account')
        assert not store.get(OPERATION, owner='owner', account_id='account').process_confirmed
        assert store.confirm_process(OPERATION, owner='owner',
                                     account_id='account').process_confirmed
        assert store.get(OPERATION, owner='owner', account_id='account').state == 'unknown'
        ready = store.reconcile_search(OPERATION,
            json.dumps({'results': [_item()]}).encode(), owner='owner',
            account_id='account', observed_account_id='account')
        assert ready.state == 'ready'
        assert not store.claim_put(OPERATION, owner='owner', account_id='account')
        assert not store.claim_process(OPERATION, owner='owner', account_id='account')


@pytest.mark.parametrize('item', [
    _item(name='other.txt'), _item(size=45), _item(size=True),
    _item(library_id=''), _item(library_id='file_wrong_kind'),
])
def test_matching_file_id_with_changed_metadata_fails_closed(tmp_path, item):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        assert store.claim_create(OPERATION, owner='owner', account_id='account')
        store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
        with pytest.raises(ValueError):
            store.reconcile_search(OPERATION,
                json.dumps({'results': [item]}).encode(), owner='owner',
                account_id='account', observed_account_id='account')
        assert store.get(OPERATION, owner='owner', account_id='account').state == 'unknown'


def test_conflicting_file_id_and_ambiguous_search_are_refused(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        store = LibraryUploadLedger(connection)
        _reserve(store)
        assert store.claim_create(OPERATION, owner='owner', account_id='account')
        store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
        with pytest.raises(ValueError, match='identity changed'):
            store.checkpoint_file_id(OPERATION, 'file_other', owner='owner',
                                     account_id='account')
        with pytest.raises(ValueError, match='ambiguous'):
            store.reconcile_search(OPERATION,
                json.dumps({'results': [_item(), _item()]}).encode(),
                owner='owner', account_id='account', observed_account_id='account')
        with pytest.raises(ValueError, match='too large'):
            store.reconcile_search(OPERATION, b' ' * 1_048_577,
                owner='owner', account_id='account', observed_account_id='account')


def test_create_claim_is_atomic_across_connections_and_bound_to_owner(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with sqlite3.connect(database) as first, sqlite3.connect(database) as second:
        first_store = LibraryUploadLedger(first)
        second_store = LibraryUploadLedger(second)
        _reserve(first_store)
        with pytest.raises(ValueError, match='unavailable'):
            second_store.claim_create(OPERATION, owner='other', account_id='account')
        with pytest.raises(ValueError, match='not claimed'):
            first_store.checkpoint_file_id(OPERATION, FILE_ID,
                                           owner='owner', account_id='account')
        assert first_store.claim_create(OPERATION, owner='owner', account_id='account')
        assert not second_store.claim_create(OPERATION, owner='owner', account_id='account')
        first_store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner',
                                       account_id='account')
        assert first_store.claim_put(OPERATION, owner='owner', account_id='account')
        assert not second_store.claim_put(OPERATION, owner='owner', account_id='account')
        first_store.confirm_put(OPERATION, owner='owner', account_id='account')
        assert second_store.claim_process(OPERATION, owner='owner', account_id='account')
        assert not first_store.claim_process(OPERATION, owner='owner', account_id='account')


def test_legacy_reservation_migration_fails_closed(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE subchat_library_uploads ('
            'operation_id TEXT PRIMARY KEY, owner TEXT, account_id TEXT NOT NULL, '
            'file_name TEXT NOT NULL, file_size INTEGER NOT NULL, sha256 TEXT NOT NULL, '
            'file_id TEXT, library_item_id TEXT, state TEXT NOT NULL)'
        )
        connection.execute('INSERT INTO subchat_library_uploads VALUES (?,?,?,?,?,?,?,?,?)',
                           (OPERATION, 'owner', 'account', 'fixture.txt', 44, SHA,
                            None, None, 'unknown'))
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        assert store.get(OPERATION, owner='owner', account_id='account').create_claimed
        assert not store.claim_create(OPERATION, owner='owner', account_id='account')
        store.checkpoint_file_id(OPERATION, FILE_ID, owner='owner', account_id='account')
        assert not store.claim_put(OPERATION, owner='owner', account_id='account')
        with pytest.raises(ValueError, match='unconfirmed'):
            store.claim_process(OPERATION, owner='owner', account_id='account')


def test_previous_create_checkpoint_migrates_network_stages_as_uncertain(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE subchat_library_uploads ('
            'operation_id TEXT PRIMARY KEY, owner TEXT, account_id TEXT NOT NULL, '
            'file_name TEXT NOT NULL, file_size INTEGER NOT NULL, sha256 TEXT NOT NULL, '
            'file_id TEXT, library_item_id TEXT, state TEXT NOT NULL, '
            'create_claimed INTEGER NOT NULL DEFAULT 0)'
        )
        connection.execute('INSERT INTO subchat_library_uploads VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (OPERATION, 'owner', 'account', 'fixture.txt', 44, SHA,
                            FILE_ID, None, 'unknown', 1))
    with sqlite3.connect(database) as connection:
        store = LibraryUploadLedger(connection)
        saved = store.get(OPERATION, owner='owner', account_id='account')
        assert saved.put_claimed and saved.process_claimed
        assert not saved.put_confirmed and not saved.process_confirmed
        assert not store.claim_put(OPERATION, owner='owner', account_id='account')
        with pytest.raises(ValueError, match='unconfirmed'):
            store.claim_process(OPERATION, owner='owner', account_id='account')
