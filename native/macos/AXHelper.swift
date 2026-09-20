// SPDX-License-Identifier: MIT
// Experimental native AX provider helper. See docs/GUI-RELIABILITY-2026-09-14.md.
import AppKit
import ApplicationServices
import CryptoKit
import Darwin
import Foundation

private let maxInputBytes = 64 * 1024
private let maxWindows = 128
private let maxTreeNodes = 128
private let maxTreeDepth = 10
private let maxChildrenPerElement = 32
private let maxObservations = 128
private let observationTTL: TimeInterval = 60
private let axMessagingTimeout: Float = 0.25
private let requestDeadlineSeconds: TimeInterval = 3
private let maxLabelCharacters = 512
private let maxValueCharacters = 4096

private struct HelperFailure: Error {
    let code: String
}

private struct RequestDeadline {
    let expiresAt: TimeInterval

    init() {
        expiresAt = uptime() + requestDeadlineSeconds
    }

    func check() throws {
        guard uptime() < expiresAt else {
            throw helperError("deadline_exceeded")
        }
    }
}

private struct ProcessIdentity: Equatable {
    let bundleID: String
    let pid: pid_t
    let launchTime: TimeInterval
}

private struct WindowRecord {
    let handle: Int
    let app: String
    let process: ProcessIdentity
    var element: AXUIElement
    var lastSeenUptime: TimeInterval
}

private struct ObservedElement {
    let element: AXUIElement
    let valueDigest: Data?
}

private func valueDigest(_ value: CFTypeRef?) -> Data? {
    guard let value else { return Data([0]) }
    // Retain only a digest, not every window's potentially large text for the lease.
    if let text = value as? String {
        return Data([1]) + Data(SHA256.hash(data: Data(text.utf8)))
    }
    if let number = value as? NSNumber,
       let encoded = try? JSONSerialization.data(withJSONObject: number,
                                                  options: [.fragmentsAllowed]) {
        return Data([2]) + Data(SHA256.hash(data: encoded))
    }
    return nil // Unsupported values remain observable but cannot be safely replaced.
}

private struct ObservationRecord {
    let id: String
    let app: String
    let windowID: Int
    let process: ProcessIdentity
    let elements: [String: ObservedElement]
    let expiresAtUptime: TimeInterval
}

private struct TraversalState {
    var visited: [AXUIElement] = []
    var nodeCount = 0
    var truncated = false
    var outputBytes = 0
}

private func reserveNodeOutput(_ node: inout [String: Any],
                               state: inout TraversalState) throws -> Bool {
    // Include escaping and per-child punctuation; leave room for reply framing.
    var size = try JSONSerialization.data(withJSONObject: node).count + 4
    if state.outputBytes + size > 48 * 1024 {
        node["value"] = NSNull()
        node["label"] = NSNull()
        node["value_truncated"] = true
        node["label_truncated"] = true
        state.truncated = true
        size = try JSONSerialization.data(withJSONObject: node).count + 4
    }
    if state.outputBytes + size > 48 * 1024 {
        state.truncated = true
        return false
    }
    state.outputBytes += size
    return true
}

private enum LocatedElement {
    case found(AXUIElement)
    case absent
    case limitExceeded
}

private func uptime() -> TimeInterval {
    ProcessInfo.processInfo.systemUptime
}

private func helperError(_ code: String) -> HelperFailure {
    HelperFailure(code: code)
}

private func bounded(_ value: String, limit: Int) -> (String, Bool) {
    guard value.count > limit else { return (value, false) }
    return (String(value.prefix(limit)), true)
}

private func isBooleanNSNumber(_ value: NSNumber) -> Bool {
    CFGetTypeID(value) == CFBooleanGetTypeID()
}

private func positiveInt(_ value: Any?) throws -> Int {
    guard let number = value as? NSNumber, !isBooleanNSNumber(number) else {
        throw helperError("invalid_input")
    }
    let text = number.stringValue
    guard !text.contains("."), !text.lowercased().contains("e"),
          let parsed = Int(text), parsed > 0 else {
        throw helperError("invalid_input")
    }
    return parsed
}

private func nonEmptyString(_ value: Any?, maxBytes: Int = 4096) throws -> String {
    guard let string = value as? String, !string.isEmpty,
          string.utf8.count <= maxBytes else {
        throw helperError("invalid_input")
    }
    return string
}

