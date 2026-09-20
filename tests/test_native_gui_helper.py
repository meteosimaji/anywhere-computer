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


def test_serialized_observation_budget_in_real_swift_source(tmp_path):
    source = Path(__file__).resolve().parents[1] / "native/macos/AXHelper.swift"
    text = source.read_text()
    entry = "\nrunJSONLines()\n"
    assert text.endswith(entry)
    # Exercise the actual serialization functions without requiring a GUI app or
    # Accessibility permission on CI; only the executable entry point is replaced.
    harness = text.removesuffix(entry) + r'''
private var state = TraversalState()
var nodes: [[String: Any]] = []
for _ in 0..<128 {
    var node: [String: Any] = ["element_ref": UUID().uuidString,
        "value": String(repeating: "日本語\n\"", count: 4096),
        "label": "field", "children": [[String: Any]]()]
    if try reserveNodeOutput(&node, state: &state) { nodes.append(node) }
}
emit(["id": "bounded", "result": ["nodes": nodes, "truncated": state.truncated]])
emit(["id": "oversized", "result": ["value": String(repeating: "x", count: 70000)]])
'''
    program = tmp_path / "Budget.swift"
    program.write_text(harness)
    executable = tmp_path / "budget"
    subprocess.run(["swiftc", str(program), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    lines = result.stdout.splitlines(keepends=True)
    assert len(lines) == 2 and all(len(line) <= 65536 for line in lines)
    bounded, oversized = map(json.loads, lines)
    assert bounded["result"]["truncated"] is True
    assert len(bounded["result"]["nodes"]) == 128
    assert oversized == {"id": "oversized", "error": {"code": "response_too_large"}}
