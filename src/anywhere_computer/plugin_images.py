"""Bounded inline images for plugin results; no URLs are fetched or decoded as pixels."""

import base64
import binascii
import hashlib

from pydantic import JsonValue

IMAGE_LIMIT = 2 * 1024 * 1024
MAX_IMAGES = 4
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _matches_type(raw: bytes, mime_type: str) -> bool:
    if mime_type == "image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    return raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"


def bounded_image(
    item: dict[str, JsonValue], remaining: int,
) -> tuple[dict[str, JsonValue] | None, int]:
    """Validate the envelope and magic bytes, not the complete compressed image."""
    data, mime_type = item.get("data"), item.get("mimeType")
    if not isinstance(data, str) or not isinstance(mime_type, str):
        raise ValueError("Invalid plugin image envelope")
    if mime_type not in _IMAGE_TYPES or len(data) > 4 * ((remaining + 2) // 3):
        return None, 0
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Invalid plugin image base64") from error
    if not raw or base64.b64encode(raw).decode("ascii") != data:
        raise ValueError("Plugin image base64 must be nonempty and canonical")
    if not _matches_type(raw, mime_type):
        raise ValueError("Plugin image does not match its MIME type")
    if len(raw) > remaining:
        return None, 0
    return {"type": "image", "data": data, "mimeType": mime_type}, len(raw)


def image_summary(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Describe a previously validated image without repeating base64 in model text."""
    data = item["data"]
    if not isinstance(data, str):
        raise ValueError("Invalid plugin image data")
    raw = base64.b64decode(data, validate=True)
    return {
        "type": "image", "mimeType": item["mimeType"],
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }
