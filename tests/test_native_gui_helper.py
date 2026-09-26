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
        request.update(method="press", window_id=1, observation_id="snapshot",
                       element_ref="button", value="must not be accepted")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        for key in ("observation_id", "element_ref", "value"):
            request.pop(key)
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


def test_observation_budget_and_value_identity_in_real_swift_source(tmp_path):
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
emit(["id": "value-identity", "result": [
    "same": valueDigest("日本語 ✅" as CFString) == valueDigest("日本語 ✅" as CFString),
    "changed": valueDigest("before" as CFString) != valueDigest("after" as CFString),
    "typed": valueDigest("1" as CFString) != valueDigest(NSNumber(value: 1)),
    "empty": valueDigest(nil) != valueDigest("" as CFString),
    "unsupported": valueDigest(NSArray(array: [1, 2])) == nil
]])
'''
    program = tmp_path / "Budget.swift"
    program.write_text(harness)
    executable = tmp_path / "budget"
    subprocess.run(["swiftc", str(program), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    lines = result.stdout.splitlines(keepends=True)
    assert len(lines) == 3 and all(len(line) <= 65536 for line in lines)
    bounded, oversized, identity = map(json.loads, lines)
    assert bounded["result"]["truncated"] is True
    assert len(bounded["result"]["nodes"]) == 128
    assert oversized == {"id": "oversized", "error": {"code": "response_too_large"}}

    assert identity["result"] == dict.fromkeys(
        ["same", "changed", "typed", "empty", "unsupported"], True)


def test_process_selection_and_start_identity_in_real_swift_source(tmp_path):
    source = Path(__file__).resolve().parents[1] / "native/macos/AXHelper.swift"
    entry = "\nrunJSONLines()\n"
    text = source.read_text()
    assert text.endswith(entry)
    program = tmp_path / "ProcessIdentity.swift"
    program.write_text(text.removesuffix(entry) + r'''
func selectionError(_ candidates: [String], regular: Set<String>) -> String {
    do {
        _ = try uniqueGUIProcess(candidates) { regular.contains($0) }
        return "none"
    } catch let failure as HelperFailure {
        return failure.code
    } catch {
        return "unexpected"
    }
}
func missingStartError() -> String {
    do {
        _ = try processStart(pid_t.max)
        return "none"
    } catch let failure as HelperFailure {
        return failure.code
    } catch {
        return "unexpected"
    }
}
let first = try processStart(getpid())
let second = try processStart(getpid())
private let identity = ProcessIdentity(bundleID: "fixture", pid: getpid(),
    startSeconds: first.seconds, startMicroseconds: first.microseconds)
emit(["id": "identity", "result": [
    "main_selected": try uniqueGUIProcess(["main", "agent1", "agent2"])
        { $0 == "main" } == "main",
    "single_accessory_selected": try uniqueGUIProcess(["agent"])
        { _ in false } == "agent",
    "no_process": selectionError([], regular: []) == "process_not_found",
    "two_regular": selectionError(["main1", "main2"],
        regular: ["main1", "main2"]) == "ambiguous_process",
    "two_accessory": selectionError(["agent1", "agent2"],
        regular: []) == "ambiguous_process",
    "start_stable": first.seconds > 0 && first == second,
    "missing_pid_rejected": missingStartError() == "process_identity_unavailable",
    "different_pid": identity != ProcessIdentity(bundleID: "fixture", pid: getpid() + 1,
        startSeconds: first.seconds, startMicroseconds: first.microseconds),
    "different_second": identity != ProcessIdentity(bundleID: "fixture", pid: getpid(),
        startSeconds: first.seconds + 1, startMicroseconds: first.microseconds),
    "different_microsecond": identity != ProcessIdentity(bundleID: "fixture", pid: getpid(),
        startSeconds: first.seconds, startMicroseconds: first.microseconds + 1),
]])
''')
    executable = tmp_path / "process-identity"
    subprocess.run(["swiftc", str(program), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    assert json.loads(result.stdout)["result"] == dict.fromkeys(
        ["main_selected", "single_accessory_selected", "no_process", "two_regular",
         "two_accessory", "start_stable", "missing_pid_rejected", "different_pid",
         "different_second", "different_microsecond"], True)