private func requestedCFValue(_ value: Any?) throws -> CFTypeRef {
    if let string = value as? String {
        guard string.utf8.count <= 32 * 1024 else {
            throw helperError("input_too_large")
        }
        return string as CFString
    }
    if let number = value as? NSNumber {
        if isBooleanNSNumber(number) {
            return number.boolValue ? kCFBooleanTrue : kCFBooleanFalse
        }
        guard number.doubleValue.isFinite else {
            throw helperError("invalid_input")
        }
        return number
    }
    throw helperError("invalid_input")
}

private func jsonScalar(_ value: CFTypeRef?) -> (Any, Bool) {
    guard let value else { return (NSNull(), false) }
    if let string = value as? String {
        let result = bounded(string, limit: maxValueCharacters)
        return (result.0, result.1)
    }
    if let number = value as? NSNumber {
        if isBooleanNSNumber(number) {
            return (number.boolValue, false)
        }
        return (number, false)
    }
    return (NSNull(), false)
}

private func cfElementsEqual(_ lhs: AXUIElement, _ rhs: AXUIElement) -> Bool {
    CFEqual(lhs, rhs)
}

private func copyOptionalAttribute(
    _ element: AXUIElement,
    _ name: CFString
) throws -> CFTypeRef? {
    var value: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(element, name, &value)
    switch status {
    case .success:
        return value
    case .attributeUnsupported, .noValue:
        return nil
    default:
        throw helperError("ax_error")
    }
}

private func stringAttribute(
    _ element: AXUIElement,
    _ name: CFString
) throws -> String? {
    guard let value = try copyOptionalAttribute(element, name) else { return nil }
    guard let string = value as? String else {
        throw helperError("ax_error")
    }
    return string
}

private func boolAttribute(
    _ element: AXUIElement,
    _ name: CFString
) throws -> Bool? {
    guard let value = try copyOptionalAttribute(element, name) else { return nil }
    guard let number = value as? NSNumber, isBooleanNSNumber(number) else {
        throw helperError("ax_error")
    }
    return number.boolValue
}

private func valueIsSettable(_ element: AXUIElement) throws -> Bool {
    var settable = DarwinBoolean(false)
    let status = AXUIElementIsAttributeSettable(
        element,
        kAXValueAttribute as CFString,
        &settable
    )
    switch status {
    case .success:
        return settable.boolValue
    case .attributeUnsupported, .noValue:
        return false
    default:
        throw helperError("ax_error")
    }
}

private struct SetValueEnabledState {
    let value: Bool?
    let state: String
    let axStatus: Int
}

private func enabledStateForSetValue(_ element: AXUIElement) throws -> SetValueEnabledState {
    var rawValue: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(
        element,
        kAXEnabledAttribute as CFString,
        &rawValue
    )
    switch status {
    case .success:
        guard let rawValue,
              let number = rawValue as? NSNumber,
              isBooleanNSNumber(number) else {
            throw helperError("ax_error")
        }
        return SetValueEnabledState(
            value: number.boolValue,
            state: "known",
            axStatus: Int(status.rawValue)
        )
    case .attributeUnsupported:
        return SetValueEnabledState(
            value: nil,
            state: "unknown_attribute_unsupported",
            axStatus: Int(status.rawValue)
        )
    case .noValue:
        return SetValueEnabledState(
            value: nil,
            state: "unknown_no_value",
            axStatus: Int(status.rawValue)
        )
    default:
        throw helperError("ax_error")
    }
}

private func elementPID(_ element: AXUIElement) throws -> pid_t {
    var pid: pid_t = 0
    guard AXUIElementGetPid(element, &pid) == .success, pid > 0 else {
        throw helperError("ax_error")
    }
    return pid
}

