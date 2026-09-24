"""Read one image asset bound to a saved ordinary-Chat input and its tool result."""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .subchat import SubchatAccessError, SubchatAnswer
from .subchat_browser.history import (
    bound_final_images,
    bound_tool_images,
    matched_input,
    project_observation,
)
from .subchat_http_download import SandboxFileTooLarge, _content_url
from .subchat_http_session import ObservedHTTPSession
from .subchat_state import SubchatAccountMismatch, SubchatSubmission

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_CONVERSATION = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


class _ImageMetadata(BaseModel):
    model_config = ConfigDict(strict=True)

    status: str = Field(min_length=1, max_length=128)
    download_url: str = Field(min_length=1, max_length=8192)
    file_size_bytes: int = Field(ge=1)


@dataclass(frozen=True, slots=True, repr=False)
class ImageDownload:
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    content: bytes
    submission_state: str


def _image_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == 'image/png':
        if not content.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('PNG signature is invalid')
        position = 8
        width = height = 0
        has_data = has_end = False
        while position + 12 <= len(content):
            size = struct.unpack_from('>I', content, position)[0]
            end = position + 12 + size
            if end > len(content):
                raise ValueError('PNG chunk is incomplete')
            kind = content[position + 4:position + 8]
            data = content[position + 8:position + 8 + size]
            crc = struct.unpack_from('>I', content, position + 8 + size)[0]
            if zlib.crc32(kind + data) != crc:
                raise ValueError('PNG chunk checksum is invalid')
            if position == 8:
                if kind != b'IHDR' or size != 13:
                    raise ValueError('PNG header is invalid')
                width, height = struct.unpack_from('>II', data)
            if kind == b'IDAT':
                has_data = True
            if kind == b'IEND':
                has_end = size == 0 and end == len(content)
                break
            position = end
        if not has_data or not has_end:
            raise ValueError('PNG image is incomplete')
        return width, height
    if mime_type == 'image/jpeg':
        if not content.startswith(b'\xff\xd8') or not content.endswith(b'\xff\xd9'):
            raise ValueError('JPEG markers are invalid')
        position = 2
        while position + 4 <= len(content):
            if content[position] != 0xff:
                raise ValueError('JPEG marker is invalid')
            while position < len(content) and content[position] == 0xff:
                position += 1
            if position >= len(content):
                break
            marker = content[position]
            position += 1
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                continue
            if position + 2 > len(content):
                break
            size = struct.unpack_from('>H', content, position)[0]
            if size < 2 or position + size > len(content):
                raise ValueError('JPEG segment is invalid')
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                if size < 7:
                    raise ValueError('JPEG frame is invalid')
                height, width = struct.unpack_from('>HH', content, position + 3)
                return width, height
            if marker == 0xda:
                break
            position += size
        raise ValueError('JPEG dimensions are unavailable')
    if mime_type == 'image/webp':
        if (len(content) < 20 or content[:4] != b'RIFF' or content[8:12] != b'WEBP'
                or struct.unpack_from('<I', content, 4)[0] != len(content) - 8):
            raise ValueError('WebP container is invalid')
        position = 12
        dimensions: tuple[int, int] | None = None
        has_image = False
        while position + 8 <= len(content):
            kind = content[position:position + 4]
            size = struct.unpack_from('<I', content, position + 4)[0]
            end = position + 8 + size
            if end > len(content):
                raise ValueError('WebP chunk is incomplete')
            data = content[position + 8:end]
            if kind == b'VP8X' and size >= 10:
                dimensions = (int.from_bytes(data[4:7], 'little') + 1,
                              int.from_bytes(data[7:10], 'little') + 1)
            elif kind == b'VP8 ' and size >= 10 and data[3:6] == b'\x9d\x01\x2a':
                has_image = True
                dimensions = dimensions or (struct.unpack_from('<H', data, 6)[0] & 0x3fff,
                                            struct.unpack_from('<H', data, 8)[0] & 0x3fff)
            elif kind == b'VP8L' and size >= 5 and data[0] == 0x2f:
                has_image = True
                bits = int.from_bytes(data[1:5], 'little')
                dimensions = dimensions or ((bits & 0x3fff) + 1,
                                            ((bits >> 14) & 0x3fff) + 1)
            elif kind == b'ANIM':
                has_image = True
            position = end + (size & 1)
        if position != len(content) or not has_image or dimensions is None:
            raise ValueError('WebP image is incomplete')
        return dimensions
    raise ValueError('Unsupported image type')


