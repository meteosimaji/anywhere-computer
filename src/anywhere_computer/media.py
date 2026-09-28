"""Bounded local audio clips and video frames for MCP clients that accept media items."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import stat
from pathlib import Path
from typing import Annotated

from pydantic import Field, JsonValue

from .files import absolute_path
from .models import Contract
from .plugin_audio import AUDIO_LIMIT, bounded_audio
from .plugin_images import IMAGE_LIMIT, bounded_image

_SOURCE_LIMIT = 512 * 1024 * 1024
_DECODER_TIMEOUT = 12


class MediaAudioClip(Contract):
    path: str = Field(min_length=1, max_length=4096)
    start_seconds: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)
    duration_seconds: int = Field(default=10, ge=1, le=10)


class MediaVideoFrames(Contract):
    path: str = Field(min_length=1, max_length=4096)
    timestamps_seconds: list[Annotated[float, Field(ge=0, le=600, allow_inf_nan=False)]] = (
        Field(min_length=1, max_length=4)
    )


def media_status() -> dict[str, JsonValue]:
    return {"decoder_available": shutil.which("ffmpeg") is not None,
            "source_limit_bytes": _SOURCE_LIMIT, "audio_clip_limit_seconds": 10,
            "video_frame_limit": 4, "model_media_receipt_verified": False}


def _source(path_value: str) -> Path:
    path = absolute_path(path_value)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _SOURCE_LIMIT:
        raise ValueError("Media source must be a regular file within the 512 MiB limit")
    return path


async def _decode(arguments: list[str]) -> bytes:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise ValueError("Media decoder unavailable; install FFmpeg on this device")
    process = await asyncio.create_subprocess_exec(
        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2",
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), _DECODER_TIMEOUT)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise ValueError("Media decoding exceeded the 12-second limit") from error
    if process.returncode != 0 or not output:
        raise ValueError("Media decoder could not read the requested clip or frame")
    return output


async def audio_clip(args: MediaAudioClip) -> dict[str, JsonValue]:
    path = _source(args.path)
    output = await _decode([
        "-ss", str(args.start_seconds), "-protocol_whitelist", "file,pipe",
        "-i", os.fspath(path), "-t", str(args.duration_seconds), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ])
    item: dict[str, JsonValue] = {"type": "audio", "mimeType": "audio/wav",
                                  "data": base64.b64encode(output).decode("ascii")}
    validated, _ = bounded_audio(item, AUDIO_LIMIT)
    if validated is None or len(output) <= 44:
        raise ValueError("Decoded audio clip is empty or exceeds the 2 MiB limit")
    return {"path": str(path), "start_seconds": args.start_seconds,
            "requested_duration_seconds": args.duration_seconds,
            "audio_format": "mono_16khz_pcm_wav", "content": [validated]}


async def video_frames(args: MediaVideoFrames) -> dict[str, JsonValue]:
    path = _source(args.path)
    frames: list[JsonValue] = []
    images: list[JsonValue] = []
    for timestamp in args.timestamps_seconds:
        output = await _decode([
            "-ss", str(timestamp), "-protocol_whitelist", "file,pipe",
            "-i", os.fspath(path), "-frames:v", "1",
            "-vf", "scale=960:-2:force_original_aspect_ratio=decrease",
            "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ])
        item: dict[str, JsonValue] = {"type": "image", "mimeType": "image/jpeg",
                                      "data": base64.b64encode(output).decode("ascii")}
        validated, size = bounded_image(item, IMAGE_LIMIT)
        if validated is None:
            raise ValueError("Decoded video frame exceeds the 2 MiB image limit")
        images.append(validated)
        frames.append({"requested_timestamp_seconds": timestamp, "bytes": size,
                       "capture_kind": "decoded_frame_near_timestamp"})
    return {"path": str(path), "frames": frames, "content": images}