private func elementArray(
    _ element: AXUIElement,
    attribute: CFString,
    maxCount: Int
) throws -> ([AXUIElement], Bool) {
    var count: CFIndex = 0
    let countStatus = AXUIElementGetAttributeValueCount(element, attribute, &count)
    switch countStatus {
    case .success:
        break
    case .attributeUnsupported, .noValue:
        return ([], false)
    default:
        throw helperError("ax_error")
    }
    guard count >= 0 else { throw helperError("ax_error") }
    if count == 0 { return ([], false) }

    let requested = min(Int(count), maxCount)
    var values: CFArray?
    let status = AXUIElementCopyAttributeValues(
        element,
        attribute,
        0,
        requested,
        &values
    )
    guard status == .success else {
        throw helperError("ax_error")
    }
    guard let values, let result = values as? [AXUIElement] else {
        throw helperError("ax_error")
    }
    return (result, Int(count) > maxCount)
}

private func labelForElement(
    _ element: AXUIElement,
    deadline: RequestDeadline
) throws -> (String?, Bool, [[String: Any]]) {
    var diagnostics: [[String: Any]] = []
    let candidates: [(String, CFString)] = [
        ("AXTitle", kAXTitleAttribute as CFString),
        ("AXDescription", kAXDescriptionAttribute as CFString),
        ("AXHelp", kAXHelpAttribute as CFString),
    ]

    for (name, attribute) in candidates {
        try deadline.check()
        var rawValue: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, attribute, &rawValue)
        try deadline.check()

        guard status == .success else {
            // Labels are decorative. Preserve the exact AX status, but do not let
            // an unavailable label candidate invalidate an otherwise usable node.
            diagnostics.append([
                "attribute": name,
                "status": Int(status.rawValue),
            ])
            continue
        }
        guard let rawValue else {
            diagnostics.append([
                "attribute": name,
                "status": Int(status.rawValue),
                "issue": "missing_value",
            ])
            continue
        }
        guard let candidate = rawValue as? String else {
            diagnostics.append([
                "attribute": name,
                "status": Int(status.rawValue),
                "issue": "unexpected_type",
            ])
            continue
        }
        guard !candidate.isEmpty else { continue }

        let result = bounded(candidate, limit: maxLabelCharacters)
        return (result.0, result.1, diagnostics)
    }
    return (nil, false, diagnostics)
}

private final class AXHelper {
    private var nextWindowHandle = 1
    private var windows: [Int: WindowRecord] = [:]
    private var observations: [String: ObservationRecord] = [:]
    private let timeoutConfigured: Bool

    init() {
        let systemWide = AXUIElementCreateSystemWide()
        timeoutConfigured =
            AXUIElementSetMessagingTimeout(systemWide, axMessagingTimeout) == .success
    }

    private func requireAXPermission() throws {
        guard timeoutConfigured else { throw helperError("ax_error") }
        guard AXIsProcessTrusted() else {
            throw helperError("accessibility_required")
        }
    }

    private func resolveProcess(_ bundleID: String) throws -> (NSRunningApplication, ProcessIdentity, AXUIElement) {
        let candidates = NSWorkspace.shared.runningApplications.filter {
            $0.bundleIdentifier == bundleID && !$0.isTerminated
        }
        guard !candidates.isEmpty else { throw helperError("process_not_found") }
        guard candidates.count == 1 else { throw helperError("ambiguous_process") }

        let running = candidates[0]
        guard !running.isTerminated, running.processIdentifier > 0,
              let launchDate = running.launchDate else {
            throw helperError("process_identity_unavailable")
        }
        let identity = ProcessIdentity(
            bundleID: bundleID,
            pid: running.processIdentifier,
            launchTime: launchDate.timeIntervalSinceReferenceDate
        )
        let application = AXUIElementCreateApplication(identity.pid)
        return (running, identity, application)
    }

    private func confirmProcess(_ expected: ProcessIdentity) throws -> AXUIElement {
        let (running, current, application) = try resolveProcess(expected.bundleID)
        guard !running.isTerminated, current == expected else {
            throw helperError("process_identity_changed")
        }
        return application
    }

    private func currentWindows(
        _ application: AXUIElement,
        pid: pid_t,
        deadline: RequestDeadline
    ) throws -> [AXUIElement] {
        try deadline.check()
        let result = try elementArray(
            application,
            attribute: kAXWindowsAttribute as CFString,
            maxCount: maxWindows + 1
        )
        try deadline.check()
        guard !result.1, result.0.count <= maxWindows else {
            throw helperError("window_limit_exceeded")
        }
        for window in result.0 {
            try deadline.check()
            guard try elementPID(window) == pid else {
                throw helperError("ax_error")
            }
        }
        return result.0
    }