async def download_verified_image(saved: SubchatSubmission, history_payload: bytes, *,
                                  session: ObservedHTTPSession, client: httpx.AsyncClient,
                                  max_bytes: int = MAX_IMAGE_BYTES) -> ImageDownload:
    """Resolve only the one image in the saved input's finished tool result."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_IMAGE_BYTES:
        raise ValueError('Invalid image byte limit')
    if (saved.state not in {'submitted', 'completed'} or saved.provider_account_id is None
            or saved.provider_account_id != session.account_id):
        raise SubchatAccountMismatch('Saved Chat image belongs to another account')
    if (saved.conversation_id is None or _CONVERSATION.fullmatch(saved.conversation_id) is None
            or saved.user_message_id is None):
        raise ValueError('Saved Chat input identity is unavailable')
    matched = matched_input(history_payload, saved)
    if matched is None:
        raise ValueError('Saved Chat input is absent from history')
    history, user = matched
    keys = ('turn_exchange_id', 'working_turn_id')
    if any(not isinstance(user.metadata.get(key), str) or not user.metadata[key] for key in keys):
        raise ValueError('Saved Chat turn identity is unavailable')
    matching_users = [message for message in history.messages
                      if message.author.get('role') == 'user'
                      and all(message.metadata.get(key) == user.metadata[key] for key in keys)]
    if len(matching_users) != 1:
        raise ValueError('Saved Chat turn identity is ambiguous')
    observation = project_observation(history_payload, saved)
    if not isinstance(observation, SubchatAnswer) and any(
        message.author.get('role') == 'assistant' and message.channel == 'final'
        and all(message.metadata.get(key) == user.metadata[key] for key in keys)
        for message in history.messages[history.messages.index(user) + 1:]
    ):
        raise ValueError('Saved Chat final is not verified')
    final = None
    if saved.state == 'completed':
        if (not isinstance(observation, SubchatAnswer)
                or observation.answer_message_id != saved.answer_message_id
                or observation.answer_type != saved.answer_type
                or observation.text != saved.answer):
            raise ValueError('Saved Chat final does not match current history')
    if isinstance(observation, SubchatAnswer):
        final = next(message for message in history.messages
                     if message.id == observation.answer_message_id)
    images = (*bound_tool_images(history, user, before=final),
              *(bound_final_images(final) if final is not None else ()))
    if len(images) != 1:
        raise ValueError('Exactly one bound image is required')
    image = images[0]
    mime = image.mime_type
    size = image.size_bytes
    width = image.width
    height = image.height
    if size > max_bytes:
        raise SandboxFileTooLarge('Chat image exceeds the requested byte limit')
    file_id = image.pointer.removeprefix('sediment://')
    metadata_url = ('https://chatgpt.com/backend-api/files/download/' + file_id
                    + '?' + urlencode({'conversation_id': saved.conversation_id,
                                       'inline': 'false'}))
    headers = {**session.headers(), 'accept-encoding': 'identity'}
    metadata_bytes = bytearray()
    async with client.stream('GET', metadata_url, headers=headers, timeout=15.0,
                             follow_redirects=False) as response:
        if response.status_code in (401, 403):
            raise SubchatAccessError(response.status_code)
        if (response.status_code != 200 or response.headers.get('content-type', '').split(';')[0]
                .strip().lower() != 'application/json'):
            raise ConnectionError('Chat image metadata was not accepted')
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed Chat image metadata is unsupported')
        async for chunk in response.aiter_raw():
            if len(metadata_bytes) + len(chunk) > 65_536:
                raise ValueError('Chat image metadata is too large')
            metadata_bytes.extend(chunk)
    metadata = _ImageMetadata.model_validate_json(metadata_bytes)
    if metadata.status != 'success' or metadata.file_size_bytes != size:
        raise ValueError('Chat image metadata does not match history')
    signed_url = _content_url(metadata.download_url)
    query = parse_qs(urlsplit(signed_url).query, strict_parsing=True)
    if query.get('id') != [file_id]:
        raise ValueError('Chat image download URL names another asset')
    content = bytearray()
    async with client.stream('GET', signed_url, headers=headers,
                             timeout=httpx.Timeout(120.0, connect=10.0),
                             follow_redirects=False) as response:
        if response.status_code in (401, 403):
            raise SubchatAccessError(response.status_code)
        if response.status_code != 200:
            raise ConnectionError('Chat image content was not accepted')
        if response.headers.get('content-type', '').split(';')[0].strip().lower() != mime:
            raise ValueError('Chat image response type does not match history')
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed Chat image response is unsupported')
        length = response.headers.get('content-length')
        if length is not None and (not length.isdecimal() or int(length) != size):
            raise ValueError('Chat image response length does not match history')
        async for chunk in response.aiter_raw():
            if len(content) + len(chunk) > max_bytes:
                raise SandboxFileTooLarge('Chat image exceeds the requested byte limit')
            content.extend(chunk)
    if len(content) != size:
        raise ValueError('Chat image bytes do not match history')
    actual_width, actual_height = _image_dimensions(bytes(content), mime)
    if (actual_width, actual_height) != (width, height):
        raise ValueError('Chat image dimensions do not match history')
    return ImageDownload(mime, size, width, height, bytes(content), saved.state)
