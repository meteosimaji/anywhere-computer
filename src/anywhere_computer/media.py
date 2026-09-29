"""Bounded local audio clips and video frames for MCP clients that accept media items."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import wave
from pathlib import Path
from typing import Annotated

from pydantic import Field, JsonValue

from .files import absolute_path
from .models import Contract
from .plugin_audio import AUDIO_LIMIT, bounded_audio
from .plugin_images import IMAGE_LIMIT, bounded_image

_SOURCE_LIMIT = 512 * 1024 * 1024
_DECODER_TIMEOUT = 12
_TRANSCRIPTION_TIMEOUT = 120
_TRANSCRIPT_LIMIT = 8192
_MODEL_LIMIT = 5 * 1024 * 1024 * 1024


class MediaAudioClip(Contract):
    path: str = Field(min_length=1, max_length=4096)
    start_seconds: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)
    duration_seconds: int = Field(default=10, ge=1, le=10)


class MediaVideoFrames(Contract):
    path: str = Field(min_length=1, max_length=4096)
    timestamps_seconds: list[Annotated[float, Field(ge=0, le=600, allow_inf_nan=False)]] = (
        Field(min_length=1, max_length=4)
    )


class MediaTranscribe(Contract):
    path: str = Field(min_length=1, max_length=4096)
    model_path: str = Field(min_length=1, max_length=4096)
    start_seconds: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)
    duration_seconds: int = Field(default=10, ge=1, le=10)
    language: str | None = Field(default=None, min_length=2, max_length=3,
                                  pattern=r"^[a-z]+$")


def media_status() -> dict[str, JsonValue]:
    return {"decoder_available": shutil.which("ffmpeg") is not None,
            "local_transcriber_available": shutil.which("whisper") is not None,
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
        raise ValueError("Media decoding exceeded the 12-second limit") from error
    finally:
        # Cancellation must release the decoder too, including daemon shutdown.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
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


def _checkpoint(path_value: str) -> tuple[Path, str]:
    path = absolute_path(path_value)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MODEL_LIMIT or path.suffix != ".pt":
        raise ValueError("Whisper model must be an existing local .pt file within 5 GiB")
    with path.open("rb") as checkpoint:
        digest = hashlib.file_digest(checkpoint, "sha256").hexdigest()
    return path, digest


async def transcribe(args: MediaTranscribe) -> dict[str, JsonValue]:
    """Return text from an explicitly installed local Whisper checkpoint."""
    executable = shutil.which("whisper")
    if executable is None:
        raise ValueError("Local Whisper CLI unavailable; install it and a trusted model file")
    model, model_sha256 = await asyncio.to_thread(_checkpoint, args.model_path)
    path = _source(args.path)
    wav = await _decode([
        "-ss", str(args.start_seconds), "-protocol_whitelist", "file,pipe",
        "-i", os.fspath(path), "-t", str(args.duration_seconds), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ])
    if len(wav) > AUDIO_LIMIT:
        raise ValueError("Decoded audio clip exceeds the 2 MiB limit")
    with wave.open(io.BytesIO(wav), "rb") as reader:
        samples = reader.readframes(reader.getnframes())
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise ValueError("Decoded audio format is invalid")
    if not samples or not any(samples):
        return {"path": str(path), "start_seconds": args.start_seconds,
                "requested_duration_seconds": args.duration_seconds,
                "backend": "openai-whisper-cli", "model_sha256": model_sha256,
                "language": args.language, "speech_detected": False, "text": "",
                "quality_warning": "Digital silence; no model inference was run"}
    with tempfile.TemporaryDirectory(prefix="anywhere-transcribe-") as temporary:
        clip = Path(temporary) / "clip.wav"
        descriptor = os.open(clip, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(wav)
        command = [executable, str(clip), "--model", str(model),
                   "--output_dir", temporary, "--output_format", "json",
                   "--verbose", "False", "--fp16", "False", "--threads", "2",
                   "--condition_on_previous_text", "False"]
        if args.language is not None:
            command.extend(["--language", args.language])
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), _TRANSCRIPTION_TIMEOUT)
        except TimeoutError as error:
            raise ValueError("Local transcription exceeded the 120-second limit") from error
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        transcript = Path(temporary) / "clip.json"
        if process.returncode != 0 or not transcript.is_file() or (
            transcript.stat().st_size > 256 * 1024
        ):
            raise ValueError("Local transcription failed or returned an invalid result")
        try:
            result = json.loads(transcript.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Local transcription returned an invalid result") from error
    if not isinstance(result, dict):
        raise ValueError("Local transcription returned an invalid result")
    text = result.get("text")
    language = result.get("language")
    if not isinstance(text, str) or len(text) > _TRANSCRIPT_LIMIT or (
        language is not None and (
            not isinstance(language, str) or len(language) > 16 or not language.isascii()
        )
    ):
        raise ValueError("Local transcription returned an invalid result")
    return {"path": str(path), "start_seconds": args.start_seconds,
            "requested_duration_seconds": args.duration_seconds,
            "backend": "openai-whisper-cli", "model_sha256": model_sha256,
            "language": language, "speech_detected": bool(text.strip()),
            "text": text.strip(),
            "quality_warning": "Transcription is model generated; verify unclear speech"}


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
