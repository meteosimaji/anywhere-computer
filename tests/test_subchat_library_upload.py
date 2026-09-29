"""The Library UI batch is one-shot and receipts are stage-specific."""

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys

import httpx
import pytest

from anywhere_computer.subchat_http_session import ObservedHTTPSession
from anywhere_computer.subchat_library_upload import (
    UploadPreflightError,
    _completed_process_stream,
    _observe_ready_after_upload,
    _read_source,
    _status_in_session,
    _upload_on_page,
    main,
    prepare_local_upload,
    upload_local_file,
)
from anywhere_computer.subchat_upload_state import LibraryUploadLedger

OPERATION = 'a' * 32
FILE_ID = 'file_fixture'
UPLOAD_URL = 'https://upload.example.test/object'


async def test_library_mcp_annotations_describe_upload_and_saved_reconciliation():
    from anywhere_computer.subchat_library_mcp import _catalog

    catalog = {tool['name']: tool['annotations'] for tool in await _catalog()}
    assert catalog['subchat_upload_library'] == {
        'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': True}
    assert catalog['subchat_upload_library_batch'] == catalog['subchat_upload_library']
    assert catalog['subchat_upload_status'] == {
        'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS-only Library upload')
async def test_mcp_upload_requires_prepared_exact_file(tmp_path, monkeypatch):
    from anywhere_computer import subchat_plugin

    state = tmp_path / 'state'
    source = tmp_path / 'approved.txt'
    source.write_text('approved bytes', encoding='utf-8')
    monkeypatch.setattr(subchat_plugin, 'plugin_paths',
                        lambda: (tmp_path / 'profile', state))
    monkeypatch.setattr(subchat_plugin, 'selected_chrome_login',
                        lambda _state: (tmp_path / 'source-profile', 'account-id'))
    with pytest.raises(UploadPreflightError, match='Prepare this exact file'):
        await upload_local_file(source, operation_id=OPERATION, require_prepared=True)
    with sqlite3.connect(state / 'library-uploads.sqlite3') as connection:
        LibraryUploadLedger(connection).reserve(
            OPERATION, owner=None, account_id='account-id', file_name='approved.txt',
            file_size=len(b'approved bytes'),
            sha256=hashlib.sha256(b'approved bytes').hexdigest())
    with pytest.raises(UploadPreflightError, match='preparation is unavailable'):
        await upload_local_file(source, operation_id=OPERATION, require_prepared=True)
    saved = prepare_local_upload(source, operation_id=OPERATION)
    assert saved.sha256 == hashlib.sha256(b'approved bytes').hexdigest()
    assert not saved.create_claimed
    other = tmp_path / 'other' / source.name
    other.parent.mkdir()
    other.write_bytes(source.read_bytes())
    with pytest.raises(UploadPreflightError, match='file changed'):
        await upload_local_file(other, operation_id=OPERATION, require_prepared=True)
    source.write_text('modified bytes', encoding='utf-8')
    with pytest.raises(UploadPreflightError, match='file changed'):
        await upload_local_file(source, operation_id=OPERATION, require_prepared=True)
    with sqlite3.connect(state / 'library-uploads.sqlite3') as connection:
        assert LibraryUploadLedger(connection).get(
            OPERATION, owner=None, account_id='account-id').create_claimed is False


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS-only Library upload')
@pytest.mark.parametrize('ready', [False, True])
async def test_started_upload_is_recovered_before_source_or_approval_checks(
        tmp_path, monkeypatch, ready):
    from anywhere_computer import subchat_library_upload, subchat_plugin

    state = tmp_path / 'state'
    state.mkdir()
    source = tmp_path / 'approved.txt'
    source.write_bytes(b'approved bytes')
    monkeypatch.setattr(subchat_plugin, 'plugin_paths',
                        lambda: (tmp_path / 'profile', state))
    monkeypatch.setattr(subchat_plugin, 'selected_chrome_login',
                        lambda _state: (tmp_path / 'source-profile', 'account-id'))
    with sqlite3.connect(state / 'library-uploads.sqlite3') as connection:
        ledger = LibraryUploadLedger(connection)
        ledger.reserve(OPERATION, owner=None, account_id='account-id',
                       file_name=source.name, file_size=len(b'approved bytes'),
                       sha256=hashlib.sha256(b'approved bytes').hexdigest())
        ledger.authorize_mcp_upload(OPERATION, owner=None, account_id='account-id',
                                    source_path=str(source))
        assert ledger.claim_ui_batch(OPERATION, owner=None, account_id='account-id',
                                     require_prepared=True, source_path=str(source))
        if ready:
            ledger.checkpoint_file_id(OPERATION, FILE_ID, owner=None,
                                      account_id='account-id')
            ledger.reconcile_search(OPERATION, json.dumps({'items': [{
                'file_id': FILE_ID, 'file_name': source.name,
                'file_size_bytes': len(b'approved bytes'), 'state': 'ready',
                'id': 'libfile_fixture'}]}).encode(), owner=None,
                account_id='account-id', observed_account_id='account-id')
        connection.execute('UPDATE subchat_library_uploads SET mcp_prepared_until=0 '
                           'WHERE operation_id=?', (OPERATION,))
    source.unlink()
    monkeypatch.setattr(subchat_library_upload, '_read_source',
                        lambda _path: pytest.fail('Started upload reread its source'))
    saved = await upload_local_file(tmp_path / 'different' / source.name,
                                    operation_id=OPERATION, require_prepared=True)
    assert saved.create_claimed and saved.state == ('ready' if ready else 'unknown')
    monkeypatch.setattr(subchat_plugin, 'selected_chrome_login',
                        lambda _state: (tmp_path / 'source-profile', 'other-account'))
    with pytest.raises(ValueError, match='unavailable'):
        await upload_local_file(source, operation_id=OPERATION, require_prepared=True)