    private func pruneState() {
        let now = uptime()
        observations = observations.filter { $0.value.expiresAtUptime > now }

        if windows.count > 512 {
            let staleHandles = windows.values
                .sorted { $0.lastSeenUptime < $1.lastSeenUptime }
                .prefix(windows.count - 512)
                .map(\.handle)
            for handle in staleHandles {
                windows.removeValue(forKey: handle)
            }
        }
    }

    private func existingHandle(
        app: String,
        process: ProcessIdentity,
        element: AXUIElement
    ) -> Int? {
        windows.values
            .filter { $0.app == app && $0.process == process && cfElementsEqual($0.element, element) }
            .map(\.handle)
            .min()
    }

    private func recordWindows(
        app: String,
        process: ProcessIdentity,
        elements: [AXUIElement],
        deadline: RequestDeadline
    ) throws -> [[String: Any]] {
        var result: [[String: Any]] = []
        let now = uptime()
        for element in elements {
            try deadline.check()
            let handle: Int
            if let existing = existingHandle(app: app, process: process, element: element) {
                handle = existing
                windows[handle]?.element = element
                windows[handle]?.lastSeenUptime = now
            } else {
                guard nextWindowHandle > 0, nextWindowHandle < Int.max else {
                    throw helperError("internal_state")
                }
                handle = nextWindowHandle
                nextWindowHandle += 1
                windows[handle] = WindowRecord(
                    handle: handle,
                    app: app,
                    process: process,
                    element: element,
                    lastSeenUptime: now
                )
            }

            let title = try stringAttribute(element, kAXTitleAttribute as CFString)
            try deadline.check()
            let role = try stringAttribute(element, kAXRoleAttribute as CFString)
            try deadline.check()
            var item: [String: Any] = [
                "window_id": handle,
                "title": title ?? NSNull(),
                "role": role ?? NSNull(),
            ]
            if let title, title.count > maxLabelCharacters {
                item["title"] = String(title.prefix(maxLabelCharacters))
                item["title_truncated"] = true
            }
            result.append(item)
        }
        return result
    }

    private func windowRecord(_ handle: Int, app: String) throws -> WindowRecord {
        guard let record = windows[handle], record.app == app else {
            throw helperError("window_unavailable")
        }
        return record
    }

    private func revalidateWindow(
        _ record: WindowRecord,
        deadline: RequestDeadline
    ) throws -> AXUIElement {
        try deadline.check()
        let application = try confirmProcess(record.process)
        try deadline.check()
        let current = try currentWindows(application, pid: record.process.pid, deadline: deadline)
        let matches = current.filter { cfElementsEqual($0, record.element) }
        guard matches.count == 1 else {
            throw helperError("window_unavailable")
        }
        return matches[0]
    }

    private func seen(_ element: AXUIElement, in state: TraversalState) -> Bool {
        state.visited.contains { cfElementsEqual($0, element) }
    }

