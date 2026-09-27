"""Explicit local-file upload through the selected macOS ChatGPT Library UI.

Run with ``python -m anywhere_computer.subchat_library_upload FILE``. A failed or
interrupted operation is read-only on reuse of its printed operation ID.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
import stat
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from .state import prepare_directory
from .subchat_http_library import reconcile_library_upload
from .subchat_upload_state import LibraryUpload, LibraryUploadLedger

if TYPE_CHECKING:
    from playwright.async_api import Page, Response

    from .subchat_http_session import ObservedHTTPSession

_LIBRARY_HOME = 'https://chatgpt.com/library'
_CREATE_URL = 'https://chatgpt.com/backend-api/files'
_PROCESS_URL = 'https://chatgpt.com/backend-api/files/process_upload_stream'
_MAX_FILE_BYTES = 20_971_520


class UploadPreflightError(ValueError):
    """The Library upload was refused before reserving or dispatching a batch."""


def _completed_process_stream(body: bytes, file_id: str) -> bool:
    """The observed stream is newline-delimited JSON, not SSE data lines."""
    if not body or len(body) > 1_048_576:
        raise ValueError('Library processing response is empty or too large')
    completed = False
    for raw_line in body.splitlines():
        if not raw_line:
            continue
        try:
            item = json.loads(raw_line)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError('Invalid Library processing event') from error
        if not isinstance(item, dict) or item.get('file_id') != file_id:
            raise ValueError('Library processing event has a different file identity')
        event = item.get('event')
        if not isinstance(event, str) or not event.startswith('file.'):
            raise ValueError('Invalid Library processing event')
        if (completed or item.get('error') is not None
                or event.endswith(('.failed', '.error'))):
            raise ValueError('Library processing event failed or followed completion')
        if event == 'file.processing.completed':
            if item.get('progress') != 100.0:
                raise ValueError('Library processing completion is incomplete')
            completed = True
    return completed


def _read_source(path: Path) -> tuple[str, bytes, str]:
    """Pin bytes in memory before browser dispatch; reject redirected or mutable files."""
    directory_flag = getattr(os, 'O_DIRECTORY', 0)
    nofollow_flag = getattr(os, 'O_NOFOLLOW', 0)
    if not directory_flag or not nofollow_flag:
        raise ValueError('Upload source confinement is unavailable on this host')
    if not path.is_absolute():
        raise ValueError('Upload source must be an absolute regular file without a symlink')
    name = path.name
    if (not name or len(name) > 256 or name in {'.', '..'}
            or any(ord(character) < 32 for character in name)):
        raise ValueError('Upload filename is invalid')
    directory_flags = os.O_RDONLY | directory_flag | nofollow_flag
    directories: list[tuple[Path, int]] = []
    try:
        parent = Path(path.anchor)
        directories.append((parent, os.open(parent, directory_flags)))
        for component in path.parts[1:-1]:
            parent = parent / component
            directories.append((parent, os.open(
                component, directory_flags, dir_fd=directories[-1][1])))
        descriptor = os.open(path.name, os.O_RDONLY | nofollow_flag,
                             dir_fd=directories[-1][1])
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_FILE_BYTES
                    or metadata.st_nlink != 1):
                raise ValueError('Upload source must be a nonempty regular file of at most 20 MiB')
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                payload = stream.read(_MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
            if (len(payload) != metadata.st_size or len(payload) > _MAX_FILE_BYTES
                    or (metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size)
                    != (after.st_mtime_ns, after.st_ctime_ns, after.st_size)):
                raise ValueError('Upload source changed while it was read')
            for current, current_fd in directories:
                observed = os.stat(current, follow_symlinks=False)
                pinned = os.fstat(current_fd)
                if (not stat.S_ISDIR(observed.st_mode)
                        or (observed.st_dev, observed.st_ino)
                        != (pinned.st_dev, pinned.st_ino)):
                    raise ValueError('Upload source parent changed while it was read')
            observed_file = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISREG(observed_file.st_mode)
                    or (observed_file.st_dev, observed_file.st_ino)
                    != (metadata.st_dev, metadata.st_ino)):
                raise ValueError('Upload source changed while it was read')
            return name, payload, hashlib.sha256(payload).hexdigest()
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError('Upload source is unavailable or redirected') from error
    finally:
        for _, directory_fd in reversed(directories):
            os.close(directory_fd)


async def _upload_on_page(page: Page, ledger: LibraryUploadLedger,
                          operation_id: str, *, owner: str | None,
                          account_id: str, name: str, payload: bytes,
                          require_prepared: bool = False,
                          source_path: str | None = None) -> LibraryUpload:
    """Capture all browser responses before starting its automatic upload batch."""
    home = await page.goto(_LIBRARY_HOME, wait_until='domcontentloaded')
    parsed = urlsplit(page.url)
    if (home is None or home.status != 200 or parsed.scheme != 'https'
            or parsed.netloc != 'chatgpt.com' or parsed.path != '/library'):
        raise ConnectionError('ChatGPT Library did not open')
    inputs = page.locator('input[type="file"]')
    await inputs.first.wait_for(state='attached', timeout=15_000)
    if await inputs.count() != 1:
        raise ValueError('Library upload input is unavailable or ambiguous')
    mime_type, _ = mimetypes.guess_type(name)
    if mime_type is None:
        raise ValueError('Upload filename has no known MIME type')
    loop = asyncio.get_running_loop()
    create: asyncio.Future[Response] = loop.create_future()
    puts: asyncio.Queue[Response] = asyncio.Queue(maxsize=128)
    processes: asyncio.Queue[Response] = asyncio.Queue(maxsize=128)
    overflow = False

    async def matching(queue: asyncio.Queue[Response],
                       predicate: Callable[[Response], bool], timeout: float) -> Response:
        deadline = loop.time() + timeout
        while True:
            if overflow:
                raise ValueError('Too many Library upload responses to identify the batch')
            response = await asyncio.wait_for(queue.get(), timeout=max(0, deadline - loop.time()))
            if predicate(response):
                return response

    def observe(response: Response) -> None:
        nonlocal overflow
        request = response.request
        if request.method == 'POST' and response.url == _CREATE_URL:
            body = request.post_data_json
            if (isinstance(body, dict) and body.get('file_name') == name
                    and type(body.get('file_size')) is int
                    and body['file_size'] == len(payload)
                    and body.get('store_in_library') is True
                    and body.get('library_persistence_mode') == 'required'
                    and not create.done()):
                create.set_result(response)
        elif request.method == 'PUT':
            try:
                puts.put_nowait(response)
            except asyncio.QueueFull:
                overflow = True
        elif request.method == 'POST' and response.url == _PROCESS_URL:
            try:
                processes.put_nowait(response)
            except asyncio.QueueFull:
                overflow = True

    page.on('response', observe)
    try:
        try:
            claimed = ledger.claim_ui_batch(
                operation_id, owner=owner, account_id=account_id,
                require_prepared=require_prepared, source_path=source_path)
        except ValueError as error:
            if require_prepared:
                raise UploadPreflightError(str(error)) from error
            raise
        if not claimed:
            return ledger.get(operation_id, owner=owner, account_id=account_id)
        # The browser receives the pinned bytes, never a path that can be swapped.
        await inputs.set_input_files({'name': name, 'mimeType': mime_type,
                                      'buffer': payload})
        response = await asyncio.wait_for(create, timeout=30)
        if response.status != 200:
            raise ConnectionError('Library file creation was not accepted')
        body = await response.body()
        if len(body) > 131_072:
            raise ValueError('Library file creation response is too large')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError('Library file creation response is invalid')
        file_id = data.get('file_id')
        upload_url = data.get('upload_url')
        if (not isinstance(file_id, str) or not isinstance(upload_url, str)
                or not upload_url.startswith('https://') or len(upload_url) > 4096):
            raise ValueError('Library file creation response is invalid')
        ledger.checkpoint_file_id(operation_id, file_id, owner=owner,
                                  account_id=account_id)
        put_response = await matching(puts, lambda candidate: candidate.url == upload_url, 60)
        if put_response.status != 201:
            raise ConnectionError('Library byte upload was not confirmed')
        ledger.confirm_put(operation_id, owner=owner, account_id=account_id)
        process_response = await matching(
            processes, lambda candidate: (isinstance(candidate.request.post_data_json, dict)
                                          and candidate.request.post_data_json.get('file_id')
                                          == file_id), 60)
        if (process_response.status != 200 or process_response.headers.get(
                'content-type', '').split(';', 1)[0] != 'text/event-stream'):
            raise ConnectionError('Library processing was not accepted')
        process_stream = await process_response.body()
        if not _completed_process_stream(process_stream, file_id):
            raise ValueError('Library processing has no completed event')
        ledger.confirm_process(operation_id, owner=owner, account_id=account_id)
        return ledger.get(operation_id, owner=owner, account_id=account_id)
    finally:
        page.remove_listener('response', observe)


async def _observe_ready_after_upload(client: httpx.AsyncClient,
                                      session: ObservedHTTPSession,
                                      ledger: LibraryUploadLedger,
                                      operation_id: str, *, owner: str | None,
                                      account_id: str) -> LibraryUpload:
    """Allow brief Library indexing lag using read-only exact-ID searches."""
    saved = ledger.get(operation_id, owner=owner, account_id=account_id)
    for attempt in range(3):
        saved = await reconcile_library_upload(
            client, session, ledger, operation_id, owner=owner,
            account_id=account_id)
        if saved.state == 'ready' or saved.file_id is None or attempt == 2:
            return saved
        await asyncio.sleep(2)
    return saved


async def upload_local_file(path: Path, *, operation_id: str,
                            owner: str | None = None,
                            require_prepared: bool = False) -> LibraryUpload:
    """One user-invoked upload; an existing operation ID can only be inspected."""
    if sys.platform != 'darwin':
        raise UploadPreflightError('Library UI upload is currently supported on macOS only')
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as error:
        if error.name != 'playwright':
            raise
        raise UploadPreflightError('Install browser support with the browser extra') from None
    from .subchat_browser.background import background_chrome_context, new_background_page
    from .subchat_chrome_login import chrome_http_session
    from .subchat_chrome_profile import temporary_chrome_profile
    from .subchat_plugin import plugin_paths, selected_chrome_login

    try:
        source, account_id = selected_chrome_login(plugin_paths()[1])
    except ValueError as error:
        raise UploadPreflightError(str(error)) from error
    if source is None or account_id is None:
        raise UploadPreflightError('Select a Chrome login and pin its Chat account first')
    _, state = plugin_paths()
    prepare_directory(state)
    with closing(sqlite3.connect(state / 'library-uploads.sqlite3')) as connection:
        ledger = LibraryUploadLedger(connection)
        try:
            saved = ledger.get(operation_id, owner=owner, account_id=account_id)
        except ValueError:
            if connection.execute(
                    'SELECT 1 FROM subchat_library_uploads WHERE operation_id=?',
                    (operation_id,)).fetchone() is not None:
                raise
            saved = None
        if require_prepared and saved is None:
            raise UploadPreflightError(
                'Prepare this exact file locally before an MCP upload')
        if saved is not None and (saved.create_claimed or saved.state == 'ready'):
            if saved.file_id is None or saved.state == 'ready':
                return saved
            # A claimed upload can only be reconciled, regardless of the
            # source path, source bytes, or expired preparation.
        else:
            try:
                name, payload, digest = _read_source(path)
            except ValueError as error:
                raise UploadPreflightError(str(error)) from error
        if require_prepared:
            assert saved is not None
            if not saved.create_claimed and saved.state != 'ready':
                if (saved.mcp_prepared_until is None
                        or saved.mcp_prepared_until <= time.time()):
                    raise UploadPreflightError('Library MCP preparation is unavailable or expired')
                if (saved.file_name, saved.file_size, saved.sha256,
                        saved.mcp_source_path) != (
                        name, len(payload), digest, str(path)):
                    raise UploadPreflightError('Prepared Library upload file changed')
        else:
            if saved is None or not saved.create_claimed:
                saved = ledger.reserve(operation_id, owner=owner, account_id=account_id,
                                       file_name=name, file_size=len(payload), sha256=digest)
        async with async_playwright() as driver:
            async with temporary_chrome_profile(source) as snapshot:
                async with background_chrome_context(
                        driver, snapshot, [f'--profile-directory={source.name}']) as context:
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
                        session = await chrome_http_session(
                            context, client, expected_account_id=account_id,
                            page_factory=lambda: new_background_page(context))
                        if not saved.create_claimed:
                            page = await new_background_page(context)
                            try:
                                saved = await _upload_on_page(
                                    page, ledger, operation_id, owner=owner,
                                    account_id=account_id, name=name, payload=payload,
                                    require_prepared=require_prepared,
                                    source_path=str(path))
                            finally:
                                await page.close()
                        if saved.file_id is not None:
                            saved = await _observe_ready_after_upload(
                                client, session, ledger, operation_id,
                                owner=owner, account_id=account_id)
                        return saved


def prepare_local_upload(path: Path, *, operation_id: str,
                         owner: str | None = None) -> LibraryUpload:
    """Local owner action: bind one operation ID to exact bytes without uploading."""
    if sys.platform != 'darwin':
        raise UploadPreflightError('Library UI upload is currently supported on macOS only')
    from .subchat_plugin import plugin_paths, selected_chrome_login

    _, state = plugin_paths()
    source, account_id = selected_chrome_login(state)
    if source is None or account_id is None:
        raise UploadPreflightError('Select a Chrome login and pin its Chat account first')
    try:
        name, payload, digest = _read_source(path)
    except ValueError as error:
        raise UploadPreflightError(str(error)) from error
    prepare_directory(state)
    with closing(sqlite3.connect(state / 'library-uploads.sqlite3')) as connection:
        ledger = LibraryUploadLedger(connection)
        ledger.reserve(operation_id, owner=owner, account_id=account_id,
                       file_name=name, file_size=len(payload), sha256=digest)
        return ledger.authorize_mcp_upload(operation_id, owner=owner,
                                           account_id=account_id,
                                           source_path=str(path))


async def _status_in_session(client: httpx.AsyncClient, session: ObservedHTTPSession,
                             ledger: LibraryUploadLedger, operation_id: str, *,
                             owner: str | None, account_id: str) -> LibraryUpload:
    if session.account_id != account_id:
        raise ValueError('Selected Chat account changed')
    saved = ledger.get(operation_id, owner=owner, account_id=account_id)
    if saved.file_id is None or saved.state == 'ready':
        return saved
    return await reconcile_library_upload(client, session, ledger, operation_id,
                                          owner=owner, account_id=account_id)


async def status_local_upload(operation_id: str, *,
                              owner: str | None = None) -> LibraryUpload:
    """Verify the selected account, then read a saved operation without source bytes."""
    if sys.platform != 'darwin':
        raise ValueError('Library upload status is currently supported on macOS only')
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as error:
        if error.name != 'playwright':
            raise
        raise ValueError('Install browser support with the browser extra') from None
    from .subchat_browser.background import background_chrome_context, new_background_page
    from .subchat_chrome_login import chrome_http_session
    from .subchat_chrome_profile import temporary_chrome_profile
    from .subchat_plugin import plugin_paths, selected_chrome_login

    _, state = plugin_paths()
    source, account_id = selected_chrome_login(state)
    if source is None or account_id is None:
        raise ValueError('Select a Chrome login and pin its Chat account first')
    database = state / 'library-uploads.sqlite3'
    if not database.is_file() or database.is_symlink():
        raise ValueError('Library upload operation is unavailable')
    with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=rw', uri=True)
                 ) as connection:
        ledger = LibraryUploadLedger(connection)
        ledger.get(operation_id, owner=owner, account_id=account_id)
        async with async_playwright() as driver:
            async with temporary_chrome_profile(source) as snapshot:
                async with background_chrome_context(
                        driver, snapshot, [f'--profile-directory={source.name}']) as context:
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
                        session = await chrome_http_session(
                            context, client, expected_account_id=account_id,
                            page_factory=lambda: new_background_page(context))
                        return await _status_in_session(
                            client, session, ledger, operation_id,
                            owner=owner, account_id=account_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', nargs='?', type=Path)
    parser.add_argument('--operation-id', default=None,
                        help='reuse an existing operation only for read-only status')
    parser.add_argument('--status', metavar='OPERATION_ID',
                        help='inspect and reconcile an operation without its source file')
    parser.add_argument('--prepare', action='store_true',
                        help='approve exact local bytes and ID for a later MCP upload; '
                             'do not open a browser or upload')
    args = parser.parse_args(argv)
    if args.status is not None:
        if args.file is not None or args.operation_id is not None or args.prepare:
            parser.error('--status cannot be combined with a file, --operation-id or --prepare')
        operation_id = args.status
    else:
        if args.file is None:
            parser.error('a file or --status is required')
        operation_id = args.operation_id or secrets.token_hex(16)
    print(json.dumps({'operation_id': operation_id}), flush=True)
    try:
        if args.status is not None:
            result = asyncio.run(status_local_upload(operation_id))
        elif args.prepare:
            assert isinstance(args.file, Path)
            result = prepare_local_upload(args.file, operation_id=operation_id)
        else:
            assert isinstance(args.file, Path)
            result = asyncio.run(upload_local_file(args.file, operation_id=operation_id))
    except (httpx.HTTPError, TimeoutError):
        print(json.dumps({'operation_id': operation_id, 'outcome': 'unverified',
                          'automatic_retry': False,
                          'next_action': 'Inspect this same operation ID later'}))
        raise SystemExit(1) from None
    output: dict[str, object] = {
        'operation_id': result.operation_id, 'file_id': result.file_id,
        'library_item_id': result.library_item_id, 'state': result.state,
        'automatic_retry': result.automatic_retry,
    }
    if args.prepare:
        output.update({'outcome': 'prepared', 'provider_dispatched': False,
                       'prepared_until': result.mcp_prepared_until})
    print(json.dumps(output))


if __name__ == '__main__':
    main()
