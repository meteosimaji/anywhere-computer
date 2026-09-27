"""Bounded read-only reconciliation of a pinned account's Library upload."""

from __future__ import annotations

import json

import httpx

from .subchat import SubchatAccessError
from .subchat_browser.http_reader import bounded_httpx_body
from .subchat_http_session import ObservedHTTPSession
from .subchat_state import SubchatAccountMismatch
from .subchat_upload_state import LibraryUpload, LibraryUploadLedger

_LIBRARY_URL = 'https://chatgpt.com/backend-api/files/library'
_PAGE_LIMIT = 100
_MAX_PAGES = 20


async def reconcile_library_upload(client: httpx.AsyncClient,
                                   session: ObservedHTTPSession,
                                   ledger: LibraryUploadLedger,
                                   operation_id: str, *, owner: str | None,
                                   account_id: str) -> LibraryUpload:
    """Find only the previously checkpointed file ID; never dispatch an upload."""
    if session.account_id != account_id:
        raise SubchatAccountMismatch('Selected Chat account changed')
    saved = ledger.get(operation_id, owner=owner, account_id=account_id)
    if saved.file_id is None or saved.state == 'ready':
        return saved
    cursor: str | None = None
    observed_cursors: set[str] = set()
    for _ in range(_MAX_PAGES):
        request_body: dict[str, object] = {
            'categories': [], 'cursor': cursor, 'include_hidden_files': False,
            'include_saved_entities': True, 'include_sites': False,
            'limit': _PAGE_LIMIT, 'providers': [], 'ranking': 'suggested',
            'source': 'uploaded', 'trashed_only': False,
        }
        async with client.stream('POST', _LIBRARY_URL, headers=session.headers(),
                                 json=request_body, timeout=15,
                                 follow_redirects=False) as response:
            if response.status_code in (401, 403):
                raise SubchatAccessError(response.status_code)
            if response.status_code != 200:
                raise ConnectionError('Library search did not succeed')
            if response.headers.get('content-type', '').split(';', 1)[0].strip() != (
                    'application/json'):
                raise ValueError('Unexpected Library search response')
            raw = await bounded_httpx_body(response, 1_048_576,
                                           'Library search response is too large')
        try:
            page = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError('Invalid Library search response') from error
        if (not isinstance(page, dict) or not isinstance(page.get('items'), list)
                or len(page['items']) > _PAGE_LIMIT
                or (page.get('cursor') is not None
                    and (not isinstance(page['cursor'], str)
                         or not 1 <= len(page['cursor']) <= 4096))):
            raise ValueError('Invalid Library search response')
        saved = ledger.reconcile_search(operation_id, raw, owner=owner,
                                        account_id=account_id,
                                        observed_account_id=session.account_id)
        if saved.state == 'ready':
            return saved
        next_cursor = page.get('cursor')
        if next_cursor is None:
            return saved
        if next_cursor in observed_cursors:
            raise ValueError('Library search cursor repeated')
        observed_cursors.add(next_cursor)
        cursor = next_cursor
    raise ValueError('Library search exceeded its page limit')