def test_upload_help_does_not_require_browser_extra():
    script = '''import builtins
original_import = builtins.__import__
def without_playwright(name, *args, **kwargs):
    if name == "playwright" or name.startswith("playwright."):
        raise ModuleNotFoundError("No module named 'playwright'", name="playwright")
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_playwright
from anywhere_computer.subchat_library_upload import main
main(["--help"])
'''
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True,
                               text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert '--status OPERATION_ID' in completed.stdout


class FakeRequest:
    def __init__(self, method, payload=None):
        self.method = method
        self.post_data_json = payload


class FakeResponse:
    def __init__(self, url, method, status, payload=None, body=b'', headers=None):
        self.url = url
        self.request = FakeRequest(method, payload)
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def body(self):
        return self._body


class FakeInput:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return self.page.input_count

    @property
    def first(self):
        return self

    async def wait_for(self, *, state, timeout):
        assert state == 'attached' and timeout == 15_000

    async def set_input_files(self, value):
        self.page.sent.append(value)
        for response in self.page.responses:
            for handler in list(self.page.handlers):
                handler(response)


class FakePage:
    def __init__(self, responses, *, input_count=1):
        self.url = 'https://chatgpt.com/library'
        self.responses = responses
        self.input_count = input_count
        self.sent = []
        self.handlers = []

    async def goto(self, url, **_kwargs):
        assert url == self.url
        return FakeResponse(url, 'GET', 200)

    def locator(self, selector):
        assert selector == 'input[type="file"]'
        return FakeInput(self)

    def on(self, event, handler):
        assert event == 'response'
        self.handlers.append(handler)

    def remove_listener(self, event, handler):
        assert event == 'response'
        self.handlers.remove(handler)


def _responses(*, put_url=UPLOAD_URL, process_id=FILE_ID):
    return [
        FakeResponse('https://chatgpt.com/backend-api/files', 'POST', 200,
                     {'file_name': 'fixture.txt', 'file_size': 4,
                      'store_in_library': True, 'library_persistence_mode': 'required'},
                     b'{"file_id":"file_fixture","upload_url":"' +
                     UPLOAD_URL.encode() + b'"}'),
        FakeResponse(put_url, 'PUT', 201),
        FakeResponse('https://chatgpt.com/backend-api/files/process_upload_stream',
                     'POST', 200, {'file_id': process_id},
                     body=(json.dumps({'file_id': process_id,
                                       'event': 'file.processing.completed',
                                       'progress': 100.0}) + '\n').encode(),
                     headers={'content-type': 'text/event-stream'}),
    ]


def _ledger(connection):
    ledger = LibraryUploadLedger(connection)
    ledger.reserve(OPERATION, owner=None, account_id='account', file_name='fixture.txt',
                   file_size=4, sha256=hashlib.sha256(b'test').hexdigest())
    return ledger


