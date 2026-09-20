"""Real helper transport tests; no Accessibility permission or desktop mutation."""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS AX helper")


@pytest.fixture(scope="module")
def native_gui_helper(tmp_path_factory):
    compiler = shutil.which("swiftc")
    assert compiler is not None, "The native helper requires the macOS Swift toolchain"
    executable = tmp_path_factory.mktemp("native-gui") / "anywhere-gui"
    source = Path(__file__).resolve().parents[1] / "native/macos/AXHelper.swift"
    subprocess.run([compiler, str(source), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    return executable


async def test_persistent_requests_and_recovery_after_invalid_input(native_gui_helper):
    process = await asyncio.create_subprocess_exec(
        str(native_gui_helper), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr

    async def exchange(raw):
        process.stdin.write(raw + b"\n")
        await process.stdin.drain()
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 5))

    request = {"id": "one", "method": "unsupported", "app": "org.anywhere.test"}
    try:
        assert await exchange(json.dumps(request).encode()) == {
            "id": "one", "error": {"code": "unknown_method"},
        }
        assert await exchange(b"x" * 65537) == {
            "id": None, "error": {"code": "input_too_large"},
        }
        assert (await exchange(b"{"))["error"]["code"] == "invalid_json"
        request.update(method="observe", window_id=True)
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.update(method="unsupported")
        request.pop("window_id")
        # Split input is retained until the delimiter, without requiring EOF.
        raw = json.dumps(request).encode()
        process.stdin.write(raw[:10])
        await process.stdin.drain()
        assert (await exchange(raw[10:]))["error"]["code"] == "unknown_method"
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert await process.stderr.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
