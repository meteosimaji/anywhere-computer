"""Bounded download of a file linked by a verified ordinary Chat final answer."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .subchat import SubchatAccessError, SubchatAnswer
from .subchat_http_session import ObservedHTTPSession
from .subchat_state import SubchatAccountMismatch, SubchatSubmission

MAX_FILE_BYTES = 16 * 1024 * 1024
_ORIGIN = 'https://chatgpt.com'
_CONVERSATION = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')
_LINK = re.compile(r'\[[^\]\r\n]+\]\((sandbox:/mnt/data/[^)\r\n]+)\)')


class _DownloadMetadata(BaseModel):
    model_config = ConfigDict(strict=True)

    download_url: str = Field(min_length=1, max_length=8192)
    file_name: str = Field(min_length=1, max_length=255)
    file_size_bytes: int | None = Field(default=None, ge=0)
    mime_type: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True, slots=True, repr=False)
class SandboxDownload:
    file_name: str
    mime_type: str
    file_size_bytes: int
    content: bytes


class SandboxFileTooLarge(ValueError):
    """The requested file exceeds the caller's explicit byte limit."""


def _sandbox_path(value: str) -> str:
    prefix = 'sandbox:/mnt/data/'
    if (not isinstance(value, str) or not value.startswith(prefix) or len(value) > 1024
            or any(ord(char) < 32 or char in '?#%\\' for char in value)):
        raise ValueError('Invalid sandbox file path')
    tail = value[len(prefix):]
    if not tail or any(segment in ('', '.', '..') for segment in tail.split('/')):
        raise ValueError('Invalid sandbox file path')
    return '/mnt/data/' + tail


def _content_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc != 'chatgpt.com'
            or parsed.path != '/backend-api/estuary/content' or not parsed.query
            or parsed.fragment or any(ord(char) < 33 for char in value)):
        raise ValueError('Invalid Chat download URL')
    return value


async def download_verified_sandbox_file(
        saved: SubchatSubmission, answer: SubchatAnswer, sandbox_link: str,
        *, session: ObservedHTTPSession, client: httpx.AsyncClient,
        max_bytes: int = MAX_FILE_BYTES) -> SandboxDownload:
    """Fetch only a final-answer link bound to the saved account and message."""
    if max_bytes <= 0 or max_bytes > MAX_FILE_BYTES:
        raise ValueError('Invalid file size limit')
    if (saved.state != 'completed' or saved.provider_account_id != session.account_id
            or saved.provider_account_id is None):
        raise SubchatAccountMismatch('Completed Chat belongs to another account')
    if answer.text is None:
        raise ValueError('Final answer has no text containing a sandbox file link')
    if (saved.conversation_id is None or _CONVERSATION.fullmatch(saved.conversation_id) is None
            or saved.answer_message_id is None
            or saved.answer_message_id != answer.answer_message_id
            or saved.conversation_id != answer.conversation_id
            or saved.user_message_id != answer.user_message_id
            or saved.answer != answer.text):
        raise ValueError('Verified final answer does not match the saved operation')
    path = _sandbox_path(sandbox_link)
    if sandbox_link not in _LINK.findall(answer.text):
        raise ValueError('Sandbox file link is absent from the verified final answer')

    route = (_ORIGIN + '/backend-api/conversation/' + saved.conversation_id
             + '/interpreter/download?' + urlencode({
                 'download_intent': 'true', 'message_id': saved.answer_message_id,
                 'sandbox_path': path}))
    metadata_bytes = bytearray()
    headers = {**session.headers(), 'accept-encoding': 'identity'}
    async with client.stream('GET', route, headers=headers, timeout=15.0,
                             follow_redirects=False) as response:
        if response.status_code in (401, 403):
            raise SubchatAccessError(response.status_code)
        content_type = response.headers.get('content-type', '').split(';', 1)[0].strip()
        if response.status_code != 200 or content_type != 'application/json':
            raise ConnectionError('Chat file metadata was not accepted')
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed Chat file metadata is unsupported')
        async for chunk in response.aiter_raw():
            if len(metadata_bytes) + len(chunk) > 65_536:
                raise ValueError('Chat file metadata is too large')
            metadata_bytes.extend(chunk)
    metadata = _DownloadMetadata.model_validate_json(metadata_bytes)
    if metadata.file_size_bytes is not None and metadata.file_size_bytes > max_bytes:
        raise SandboxFileTooLarge('Chat file exceeds the size limit')
    if (metadata.file_name in ('.', '..') or '/' in metadata.file_name
            or '\\' in metadata.file_name or any(ord(char) < 32 for char in metadata.file_name)
            or any(ord(char) < 33 for char in metadata.mime_type)):
        raise ValueError('Invalid Chat file metadata')
    content_url = _content_url(metadata.download_url)
    content = bytearray()
    async with client.stream('GET', content_url, headers=headers,
                             timeout=httpx.Timeout(120.0, connect=10.0),
                             follow_redirects=False) as file_response:
        if file_response.status_code in (401, 403):
            raise SubchatAccessError(file_response.status_code)
        if file_response.status_code != 200:
            raise ConnectionError('Chat file content was not accepted')
        received_type = file_response.headers.get('content-type', '').split(';', 1)[0].strip()
        if received_type in {'text/html', 'application/json'}:
            raise ValueError('Chat file response has an unexpected content type')
        if file_response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed Chat file response is unsupported')
        for_chunk_limit = max_bytes + 1
        async for chunk in file_response.aiter_raw():
            if len(content) + len(chunk) >= for_chunk_limit:
                raise SandboxFileTooLarge('Chat file exceeds the size limit')
            content.extend(chunk)
    if metadata.file_size_bytes is not None and len(content) != metadata.file_size_bytes:
        raise ValueError('Chat file size does not match metadata')
    return SandboxDownload(metadata.file_name, metadata.mime_type,
                           len(content), bytes(content))
