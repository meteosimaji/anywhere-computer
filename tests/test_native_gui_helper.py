"""Real helper transport tests; no Accessibility permission or desktop mutation."""
import asyncio
import base64
import json
import os
import plistlib
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
        request.update(method="observe_targets")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.update(window_id=1, include_image="true")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.pop("include_image")
        request.update(method="action", observation_id="snapshot", element_ref="button",
                       action="AXUnobservedCustom")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.pop("action")
        request.update(method="press", window_id=1, observation_id="snapshot",
                       element_ref="button", value="must not be accepted")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.pop("element_ref")
        request.pop("value")
        request.update(method="press_target", role="AXButton")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.update(label="Save", element_ref="button")
        assert (await exchange(json.dumps(request).encode()))["error"]["code"] == (
            "invalid_input")
        request.pop("label")
        request.pop("role")
        for key in ("observation_id", "element_ref", "value"):
            request.pop(key, None)
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


@pytest.mark.parametrize('method', ['browser_status', 'browser_reveal'])
async def test_browser_display_rejects_wrong_identity_before_ui_mutation(
    native_gui_helper, method,
):
    import psutil

    process = await asyncio.create_subprocess_exec(
        str(native_gui_helper), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    assert process.stdin and process.stdout
    request = {'id': 'identity', 'method': method, 'app': 'com.google.Chrome',
               'process_id': os.getpid(),
               'process_started': psutil.Process().create_time()}
    try:
        # The exact live test process is not Chrome. A valid PID/start pair
        # must not fall back to activating any app selected by bundle ID.
        process.stdin.write(json.dumps(request).encode() + b'\n')
        await process.stdin.drain()
        result = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert result == {'id': 'identity',
                          'error': {'code': 'process_identity_changed'}}
        request['process_started'] = True
        process.stdin.write(json.dumps(request).encode() + b'\n')
        await process.stdin.drain()
        result = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert result['error']['code'] == 'invalid_input'
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
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


def test_full_observation_target_is_reachable_in_real_swift_traversal(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'native/macos/AXHelper.swift'
    text = source.read_text().removesuffix('\nrunJSONLines()\n')
    # Substitute AX child reads only; run the actual bounded locator on the
    # same broad/deep tree whose first branch the full observer visits first.
    text = text.replace('private func elementArray(', 'private func liveElementArray(', 1)
    harness = text + r'''
private let fixtureElements = (0..<1059).map { AXUIElementCreateApplication(pid_t(10000 + $0)) }
private func elementArray(_ element: AXUIElement, attribute: CFString,
                          maxCount: Int) throws -> ([AXUIElement], Bool) {
    guard let index = fixtureElements.firstIndex(where: { cfElementsEqual($0, element) }) else {
        return ([], false)
    }
    let indices: [Int]
    if index == 0 { indices = Array(1...32) }
    else if index <= 32 {
        indices = Array((33 + (index - 1) * 32)..<(33 + index * 32))
    } else if index == 33 { indices = [1057] }
    else { indices = [] }
    return (indices.prefix(maxCount).map { fixtureElements[$0] }, indices.count > maxCount)
}
private extension AXHelper {
    func fixtureLocate(_ index: Int, breadthFirst: Bool = false) throws -> String {
        switch try locateElement(root: fixtureElements[0], target: fixtureElements[index],
                                 breadthFirst: breadthFirst,
                                 deadline: RequestDeadline()) {
        case .found: return "found"
        case .absent: return "absent"
        case .limitExceeded: return "limit"
        }
    }
}
private let helper = AXHelper()
emit(["id": "reachability", "result": [
    "early_deep_target": try helper.fixtureLocate(1057),
    "late_target": try helper.fixtureLocate(1058),
    "compact_toolbar": try helper.fixtureLocate(32, breadthFirst: true),
    "compact_deep_limit": try helper.fixtureLocate(1057, breadthFirst: true),
]])
'''
    program = tmp_path / 'ObservedTraversal.swift'
    program.write_text(harness)
    executable = tmp_path / 'observed-traversal'
    subprocess.run(['swiftc', str(program), '-o', str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    assert json.loads(result.stdout)['result'] == {
        'early_deep_target': 'found', 'late_target': 'limit',
        'compact_toolbar': 'found', 'compact_deep_limit': 'limit',
    }


def test_semantic_target_selection_in_real_swift_source(tmp_path):
    source = Path(__file__).resolve().parents[1] / "native/macos/AXHelper.swift"
    text = source.read_text()
    entry = "\nrunJSONLines()\n"
    assert text.endswith(entry)
    program = tmp_path / "SemanticTarget.swift"
    program.write_text(text.removesuffix(entry) + r'''
let element = AXUIElementCreateApplication(getpid())
private let targets: [String: ObservedElement] = [
    "save": ObservedElement(element: element, valueDigest: nil,
        pressIdentity: nil, target: SemanticTarget(
            role: "AXButton", label: "Save", identifier: "save-primary")),
    "save-copy": ObservedElement(element: element, valueDigest: nil,
        pressIdentity: nil, target: SemanticTarget(
            role: "AXButton", label: "Save", identifier: "save-copy")),
    "field": ObservedElement(element: element, valueDigest: nil,
        pressIdentity: nil, target: SemanticTarget(
            role: "AXTextField", label: "Name", identifier: "name-field")),
]
func selected(_ role: String, _ label: String?, _ identifier: String?) -> String {
    do {
        return try matchingElementRef(targets, selector: SemanticSelector(
            role: role, label: label, identifier: identifier))
    } catch let failure as HelperFailure {
        return failure.code
    } catch {
        return "unexpected"
    }
}
emit(["id": "targets", "result": [
    "exact_identifier": selected("AXButton", nil, "save-primary"),
    "exact_label_and_identifier": selected("AXButton", "Save", "save-copy"),
    "duplicate_label": selected("AXButton", "Save", nil),
    "changed_role": selected("AXLink", "Save", "save-primary"),
    "missing_identifier": selected("AXButton", nil, "missing"),
    "field": selected("AXTextField", "Name", nil),
]])
''')
    executable = tmp_path / "semantic-target"
    subprocess.run(["swiftc", str(program), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    assert json.loads(result.stdout)["result"] == {
        "exact_identifier": "save",
        "exact_label_and_identifier": "save-copy",
        "duplicate_label": "target_ambiguous",
        "changed_role": "target_not_found",
        "missing_identifier": "target_not_found",
        "field": "field",
    }


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


def test_ax_failure_serialization_contains_only_sanitized_metadata(tmp_path):
    source = Path(__file__).resolve().parents[1] / "native/macos/AXHelper.swift"
    text = source.read_text()
    entry = "\nrunJSONLines()\n"
    assert text.endswith(entry)
    program = tmp_path / "AXDiagnostic.swift"
    program.write_text(text.removesuffix(entry) + r'''
private let failure = helperError("ax_error", stage: "copy_attribute",
                          attribute: diagnosticAttribute(kAXValueAttribute as CFString),
                          axStatus: .cannotComplete)
emitError(id: "safe", code: failure.code, failure: failure)
emit(["id": "unknown", "result": [
    "attribute": diagnosticAttribute("private window title" as CFString)]])
var otherFailureStillFails = false
do {
    _ = try observedValueResult(.cannotComplete, nil)
} catch let failure as HelperFailure {
    otherFailureStillFails = failure.code == "ax_error"
        && failure.attribute == "AXValue" && failure.axStatus == -25204
}
var mutationPressFailureStillFails = false
do {
    _ = try pressActionAvailable(.failure, readOnly: false)
} catch let failure as HelperFailure {
    mutationPressFailureStillFails = failure.code == "ax_error"
        && failure.stage == "copy_actions" && failure.axStatus == -25200
}
emit(["id": "optional-value", "result": [
    "failure_absent": try observedValueResult(.failure, nil).value == nil,
    "failure_uncomparable": try observedValueResult(.failure, nil).comparable == false,
    "no_value_absent": try observedValueResult(.noValue, nil).value == nil,
    "no_value_comparable": try observedValueResult(.noValue, nil).comparable,
    "success_preserved": (try observedValueResult(.success, "visible" as CFString).value)
        .map { CFEqual($0, "visible" as CFString) } ?? false,
    "other_failure_still_fails": otherFailureStillFails,
    "failure_not_settable": try observedValueSettableResult(.failure, true) == false,
    "success_settable": try observedValueSettableResult(.success, true),
    "failure_not_pressable": try pressActionAvailable(.failure, readOnly: true) == false,
    "mutation_press_failure_still_fails": mutationPressFailureStillFails,
]])
''')
    executable = tmp_path / "ax-diagnostic"
    subprocess.run(["swiftc", str(program), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines == [
        {"id": "safe", "error": {"code": "ax_error", "stage": "copy_attribute",
                                 "attribute": "AXValue", "ax_status": -25204}},
        {"id": "unknown", "result": {"attribute": "other"}},
        {"id": "optional-value", "result": {
            "failure_absent": True, "failure_uncomparable": True,
            "no_value_absent": True, "no_value_comparable": True,
            "success_preserved": True, "other_failure_still_fails": True,
            "failure_not_settable": True, "success_settable": True,
            "failure_not_pressable": True, "mutation_press_failure_still_fails": True,
        }},
    ]
    assert b"private window title" not in result.stdout


def test_window_capture_mapping_and_action_revalidation_in_real_swift_source(tmp_path):
    source = Path(__file__).resolve().parents[1] / "native/macos/AXHelper.swift"
    text = source.read_text()
    program = tmp_path / "CaptureIdentity.swift"
    program.write_text(text.removesuffix("\nrunJSONLines()\n") + r'''
let bounds = CGRect(x: -120, y: 40, width: 800, height: 600)
func match(_ pid: pid_t, _ frame: CGRect, _ title: String?) -> Bool {
    captureMatches(pid: pid, frame: frame, title: title,
                   expectedPID: 42, expectedFrame: bounds, expectedTitle: "Fixture")
}
func checkAction(_ before: Data?, _ after: Data?, _ press: Bool = false) -> String {
    do {
        try validateObservedAction(before, after, press: press)
        return "accepted"
    } catch let failure as HelperFailure { return failure.code }
    catch { return "unexpected" }
}
let surface = CGContext(data: nil, width: 120, height: 80, bitsPerComponent: 8,
                        bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
surface.setFillColor(NSColor.systemRed.cgColor)
surface.fill(CGRect(x: 0, y: 0, width: 120, height: 80))
let encoded = try encodedCapture(surface.makeImage()!)
emit(["id": "checks", "result": [
    "exact_window": match(42, bounds, "Fixture"),
    "other_process": !match(43, bounds, "Fixture"),
    "other_title": !match(42, bounds, "Other"),
    "missing_title": !match(42, bounds, nil),
    "moved_window": !match(42, bounds.offsetBy(dx: 1, dy: 0), "Fixture"),
    "invalid_bounds": !validBounds(CGRect(x: 0, y: 0, width: 0, height: 5)),
    "negative_origin_supported": validBounds(bounds),
    "jpeg_header": encoded.starts(with: [0xff, 0xd8, 0xff]),
    "bounded_encoding": encoded.count > 0 && encoded.count <= maxImageBytes,
    "bounds": boundsJSON(bounds),
    "unchanged_action": checkAction(Data([1]), Data([1])),
    "unobserved_action": checkAction(nil, Data([1])),
    "removed_action": checkAction(Data([1]), nil),
    "changed_action": checkAction(Data([1]), Data([2])),
    "press_compatibility": checkAction(nil, Data([1]), true),
    "actions": supportedActions.sorted(),
]])
''')
    executable = tmp_path / "capture-identity"
    subprocess.run(["swiftc", str(program), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    result = subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
    checked = json.loads(result.stdout)["result"]
    for name in ("exact_window", "other_process", "other_title", "missing_title", "moved_window",
                 "invalid_bounds", "negative_origin_supported", "jpeg_header", "bounded_encoding"):
        assert checked[name] is True
    assert checked["bounds"] == {
        "x": -120, "y": 40, "width": 800, "height": 600,
        "coordinate_unit": "point", "origin": "global_top_left",
    }
    assert checked["unchanged_action"] == "accepted"
    assert checked["unobserved_action"] == "action_not_observed"
    assert checked["removed_action"] == checked["changed_action"] == "action_target_changed"
    assert checked["press_compatibility"] == "press_target_changed"
    from anywhere_computer.native_gui import NativeAction
    assert checked["actions"] == sorted(
        NativeAction.model_json_schema()["properties"]["action"]["enum"])


@pytest.mark.skipif(os.environ.get("ANYWHERE_NATIVE_GUI_ACCEPTANCE") != "1",
                    reason="Opt-in disposable GUI acceptance requires existing AX/capture grants")
async def test_live_exact_window_image_and_secondary_action(
    native_gui_helper, tmp_path, monkeypatch,
):
    """Opt in on a GUI host; no OS permission dialog or user app is opened."""
    from anywhere_computer import native_gui
    from anywhere_computer.mcp_server import _reply_result
    from anywhere_computer.models import Reply

    bundle = tmp_path / "Anywhere Native Fixture.app" / "Contents"
    executable = bundle / "MacOS" / "Fixture"
    executable.parent.mkdir(parents=True)
    (bundle / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "Fixture", "CFBundleIdentifier": "org.anywherecomputer.NativeFixture",
        "CFBundleName": "Anywhere Native Fixture", "CFBundlePackageType": "APPL",
        "NSHighResolutionCapable": True,
    }))
    source = Path(__file__).parent / "fixtures/native_gui.swift"
    subprocess.run(["swiftc", str(source), "-o", str(executable)],
                   check=True, capture_output=True, timeout=90)
    monkeypatch.setattr(native_gui, "installed_helper", lambda: native_gui_helper)
    process = await asyncio.create_subprocess_exec(str(executable),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    gui = native_gui.NativeGUI()

    def nodes(node):
        yield node
        for child in node.get("children", []):
            yield from nodes(child)

    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
        opened = await gui.windows(native_gui.NativeApp(app="org.anywherecomputer.NativeFixture"),
                                   owner="fixture")
        titles = {window["title"] for window in opened["windows"]}
        assert titles == {"Anywhere Native Fixture Red", "Anywhere Native Fixture Blue"}
        selected = next(window for window in opened["windows"]
                        if window["title"] == "Anywhere Native Fixture Red")
        target = {"session_id": opened["session_id"], "app": "org.anywherecomputer.NativeFixture",
                  "window_id": selected["window_id"]}
        observed = await gui.observe(native_gui.NativeObserve(**target, include_image=True),
                                     owner="fixture")
        assert "visual_unavailable" not in observed, observed.get("visual_unavailable")
        assert observed["visual"]["window_id"] == selected["window_id"]
        assert observed["visual"]["observation_id"] == observed["observation_id"]
        assert observed["visual"]["bounds"] == observed["tree"]["bounds"]
        picture = tmp_path / "selected-window.jpg"
        picture.write_bytes(base64.b64decode(observed["content"][0]["data"], validate=True))
        pixels = json.loads(subprocess.check_output([str(executable), str(picture)], timeout=5))
        # The capture carries the display ICC profile and is encoded as JPEG;
        # test the selected red window's hue, not device-independent RGB equality.
        assert pixels["red"] > 0.9 and pixels["red"] - pixels["blue"] > 0.7
        assert pixels["width"] == observed["visual"]["width"]
        assert pixels["height"] == observed["visual"]["height"]
        projected = _reply_result("gui_native_observe", Reply(
            operation_id="a" * 32, state="completed", data=observed))
        assert projected["content"][1] == observed["content"][0]
        assert observed["content"][0]["data"] not in projected["content"][0]["text"]
        stepper = next(node for node in nodes(observed["tree"])
                       if node.get("identifier") == "fixture-stepper")
        assert stepper["value"] == 0 and "AXIncrement" in stepper["actions"]
        action = native_gui.NativeAction(**target, observation_id=observed["observation_id"],
                                        element_ref=stepper["element_ref"], action="AXIncrement")
        result = await gui.action(action, owner="fixture")
        assert result["action_accepted"] and result["postcondition_verified"] is False
        with pytest.raises(ValueError, match="observation unavailable"):
            await gui.action(action, owner="fixture")
        refreshed = await gui.observe(native_gui.NativeObserve(**target), owner="fixture")
        current = next(node for node in nodes(refreshed["tree"])
                       if node.get("identifier") == "fixture-stepper")
        assert current["value"] == 1
        process.stdin.write(b'broad\n')
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b'broad\n'
        broad = await gui.observe(native_gui.NativeObserve(**target), owner='fixture')
        deep = next(node for node in nodes(broad['tree'])
                    if node.get('identifier') == 'fixture-deep')
        assert deep['value'] == 0
        await gui.press(native_gui.NativePress(
            **target, observation_id=broad['observation_id'], element_ref=deep['element_ref']),
            owner='fixture')
        changed = await gui.observe(native_gui.NativeObserve(**target), owner='fixture')
        assert next(node for node in nodes(changed['tree'])
                    if node.get('identifier') == 'fixture-deep')['value'] == 1
        field = next(node for node in nodes(changed['tree'])
                     if node.get('identifier') == 'fixture-deep-field')
        await gui.set_value(native_gui.NativeSetValue(
            **target, observation_id=changed['observation_id'], element_ref=field['element_ref'],
            value='after'), owner='fixture')
        changed = await gui.observe(native_gui.NativeObserve(**target), owner='fixture')
        assert next(node for node in nodes(changed['tree'])
                    if node.get('identifier') == 'fixture-deep-field')['value'] == 'after'
        compact = await gui.observe(
            native_gui.NativeObserve(**target, compact=True), owner='fixture')
        assert any(node.get('identifier') == 'fixture-toolbar' for node in compact['targets']), {
            'visited': compact['visited_elements'], 'truncated': compact['truncated'],
            'targets': [(node.get('identifier'), node.get('label'), node.get('role'))
                        for node in compact['targets']],
        }
        toolbar = next(node for node in compact['targets']
                       if node.get('identifier') == 'fixture-toolbar')
        await gui.press(native_gui.NativePress(
            **target, observation_id=compact['observation_id'], element_ref=toolbar['element_ref']),
            owner='fixture')
        process.stdin.write(b'reveal-toolbar\n')
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b'revealed\n'
        changed = await gui.observe(native_gui.NativeObserve(**target), owner='fixture')
        assert next(node for node in nodes(changed['tree'])
                    if node.get('identifier') == 'fixture-toolbar')['value'] == 1
        process.stdin.write(b"ambiguous\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ambiguous\n"
        ambiguous = await gui.observe(native_gui.NativeObserve(**target, include_image=True),
                                      owner="fixture")
        assert ambiguous["visual_unavailable"] == "capture_window_ambiguous"
        assert "content" not in ambiguous
    finally:
        await gui.close()
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
