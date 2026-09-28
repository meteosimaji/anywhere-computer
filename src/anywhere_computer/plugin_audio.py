"""Bounded MCP audio items, kept out of structured model text."""

import base64
import binascii
import hashlib

from pydantic import JsonValue

AUDIO_LIMIT = 2 * 1024 * 1024
MAX_AUDIO_ITEMS = 1
_AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/x-caf"}


def _matches_type(raw: bytes, mime_type: str) -> bool:
    if mime_type in {"audio/wav", "audio/x-wav"}:
        return raw.startswith(b"RIFF") and raw[8:12] == b"WAVE"
    if mime_type == "audio/x-caf":
        return raw.startswith(b"caff")
    return raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xff
                                         and raw[1] & 0xe0 == 0xe0)


def bounded_audio(item: dict[str, JsonValue], remaining: int
                  ) -> tuple[dict[str, JsonValue] | None, int]:
    data, mime_type = item.get("data"), item.get("mimeType")
    if not isinstance(data, str) or not isinstance(mime_type, str):
        raise ValueError("Invalid plugin audio envelope")
    if mime_type not in _AUDIO_TYPES or len(data) > 4 * ((remaining + 2) // 3):
        return None, 0
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Invalid plugin audio base64") from error
    if not raw or base64.b64encode(raw).decode("ascii") != data:
        raise ValueError("Plugin audio base64 must be nonempty and canonical")
    if not _matches_type(raw, mime_type):
        raise ValueError("Plugin audio does not match its MIME type")
    if len(raw) > remaining:
        return None, 0
    return {"type": "audio", "data": data, "mimeType": mime_type}, len(raw)


def audio_summary(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    data = item["data"]
    if not isinstance(data, str):
        raise ValueError("Invalid plugin audio data")
    raw = base64.b64decode(data, validate=True)
    return {"type": "audio", "mimeType": item["mimeType"],
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