async def test_one_browser_batch_records_receipts_and_refuses_replay(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        page = FakePage(_responses())
        result = await _upload_on_page(page, ledger, OPERATION, owner=None,
                                       account_id='account', name='fixture.txt', payload=b'test')
        assert result.file_id == FILE_ID
        assert result.put_confirmed and result.process_confirmed
        assert result.state == 'unknown'
        assert page.sent == [{'name': 'fixture.txt', 'mimeType': 'text/plain', 'buffer': b'test'}]
        again = await _upload_on_page(page, ledger, OPERATION, owner=None,
                                      account_id='account', name='fixture.txt', payload=b'test')
        assert again == result and len(page.sent) == 1


async def test_unrelated_same_page_responses_do_not_capture_upload_receipts(tmp_path):
    responses = _responses()
    responses.insert(0, FakeResponse(
        'https://chatgpt.com/backend-api/files', 'POST', 200,
        {'file_name': 'other.txt', 'file_size': 4,
         'store_in_library': True, 'library_persistence_mode': 'required'},
        b'{"file_id":"file_other","upload_url":"https://other.example.test/put"}'))
    responses.insert(2, FakeResponse('https://other.example.test/put', 'PUT', 201))
    responses.insert(3, FakeResponse(
        'https://chatgpt.com/backend-api/files/process_upload_stream',
        'POST', 200, {'file_id': 'file_other'}))
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        result = await _upload_on_page(FakePage(responses), ledger, OPERATION,
                                       owner=None, account_id='account',
                                       name='fixture.txt', payload=b'test')
        assert result.file_id == FILE_ID
        assert result.put_confirmed and result.process_confirmed


@pytest.mark.parametrize('responses,receipt', [
    (_responses(put_url='https://wrong.example.test/object'), 'put'),
    (_responses(process_id='file_wrong'), 'process'),
])
async def test_wrong_stage_identity_never_confirms_process(tmp_path, responses, receipt):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                _upload_on_page(FakePage(responses), ledger, OPERATION,
                                owner=None, account_id='account', name='fixture.txt',
                                payload=b'test'), timeout=0.01)
        saved = ledger.get(OPERATION, owner=None, account_id='account')
        assert saved.create_claimed and saved.process_claimed
        assert not saved.process_confirmed
        assert saved.put_confirmed == (receipt == 'process')


async def test_http_200_without_terminal_event_does_not_confirm_processing(tmp_path):
    responses = _responses()
    responses[-1]._body = (json.dumps({'file_id': FILE_ID,
                                       'event': 'file.processing.started',
                                       'progress': 0.0}) + '\n').encode()
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        with pytest.raises(ValueError, match='no completed event'):
            await _upload_on_page(FakePage(responses), ledger, OPERATION,
                                  owner=None, account_id='account', name='fixture.txt',
                                  payload=b'test')
        saved = ledger.get(OPERATION, owner=None, account_id='account')
        assert saved.put_confirmed and not saved.process_confirmed
        assert not ledger.claim_ui_batch(OPERATION, owner=None, account_id='account')


async def test_ambiguous_input_does_not_claim(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        with pytest.raises(ValueError, match='ambiguous'):
            await _upload_on_page(FakePage(_responses(), input_count=2), ledger,
                                  OPERATION, owner=None, account_id='account',
                                  name='fixture.txt', payload=b'test')
        assert not ledger.get(OPERATION, owner=None, account_id='account').create_claimed


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX descriptor confinement')
def test_source_bytes_are_pinned_and_symlink_is_rejected(tmp_path):
    source = tmp_path / 'fixture.txt'
    source.write_bytes(b'test')
    assert _read_source(source) == ('fixture.txt', b'test', hashlib.sha256(b'test').hexdigest())
    alias = tmp_path / 'alias.txt'
    alias.symlink_to(source)
    with pytest.raises(ValueError, match='redirected'):
        _read_source(alias)
    directory = tmp_path / 'directory'
    directory.mkdir()
    (directory / 'payload.txt').write_bytes(b'test')
    parent_alias = tmp_path / 'parent-alias'
    parent_alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match='redirected'):
        _read_source(parent_alias / 'payload.txt')


