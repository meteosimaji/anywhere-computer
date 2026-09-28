"""Real bounded media decoding and typed MCP projection."""

import json
import shutil
import subprocess

import pytest
from mcp.types import AudioContent, CallToolResult, ImageContent

from anywhere_computer.mcp_results import normalize_tool_result
from anywhere_computer.mcp_server import _reply_result
from anywhere_computer.media import MediaAudioClip, MediaVideoFrames, audio_clip, video_frames
from anywhere_computer.models import Reply


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