    private func buildNode(
        _ element: AXUIElement,
        depth: Int,
        state: inout TraversalState,
        refs: inout [String: ObservedElement],
        deadline: RequestDeadline
    ) throws -> [String: Any]? {
        try deadline.check()
        if state.nodeCount >= maxTreeNodes {
            state.truncated = true
            return nil
        }
        if seen(element, in: state) {
            return nil
        }
        state.visited.append(element)
        state.nodeCount += 1

        let ref = UUID().uuidString.lowercased()

        let role = try stringAttribute(element, kAXRoleAttribute as CFString)
        try deadline.check()
        let label = try labelForElement(element, deadline: deadline)
        try deadline.check()
        let observedValue = try copyOptionalAttribute(element, kAXValueAttribute as CFString)
        refs[ref] = ObservedElement(element: element, valueDigest: valueDigest(observedValue))
        let value = jsonScalar(observedValue)
        try deadline.check()
        let enabled = try boolAttribute(element, kAXEnabledAttribute as CFString)
        try deadline.check()
        let settable = try valueIsSettable(element)
        try deadline.check()

        var node: [String: Any] = [
            "element_ref": ref,
            "role": role ?? NSNull(),
            "label": label.0 ?? NSNull(),
            "value": value.0,
            "enabled": enabled ?? NSNull(),
            "settable": settable,
            "children": [[String: Any]](),
        ]
        if label.1 { node["label_truncated"] = true }
        if !label.2.isEmpty { node["label_diagnostics"] = label.2 }
        if value.1 { node["value_truncated"] = true }
        if try !reserveNodeOutput(&node, state: &state) {
            refs.removeValue(forKey: ref)
            return nil
        }

        if depth >= maxTreeDepth {
            var childCount: CFIndex = 0
            let status = AXUIElementGetAttributeValueCount(
                element,
                kAXChildrenAttribute as CFString,
                &childCount
            )
            if status == .success && childCount > 0 {
                state.truncated = true
            } else if status != .success && status != .attributeUnsupported && status != .noValue {
                throw helperError("ax_error")
            }
            return node
        }

        let childrenResult = try elementArray(
            element,
            attribute: kAXChildrenAttribute as CFString,
            maxCount: maxChildrenPerElement
        )
        if childrenResult.1 { state.truncated = true }

        var children: [[String: Any]] = []
        for child in childrenResult.0 {
            if state.nodeCount >= maxTreeNodes {
                state.truncated = true
                break
            }
            if let built = try buildNode(
                child, depth: depth + 1, state: &state, refs: &refs, deadline: deadline
            ) {
                children.append(built)
            }
        }
        node["children"] = children
        return node
    }

    private func locateElement(
        root: AXUIElement,
        target: AXUIElement,
        deadline: RequestDeadline
    ) throws -> LocatedElement {
        var visited: [AXUIElement] = []
        var count = 0
        var limitExceeded = false

        func visit(_ element: AXUIElement, depth: Int) throws -> AXUIElement? {
            try deadline.check()
            if cfElementsEqual(element, target) { return element }
            if visited.contains(where: { cfElementsEqual($0, element) }) {
                return nil
            }
            if count >= maxTreeNodes {
                limitExceeded = true
                return nil
            }
            visited.append(element)
            count += 1

            if depth >= maxTreeDepth {
                var childCount: CFIndex = 0
                let status = AXUIElementGetAttributeValueCount(
                    element,
                    kAXChildrenAttribute as CFString,
                    &childCount
                )
                if status == .success && childCount > 0 {
                    limitExceeded = true
                } else if status != .success && status != .attributeUnsupported && status != .noValue {
                    throw helperError("ax_error")
                }
                return nil
            }

            let children = try elementArray(
                element,
                attribute: kAXChildrenAttribute as CFString,
                maxCount: maxChildrenPerElement
            )
            if children.1 { limitExceeded = true }
            for child in children.0 {
                if let found = try visit(child, depth: depth + 1) {
                    return found
                }
                if count >= maxTreeNodes {
                    limitExceeded = true
                    break
                }
            }
            return nil
        }

        if let found = try visit(root, depth: 0) {
            return .found(found)
        }
        return limitExceeded ? .limitExceeded : .absent
    }

    private func windowsResult(app: String, deadline: RequestDeadline) throws -> [String: Any] {
        try deadline.check()
        try requireAXPermission()
        pruneState()
        let (_, identity, application) = try resolveProcess(app)
        try deadline.check()
        let current = try currentWindows(application, pid: identity.pid, deadline: deadline)
        let listed = try recordWindows(
            app: app, process: identity, elements: current, deadline: deadline
        )
        return [
            "process_id": Int(identity.pid),
            "windows": listed,
            "capabilities": [
                "screen_capture": false,
                "keyboard": false,
                "click": false,
                "set_value": true,
            ],
        ]
    }

    private func observeResult(
        app: String, windowID: Int, deadline: RequestDeadline
    ) throws -> [String: Any] {
        try deadline.check()
        try requireAXPermission()
        pruneState()
        guard observations.count < maxObservations else {
            throw helperError("observation_limit_exceeded")
        }

        let record = try windowRecord(windowID, app: app)
        let window = try revalidateWindow(record, deadline: deadline)

        var state = TraversalState()
        var refs: [String: ObservedElement] = [:]
        guard let tree = try buildNode(
            window, depth: 0, state: &state, refs: &refs, deadline: deadline
        ) else {
            throw helperError("tree_limit_exceeded")
        }

        let observationID = UUID().uuidString.lowercased()
        observations[observationID] = ObservationRecord(
            id: observationID,
            app: app,
            windowID: windowID,
            process: record.process,
            elements: refs,
            expiresAtUptime: uptime() + observationTTL
        )
        return [
            "observation_id": observationID,
            "process_id": Int(record.process.pid),
            "window_id": windowID,
            "ttl_seconds": Int(observationTTL),
            "visited_elements": state.nodeCount,
            "truncated": state.truncated,
            "tree": tree,
        ]
    }

