"""Real bounded media decoding and typed MCP projection."""

import asyncio
import io
import json
import shutil
import subprocess
import sys
import wave

import pytest
from mcp.types import AudioContent, CallToolResult, ImageContent
from pydantic import ValidationError

from anywhere_computer.mcp_results import normalize_tool_result
from anywhere_computer.mcp_server import _reply_result
from anywhere_computer.media import (
    MediaAudioClip,
    MediaTranscribe,
    MediaVideoFrames,
    audio_clip,
    transcribe,
    video_frames,
)
from anywhere_computer.models import Reply


@pytest.mark.parametrize('interruption', ['cancel', 'timeout'])
async def test_interrupted_decoder_reaps_its_child(monkeypatch, interruption):
    from anywhere_computer import media

    started = asyncio.Event()
    children = []
    spawn = asyncio.create_subprocess_exec

    async def stalled_decoder(*arguments, **options):
        child = await spawn(sys.executable, '-c', 'import time; time.sleep(60)', **options)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(media.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(media.asyncio, 'create_subprocess_exec', stalled_decoder)
    if interruption == 'timeout':
        monkeypatch.setattr(media, '_DECODER_TIMEOUT', .01)
    task = asyncio.create_task(media._decode([]))
    try:
        await asyncio.wait_for(started.wait(), 10)
        if interruption == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError, match='decoding exceeded'):
                await asyncio.wait_for(task, 10)
        assert children[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Optional FFmpeg not installed")
async def test_real_audio_and_video_are_projected_without_base64_in_model_text(tmp_path):
    audio = tmp_path / "tone.wav"
    video = tmp_path / "color.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", str(audio),
    ], check=True, timeout=10)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
        "-i", "color=c=blue:s=64x48:d=1", "-c:v", "mpeg4", str(video),
    ], check=True, timeout=10)

    heard = await audio_clip(MediaAudioClip(path=str(audio), duration_seconds=1))
    heard_item = heard["content"][0]
    assert heard_item["type"] == "audio"
    heard_wire = _reply_result("media_audio_clip", Reply(
        operation_id="a" * 32, state="completed", data=heard,
    ))
    parsed_audio = CallToolResult.model_validate(heard_wire)
    assert isinstance(parsed_audio.content[1], AudioContent)
    assert heard_item["data"] not in parsed_audio.content[0].text
    assert heard_item["data"] not in json.dumps(heard_wire["structuredContent"])

    seen = await video_frames(MediaVideoFrames(path=str(video), timestamps_seconds=[0.25]))
    seen_item = seen["content"][0]
    assert seen_item["type"] == "image"
    seen_wire = _reply_result("media_video_frames", Reply(
        operation_id="b" * 32, state="completed", data=seen,
    ))
    parsed_video = CallToolResult.model_validate(seen_wire)
    assert isinstance(parsed_video.content[1], ImageContent)
    assert seen_item["data"] not in parsed_video.content[0].text
    assert seen_item["data"] not in json.dumps(seen_wire["structuredContent"])


def test_external_audio_is_bounded_and_invalid_bytes_are_rejected():
    raw = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"synthetic"
    import base64

    item = {"type": "audio", "mimeType": "audio/wav",
            "data": base64.b64encode(raw).decode("ascii")}
    result = normalize_tool_result({"content": [item, item], "isError": False})
    assert result["content"] == [item]
    assert result["omitted_audio_items"] == 1
    assert result["truncated"] is True
    invalid = {**item, "data": base64.b64encode(b"not-a-wave").decode("ascii")}
    with pytest.raises(ValueError, match="MIME type"):
        normalize_tool_result({"content": [invalid], "isError": False})