def test_processing_stream_requires_matching_terminal_event():
    def line(event, *, file_id=FILE_ID, progress=0.0):
        return (json.dumps({'file_id': file_id, 'event': event,
                            'progress': progress}) + '\n').encode()

    assert _completed_process_stream(
        line('file.processing.started')
        + line('file.processing.file_ready', progress=20.0)
        + line('file.indexing.completed')
        + line('file.processing.completed', progress=100.0), FILE_ID)
    assert not _completed_process_stream(line('file.processing.started'), FILE_ID)
    with pytest.raises(ValueError, match='identity'):
        _completed_process_stream(line('file.processing.completed',
                                       file_id='file_other', progress=100.0), FILE_ID)
    with pytest.raises(ValueError, match='incomplete'):
        _completed_process_stream(line('file.processing.completed', progress=20.0), FILE_ID)
    with pytest.raises(ValueError, match='followed completion'):
        _completed_process_stream(line('file.processing.completed', progress=100.0)
                                  + line('file.processing.failed'), FILE_ID)


def _session(account='account'):
    return ObservedHTTPSession.model_validate({
        'authorization': 'Bearer fixturetoken', 'account_id': account,
        'catalog_url': 'https://chatgpt.com/backend-api/models',
    })


async def test_initial_upload_waits_briefly_for_exact_library_item(tmp_path, monkeypatch):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        assert ledger.claim_create(OPERATION, owner=None, account_id='account')
        ledger.checkpoint_file_id(OPERATION, FILE_ID, owner=None, account_id='account')
        searches = []
        delays = []

        def respond(request):
            searches.append(request)
            item = ({'file_id': FILE_ID, 'file_name': 'fixture.txt',
                     'file_size_bytes': 4, 'state': 'ready', 'id': 'libfile_fixture'}
                    if len(searches) == 2 else None)
            return httpx.Response(200, json={'items': [item] if item else [],
                                             'cursor': None})

        async def sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr('anywhere_computer.subchat_library_upload.asyncio.sleep', sleep)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            ready = await _observe_ready_after_upload(
                client, _session(), ledger, OPERATION, owner=None,
                account_id='account')
        assert ready.state == 'ready' and ready.library_item_id == 'libfile_fixture'
        assert len(searches) == 2 and delays == [2]
        assert all(request.method == 'POST' and request.url.path.endswith('/library')
                   for request in searches)


async def test_status_without_source_reconciles_only_known_file_id(tmp_path):
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as connection:
        ledger = _ledger(connection)
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'items': [{
                'file_id': FILE_ID, 'file_name': 'fixture.txt',
                'file_size_bytes': 4, 'state': 'ready', 'id': 'libfile_fixture',
            }], 'cursor': None})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            unknown = await _status_in_session(client, _session(), ledger, OPERATION,
                                               owner=None, account_id='account')
            assert unknown.file_id is None and requests == []
            assert ledger.claim_create(OPERATION, owner=None, account_id='account')
            ledger.checkpoint_file_id(OPERATION, FILE_ID, owner=None,
                                      account_id='account')
            ready = await _status_in_session(client, _session(), ledger, OPERATION,
                                             owner=None, account_id='account')
            assert ready.state == 'ready' and ready.library_item_id == 'libfile_fixture'
            assert len(requests) == 1
            assert json.loads(requests[0].content)['source'] == 'uploaded'
            await _status_in_session(client, _session(), ledger, OPERATION,
                                     owner=None, account_id='account')
            assert len(requests) == 1
            with pytest.raises(ValueError, match='account changed'):
                await _status_in_session(client, _session('other'), ledger,
                                         OPERATION, owner=None, account_id='account')


def test_status_cli_requires_no_file(monkeypatch, capsys):
    async def status(operation_id):
        assert operation_id == OPERATION
        return type('Result', (), {'operation_id': OPERATION, 'file_id': None,
                                   'library_item_id': None, 'state': 'unknown',
                                   'automatic_retry': False})()

    monkeypatch.setattr('anywhere_computer.subchat_library_upload.status_local_upload', status)
    main(['--status', OPERATION])
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[-1]) == {
        'operation_id': OPERATION, 'file_id': None, 'library_item_id': None,
        'state': 'unknown', 'automatic_retry': False,
    }


def test_status_cli_network_timeout_keeps_same_id_unverified(monkeypatch, capsys):
    async def timeout(operation_id):
        assert operation_id == OPERATION
        raise httpx.ReadTimeout('provider read timed out')

    monkeypatch.setattr('anywhere_computer.subchat_library_upload.status_local_upload',
                        timeout)
    with pytest.raises(SystemExit) as stopped:
        main(['--status', OPERATION])
    assert stopped.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0]) == {'operation_id': OPERATION}
    assert json.loads(lines[-1]) == {
        'operation_id': OPERATION, 'outcome': 'unverified',
        'automatic_retry': False,
        'next_action': 'Inspect this same operation ID later',
    }