    private func setValueResult(
        app: String,
        windowID: Int,
        observationID: String,
        elementRef: String,
        rawValue: Any?,
        deadline: RequestDeadline
    ) throws -> [String: Any] {
        try deadline.check()
        try requireAXPermission()
        pruneState()

        guard let observation = observations[observationID],
              observation.expiresAtUptime > uptime() else {
            observations.removeValue(forKey: observationID)
            throw helperError("observation_unavailable")
        }
        guard observation.app == app, observation.windowID == windowID,
              let observedElement = observation.elements[elementRef] else {
            throw helperError("observation_mismatch")
        }

        // Any mutation can invalidate other snapshots in this helper session.
        // Consume before a possible effect; an ambiguous AX set is never replayable.
        observations.removeAll()

        let requested = try requestedCFValue(rawValue)
        let record = try windowRecord(windowID, app: app)
        guard record.process == observation.process else {
            throw helperError("process_identity_changed")
        }

        let window = try revalidateWindow(record, deadline: deadline)
        let currentElement: AXUIElement
        switch try locateElement(root: window, target: observedElement.element, deadline: deadline) {
        case .found(let element):
            currentElement = element
        case .absent:
            throw helperError("element_unavailable")
        case .limitExceeded:
            throw helperError("tree_limit_exceeded")
        }

        try deadline.check()
        guard try elementPID(currentElement) == record.process.pid else {
            throw helperError("element_unavailable")
        }

        // Recheck process and window identity immediately before the mutation without
        // walking the complete AX tree a second time. The element was found in the
        // current window tree above; enabled/settable are checked last.
        _ = try confirmProcess(record.process)
        try deadline.check()
        let immediateWindow = try revalidateWindow(record, deadline: deadline)
        guard cfElementsEqual(immediateWindow, window) else {
            throw helperError("window_unavailable")
        }
        let enabledState = try enabledStateForSetValue(currentElement)
        if enabledState.value == false {
            throw helperError("element_not_enabled")
        }
        try deadline.check()
        guard try valueIsSettable(currentElement) else {
            throw helperError("value_not_settable")
        }
        let before = try copyOptionalAttribute(currentElement, kAXValueAttribute as CFString)
        guard let expected = observedElement.valueDigest,
              let current = valueDigest(before) else {
            throw helperError("value_not_comparable")
        }
        guard expected == current else { throw helperError("value_changed") }
        // No mutation is attempted after the request budget has expired.
        try deadline.check()

        let status = AXUIElementSetAttributeValue(
            currentElement,
            kAXValueAttribute as CFString,
            requested
        )
        guard status == .success else {
            throw helperError("ax_error")
        }

        let readback = try copyOptionalAttribute(currentElement, kAXValueAttribute as CFString)
        let verified = readback.map { CFEqual($0, requested) } ?? false
        return [
            "process_id": Int(record.process.pid),
            "window_id": windowID,
            "observation_id": observationID,
            "element_ref": elementRef,
            "enabled": enabledState.value.map { $0 as Any } ?? NSNull(),
            "enabled_provided": enabledState.value != nil,
            "enabled_state": enabledState.state,
            "enabled_ax_status": enabledState.axStatus,
            "value_verified": verified,
            "persistence_verified": false,
        ]
    }