def _wav(samples: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(samples)
    return output.getvalue()


async def test_local_transcription_returns_text_without_downloading_model(
    tmp_path, monkeypatch,
):
    from anywhere_computer import media

    source = tmp_path / "audio.wav"
    source.write_bytes(b"placeholder")
    model = tmp_path / "trusted.pt"
    model.write_bytes(b"installed checkpoint")
    executable = tmp_path / "whisper"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "args = sys.argv\n"
        "assert args[args.index('--model') + 1].endswith('trusted.pt')\n"
        "assert '--model_dir' not in args\n"
        "output = pathlib.Path(args[args.index('--output_dir') + 1])\n"
        "(output / 'clip.json').write_text(json.dumps("
        "{'text': '  spoken result  ', 'language': 'en'}))\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(media.shutil, "which", lambda name: str(executable))

    async def decoded(_arguments):
        return _wav(b"\x01\x00" * 1600)

    monkeypatch.setattr(media, "_decode", decoded)
    result = await transcribe(MediaTranscribe(
        path=str(source), model_path=str(model), language="en",
    ))
    assert result["text"] == "spoken result"
    assert result["speech_detected"] is True
    assert result["backend"] == "openai-whisper-cli"
    assert len(result["model_sha256"]) == 64
    assert result["language"] == "en"


async def test_transcription_skips_model_for_digital_silence(tmp_path, monkeypatch):
    from anywhere_computer import media

    source = tmp_path / "audio.wav"
    source.write_bytes(b"placeholder")
    model = tmp_path / "trusted.pt"
    model.write_bytes(b"installed checkpoint")
    monkeypatch.setattr(media.shutil, "which", lambda name: sys.executable)

    async def decoded(_arguments):
        return _wav(b"\x00\x00" * 1600)

    monkeypatch.setattr(media, "_decode", decoded)
    result = await transcribe(MediaTranscribe(path=str(source), model_path=str(model)))
    assert result["speech_detected"] is False
    assert result["text"] == ""


def test_transcription_rejects_missing_model_and_model_symlink(tmp_path):
    from anywhere_computer.media import _checkpoint, _source

    with pytest.raises(FileNotFoundError):
        _checkpoint(str(tmp_path / "missing.pt"))
    model = tmp_path / "trusted.pt"
    model.write_bytes(b"checkpoint")
    link = tmp_path / "linked.pt"
    try:
        link.symlink_to(model)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError, match="existing local"):
        _checkpoint(str(link))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"source")
    audio_link = tmp_path / "audio-link.wav"
    audio_link.symlink_to(audio)
    assert _source(str(audio_link)) == audio_link
    directory_link = tmp_path / "directory-link.wav"
    directory_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="regular file"):
        _source(str(directory_link))


def test_transcription_rejects_unbounded_clips_and_invalid_languages(tmp_path):
    with pytest.raises(ValidationError):
        MediaTranscribe(path=str(tmp_path / "audio.wav"),
                        model_path=str(tmp_path / "trusted.pt"), duration_seconds=11)
    with pytest.raises(ValidationError):
        MediaTranscribe(path=str(tmp_path / "audio.wav"),
                        model_path=str(tmp_path / "trusted.pt"), language="en; rm")


async def test_transcription_failure_does_not_return_backend_stderr(tmp_path, monkeypatch):
    from anywhere_computer import media

    source = tmp_path / "audio.wav"
    source.write_bytes(b"placeholder")
    model = tmp_path / "trusted.pt"
    model.write_bytes(b"installed checkpoint")
    executable = tmp_path / "whisper"
    executable.write_text("#!/bin/sh\necho 'private backend details' >&2\nexit 2\n")
    executable.chmod(0o700)
    monkeypatch.setattr(media.shutil, "which", lambda name: str(executable))

    async def decoded(_arguments):
        return _wav(b"\x01\x00" * 1600)

    monkeypatch.setattr(media, "_decode", decoded)
    with pytest.raises(ValueError, match="Local transcription failed") as failure:
        await transcribe(MediaTranscribe(path=str(source), model_path=str(model), language="xx"))
    assert "private backend details" not in str(failure.value)


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_transcription_reaps_child(tmp_path, monkeypatch, interruption):
    from anywhere_computer import media

    source = tmp_path / "audio.wav"
    source.write_bytes(b"placeholder")
    model = tmp_path / "trusted.pt"
    model.write_bytes(b"installed checkpoint")
    monkeypatch.setattr(media.shutil, "which", lambda name: sys.executable)

    async def decoded(_arguments):
        return _wav(b"\x01\x00" * 1600)

    monkeypatch.setattr(media, "_decode", decoded)
    started = asyncio.Event()
    children = []
    spawn = asyncio.create_subprocess_exec

    async def stalled_transcriber(*arguments, **options):
        child = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **options)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", stalled_transcriber)
    if interruption == "timeout":
        monkeypatch.setattr(media, "_TRANSCRIPTION_TIMEOUT", .01)
    task = asyncio.create_task(transcribe(MediaTranscribe(
        path=str(source), model_path=str(model),
    )))
    try:
        await asyncio.wait_for(started.wait(), 10)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError, match="exceeded"):
                await asyncio.wait_for(task, 10)
        assert children[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()
