"""Bounded, explicitly prepared file handoff using one selected-account session."""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

import httpx
from pydantic import Field, model_validator

from .models import Contract
from .subchat_library_upload import (
    UploadPreflightError,
    _observe_ready_after_upload,
    _read_source,
    _status_in_session,
    _upload_on_page,
    prepare_local_upload,
)
from .subchat_upload_state import LibraryUpload, LibraryUploadLedger

MAX_BATCH_BYTES = 41_943_040


@dataclass(frozen=True)
class LibraryBatchResult:
    uploads: list[LibraryUpload]
    failed_operation_id: str | None = None
    error_code: str | None = None


class LibraryBatchFile(Contract):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    path: str = Field(min_length=1, max_length=4096)

    @model_validator(mode='after')
    def absolute_path(self) -> Self:
        if not Path(self.path).is_absolute():
            raise ValueError('Each upload source must be an absolute path')
        return self


class LibraryBatch(Contract):
    files: tuple[LibraryBatchFile, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode='after')
    def unique(self) -> Self:
        if (len({item.operation_id for item in self.files}) != len(self.files)
                or len({item.path for item in self.files}) != len(self.files)):
            raise ValueError('Upload operation IDs and source paths must be unique')
        return self


class LibraryBatchStatus(Contract):
    operation_ids: tuple[Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')], ...] = Field(
        min_length=1, max_length=10)

    @model_validator(mode='after')
    def valid_ids(self) -> Self:
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError('Upload operation IDs must be unique lowercase hex identities')
        return self


def prepare_local_batch(batch: LibraryBatch, *, owner: str | None = None
                        ) -> list[LibraryUpload]:
    """Check every source before saving the same per-file local approvals as single upload."""
    total = 0
    for item in batch.files:
        _, payload, _ = _read_source(Path(item.path))
        total += len(payload)
        if total > MAX_BATCH_BYTES:
            raise UploadPreflightError('Library upload batch exceeds 40 MiB')
    return [prepare_local_upload(Path(item.path), operation_id=item.operation_id, owner=owner)
            for item in batch.files]


def _prepared_payloads(ledger: LibraryUploadLedger, batch: LibraryBatch, *,
                       owner: str | None, account_id: str) -> dict[str, bytes]:
    """Pin all unsent files before opening a browser or claiming any provider request."""
    payloads: dict[str, bytes] = {}
    total = 0
    for item in batch.files:
        saved = ledger.get(item.operation_id, owner=owner, account_id=account_id)
        total += saved.file_size
        if total > MAX_BATCH_BYTES:
            raise UploadPreflightError('Library upload batch exceeds 40 MiB')
        if saved.create_claimed or saved.state == 'ready':
            continue
        if saved.mcp_prepared_until is None or saved.mcp_prepared_until <= time.time():
            raise UploadPreflightError('Library MCP preparation is unavailable or expired')
        try:
            name, payload, digest = _read_source(Path(item.path))
        except ValueError as error:
            raise UploadPreflightError(str(error)) from error
        if (saved.file_name, saved.file_size, saved.sha256, saved.mcp_source_path) != (
                name, len(payload), digest, item.path):
            raise UploadPreflightError('Prepared Library upload file changed')
        payloads[item.operation_id] = payload
    return payloads


async def run_local_batch(*, batch: LibraryBatch | None = None,
                          status: LibraryBatchStatus | None = None,
                          owner: str | None = None) -> LibraryBatchResult:
    """Upload prepared files, or only reconcile IDs; each claimed file is never resent.

    A partial/uncertain result stops new uploads in this call. A status request
    never reads source paths, including unsent files. A later explicit upload
    can use the same per-file IDs to start only still-prepared files.
    """
    if (batch is None) == (status is None):
        raise UploadPreflightError('Choose a file batch or saved upload status')
    if sys.platform != 'darwin':
        raise UploadPreflightError('Library UI upload is currently supported on macOS only')
    try:
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as error:
        if error.name != 'playwright':
            raise
        raise UploadPreflightError('Install browser support with the browser extra') from None

    from .subchat_browser.background import background_chrome_context, new_background_page
    from .subchat_chrome_login import chrome_http_session
    from .subchat_chrome_profile import temporary_chrome_profile
    from .subchat_plugin import plugin_paths, selected_chrome_login

    _, state = plugin_paths()
    source, account_id = selected_chrome_login(state)
    if source is None or account_id is None:
        raise UploadPreflightError('Select a Chrome login and pin its Chat account first')
    database = state / 'library-uploads.sqlite3'
    if not database.is_file() or database.is_symlink():
        raise UploadPreflightError('Prepare each exact file locally before a batch upload')
    operation_ids = ([item.operation_id for item in batch.files] if batch is not None
                     else list(status.operation_ids) if status is not None else [])
    with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=rw', uri=True)
                 ) as connection:
        ledger = LibraryUploadLedger(connection)
        # Validate every owner/account binding before any side effect or status network call.
        try:
            saved = [ledger.get(identity, owner=owner, account_id=account_id)
                     for identity in operation_ids]
        except ValueError as error:
            raise UploadPreflightError(str(error)) from error

        def result(failed_id: str | None = None, code: str | None = None) -> LibraryBatchResult:
            # Another connection may have claimed a later item while this one awaited
            # the provider. Never report that stale snapshot as known not dispatched.
            return LibraryBatchResult([
                ledger.get(identity, owner=owner, account_id=account_id)
                for identity in operation_ids], failed_id, code)

        payloads = (_prepared_payloads(ledger, batch, owner=owner, account_id=account_id)
                    if batch is not None else {})
        if not payloads and not any(item.file_id is not None and item.state != 'ready'
                                    for item in saved):
            return result()
        async with async_playwright() as driver:
            async with temporary_chrome_profile(source) as snapshot:
                async with background_chrome_context(
                        driver, snapshot, [f'--profile-directory={source.name}']) as context:
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
                        session = await chrome_http_session(
                            context, client, expected_account_id=account_id,
                            page_factory=lambda: new_background_page(context))
                        if session.account_id != account_id:
                            raise UploadPreflightError('Selected Chat account changed')
                        for index, old in enumerate(saved):
                            try:
                                if old.operation_id in payloads:
                                    assert batch is not None
                                    item = batch.files[index]
                                    page = await new_background_page(context)
                                    try:
                                        await _upload_on_page(
                                            page, ledger, old.operation_id, owner=owner,
                                            account_id=account_id, name=old.file_name,
                                            payload=payloads[old.operation_id],
                                            require_prepared=True, source_path=item.path)
                                    finally:
                                        await page.close()
                                    saved[index] = await _observe_ready_after_upload(
                                        client, session, ledger, old.operation_id,
                                        owner=owner, account_id=account_id)
                                else:
                                    saved[index] = await _status_in_session(
                                        client, session, ledger, old.operation_id,
                                        owner=owner, account_id=account_id)
                            except (ValueError, httpx.HTTPError, OSError, TimeoutError,
                                    BrowserError) as error:
                                # The durable per-file stage is authoritative after any
                                # lost response. Return it along with untouched later files.
                                saved[index] = ledger.get(
                                    old.operation_id, owner=owner, account_id=account_id)
                                code = ('preparation_unavailable'
                                        if isinstance(error, UploadPreflightError) else
                                        'verification_failed' if isinstance(error, ValueError)
                                        else 'transport_unverified')
                                return result(old.operation_id, code)
                            if batch is not None and saved[index].state != 'ready':
                                break
                        return result()