    func handle(_ object: [String: Any]) throws -> [String: Any] {
        let deadline = RequestDeadline()
        let allowedKeys: Set<String> = [
            "id", "method", "app", "window_id", "observation_id", "element_ref", "value",
        ]
        guard Set(object.keys).isSubset(of: allowedKeys) else {
            throw helperError("invalid_input")
        }

        let method = try nonEmptyString(object["method"], maxBytes: 64)
        let app = try nonEmptyString(object["app"], maxBytes: 512)

        switch method {
        case "windows":
            guard object["window_id"] == nil, object["observation_id"] == nil,
                  object["element_ref"] == nil, object["value"] == nil else {
                throw helperError("invalid_input")
            }
            return try windowsResult(app: app, deadline: deadline)

        case "observe":
            guard object["observation_id"] == nil, object["element_ref"] == nil,
                  object["value"] == nil else {
                throw helperError("invalid_input")
            }
            let windowID = try positiveInt(object["window_id"])
            return try observeResult(app: app, windowID: windowID, deadline: deadline)

        case "set_value":
            let windowID = try positiveInt(object["window_id"])
            let observationID = try nonEmptyString(object["observation_id"], maxBytes: 128)
            let elementRef = try nonEmptyString(object["element_ref"], maxBytes: 128)
            guard object.keys.contains("value") else {
                throw helperError("invalid_input")
            }
            return try setValueResult(
                app: app,
                windowID: windowID,
                observationID: observationID,
                elementRef: elementRef,
                rawValue: object["value"],
                deadline: deadline
            )

        default:
            throw helperError("unknown_method")
        }
    }
}

private func validResponseID(_ value: Any?) -> Any? {
    if let string = value as? String, !string.isEmpty, string.utf8.count <= 1024 {
        return string
    }
    if let number = value as? NSNumber, number.doubleValue.isFinite {
        return number
    }
    return nil
}

private func emit(_ object: [String: Any]) {
    let data: Data
    do {
        data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    } catch {
        let fallback = #"{"error":{"code":"internal_error"},"id":null}"#
        FileHandle.standardOutput.write(Data((fallback + "\n").utf8))
        return
    }
    if data.count + 1 > 64 * 1024 {
        emit(["id": object["id"] ?? NSNull(),
              "error": ["code": "response_too_large"]])
        return
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

private func emitError(id: Any?, code: String) {
    emit([
        "id": id ?? NSNull(),
        "error": ["code": code],
    ])
}

private func processLine(_ data: Data, helper: AXHelper) {
    guard data.count <= maxInputBytes else {
        emitError(id: nil, code: "input_too_large")
        return
    }

    let root: Any
    do {
        root = try JSONSerialization.jsonObject(with: data, options: [])
    } catch {
        emitError(id: nil, code: "invalid_json")
        return
    }
    guard let object = root as? [String: Any] else {
        emitError(id: nil, code: "invalid_input")
        return
    }
    guard let responseID = validResponseID(object["id"]) else {
        emitError(id: nil, code: "invalid_input")
        return
    }

    do {
        let result = try helper.handle(object)
        emit(["id": responseID, "result": result])
    } catch let failure as HelperFailure {
        emitError(id: responseID, code: failure.code)
    } catch {
        // Never serialize arbitrary exception text or target application data.
        emitError(id: responseID, code: "internal_error")
    }
}

private func runJSONLines() {
    let helper = AXHelper()
    var line = Data()
    var discardingOversizedLine = false
    var buffer = [UInt8](repeating: 0, count: 4096)

    while true {
        let count: Int = buffer.withUnsafeMutableBytes { rawBuffer in
            guard let base = rawBuffer.baseAddress else { return 0 }
            return Darwin.read(STDIN_FILENO, base, rawBuffer.count)
        }
        if count < 0 {
            if errno == EINTR { continue }
            emitError(id: nil, code: "stdin_error")
            return
        }
        if count == 0 {
            if discardingOversizedLine {
                emitError(id: nil, code: "input_too_large")
            } else if !line.isEmpty {
                processLine(line, helper: helper)
            }
            return
        }

        for byte in buffer.prefix(count) {
            if byte == 0x0A {
                if discardingOversizedLine {
                    emitError(id: nil, code: "input_too_large")
                    discardingOversizedLine = false
                    line.removeAll(keepingCapacity: false)
                } else {
                    processLine(line, helper: helper)
                    line.removeAll(keepingCapacity: true)
                }
                continue
            }

            if discardingOversizedLine {
                continue
            }
            if line.count >= maxInputBytes {
                discardingOversizedLine = true
                line.removeAll(keepingCapacity: false)
                continue
            }
            line.append(byte)
        }
    }
}

runJSONLines()
