"""Read-only inspection of the optional portable macOS audio helper."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from pydantic import JsonValue

OUTPUT_LIMIT = 64 * 1024


async def _query(command: list[str], *, timeout: float = 10) -> object:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def read() -> bytes:
        assert process.stdout is not None
        output = bytearray()
        while chunk := await process.stdout.read(min(8192, OUTPUT_LIMIT + 1 - len(output))):
            output.extend(chunk)
            if len(output) > OUTPUT_LIMIT:
                raise ValueError("Audio helper output exceeds limit")
        if await process.wait() != 0:
            raise ValueError("Audio helper inspection failed")
        return bytes(output)

    try:
        return json.loads(await asyncio.wait_for(read(), timeout))
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


async def inspect_audio() -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"capture_started": False, "capture_tool_available": False}
    if sys.platform != "darwin":
        return {**result, "state": "unsupported", "reason": "macOS helper required"}
    root = Path(sys.prefix).parent
    helper = root / "native" / "anywhere-audio"
    manifest = root / "manifest.json"
    if not helper.is_file() or helper.is_symlink() or not manifest.is_file():
        return {**result, "state": "unavailable", "reason": "Audio helper is not installed"}
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    if (not isinstance(recorded, dict) or not isinstance(recorded.get("files"), dict)
            or recorded["files"].get("native/anywhere-audio")
            != hashlib.sha256(helper.read_bytes()).hexdigest()
            or not os.access(helper, os.X_OK)):
        raise ValueError("Audio helper does not match its portable manifest")
    permissions = await _query([str(helper), "--check"])
    devices = await _query([str(helper), "--list-devices"])
    if (not isinstance(permissions, dict) or permissions.get("state") != "permission_check"
            or permissions.get("capture_started") is not False
            or not all(type(permissions.get(key)) is bool for key in (
                "screen_capture_allowed", "microphone_allowed"))
            or not isinstance(devices, dict) or devices.get("state") != "device_list"
            or devices.get("capture_started") is not False
            or not isinstance(devices.get("devices"), list)):
        raise ValueError("Invalid audio helper inspection response")
    inputs: list[JsonValue] = []
    for device in devices["devices"]:
        if (not isinstance(device, dict) or not all(
                isinstance(device.get(key), str) and 0 < len(device[key]) <= 1024
                for key in ("id", "name"))):
            raise ValueError("Invalid audio input device")
        inputs.append({"id": device["id"], "name": device["name"]})
    return {**result, "state": "available", "devices": inputs,
            "screen_capture_allowed": permissions["screen_capture_allowed"],
            "microphone_allowed": permissions["microphone_allowed"]}
