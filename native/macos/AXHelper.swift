// SPDX-License-Identifier: MIT
// Experimental native AX provider helper. See docs/GUI-MCP.md.
import AppKit
import ApplicationServices
import CryptoKit
import Darwin
import Foundation
import ScreenCaptureKit

private let maxInputBytes = 64 * 1024
private let maxWindows = 128
private let maxTreeNodes = 128
private let maxTreeDepth = 10
private let maxChildrenPerElement = 32
private let maxSelectorScanNodes = 1024
private let maxSelectorScanDepth = 16
private let maxSelectorChildrenPerElement = 256
private let maxObservations = 128
private let observationTTL: TimeInterval = 60
private let axMessagingTimeout: Float = 0.25
private let requestDeadlineSeconds: TimeInterval = 3
private let maxLabelCharacters = 512
private let maxValueCharacters = 4096
private let maxImageBytes = 2 * 1024 * 1024
private let supportedActions: Set<String> = [
    "AXPress", "AXIncrement", "AXDecrement", "AXConfirm", "AXCancel", "AXShowMenu",
    "AXPick", "AXScrollUpByPage", "AXScrollDownByPage", "AXScrollLeftByPage",
    "AXScrollRightByPage",
]

private struct HelperFailure: Error {
    let code: String
    let stage: String?
    let attribute: String?
    let axStatus: Int?
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

    var remaining: TimeInterval { max(0, expiresAt - uptime()) }
}

private struct ProcessIdentity: Equatable {
    let bundleID: String
    let pid: pid_t
    let startSeconds: UInt64
    let startMicroseconds: UInt64
}

private func processStart(_ pid: pid_t) throws -> (seconds: UInt64, microseconds: UInt64) {
    var info = proc_bsdinfo()
    let size = withUnsafeMutablePointer(to: &info) { pointer in
        proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, pointer,
                     Int32(MemoryLayout<proc_bsdinfo>.size))
    }
    guard size == MemoryLayout<proc_bsdinfo>.size, info.pbi_pid == pid,
          info.pbi_start_tvsec > 0, info.pbi_start_tvusec < 1_000_000 else {
        throw helperError("process_identity_unavailable")
    }
    return (info.pbi_start_tvsec, info.pbi_start_tvusec)
}

private func uniqueGUIProcess<T>(_ candidates: [T], isRegular: (T) -> Bool) throws -> T {
    guard !candidates.isEmpty else { throw helperError("process_not_found") }
    let regular = candidates.filter(isRegular)
    let selected = regular.isEmpty ? candidates : regular
    guard selected.count == 1 else { throw helperError("ambiguous_process") }
    return selected[0]
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
    let pressIdentity: Data?
    let target: SemanticTarget
    var actions: [String: Data] = [:]
}

private struct SemanticTarget {
    let role: String?
    let label: String?
    let identifier: String?
}

private struct SemanticSelector {
    let role: String
    let label: String?
    let identifier: String?

    func matches(_ target: SemanticTarget) -> Bool {
        target.role == role && (label == nil || target.label == label)
            && (identifier == nil || target.identifier == identifier)
    }
}

private func matchingElementRef(
    _ elements: [String: ObservedElement], selector: SemanticSelector
) throws -> String {
    let matches = elements.filter { selector.matches($0.value.target) }
    guard !matches.isEmpty else { throw helperError("target_not_found") }
    guard matches.count == 1, let reference = matches.first?.key else {
        throw helperError("target_ambiguous")
    }
    return reference
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

// The action and its visible identity must be observed, then rechecked before dispatch.
private func pressActionAvailable(_ status: AXError, readOnly: Bool) throws -> Bool {
    if status == .attributeUnsupported || status == .noValue || status == .actionUnsupported
        || (readOnly && status == .failure) {
        return false
    }
    guard status == .success else {
        throw helperError("ax_error", stage: "copy_actions", axStatus: status)
    }
    return true
}

private func pressIdentity(_ element: AXUIElement, readOnly: Bool = false) throws -> Data? {
    try actionIdentities(element, readOnly: readOnly)["AXPress"]
}

private func actionIdentities(
    _ element: AXUIElement, readOnly: Bool = false
) throws -> [String: Data] {
    var raw: CFArray?
    let status = AXUIElementCopyActionNames(element, &raw)
    if try !pressActionAvailable(status, readOnly: readOnly) {
        return [:]
    }
    guard let actions = raw as? [String] else { return [:] }
    let allowed = actions.filter { supportedActions.contains($0) }
    guard !allowed.isEmpty else {
        return [:]
    }
    let attributes = [kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
                      kAXHelpAttribute, kAXIdentifierAttribute]
    let identity: [String?] = try attributes.map { try stringAttribute(element, $0 as CFString) }
    let data = try JSONEncoder().encode(identity)
    let digest = Data(SHA256.hash(data: data))
    return Dictionary(allowed.map { ($0, digest) }, uniquingKeysWith: { first, _ in first })
}

private func validateObservedAction(_ observed: Data?, _ current: Data?, press: Bool) throws {
    guard let observed else {
        throw helperError(press ? "press_target_changed" : "action_not_observed")
    }
    guard let current, observed == current else {
        throw helperError(press ? "press_target_changed" : "action_target_changed")
    }
}

private func elementBounds(_ element: AXUIElement) -> CGRect? {
    var rawPosition: CFTypeRef?
    var rawSize: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString,
                                        &rawPosition) == .success,
          AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString,
                                        &rawSize) == .success,
          let rawPosition, let rawSize,
          CFGetTypeID(rawPosition) == AXValueGetTypeID(),
          CFGetTypeID(rawSize) == AXValueGetTypeID() else { return nil }
    var position = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(rawPosition as! AXValue, .cgPoint, &position),
          AXValueGetValue(rawSize as! AXValue, .cgSize, &size) else { return nil }
    let bounds = CGRect(origin: position, size: size)
    return validBounds(bounds) ? bounds : nil
}

private func validBounds(_ bounds: CGRect) -> Bool {
    [bounds.minX, bounds.minY, bounds.width, bounds.height].allSatisfy { $0.isFinite }
        && bounds.width > 0 && bounds.height > 0
        && bounds.width <= 32768 && bounds.height <= 32768
}

private func boundsJSON(_ bounds: CGRect) -> [String: Any] {
    ["x": bounds.minX, "y": bounds.minY, "width": bounds.width, "height": bounds.height,
     "coordinate_unit": "point", "origin": "global_top_left"]
}

// Public AX does not expose the CoreGraphics window ID. Require a unique
// same-process, same-frame, same-title capture window; never guess by z-order.
private func captureMatches(pid: pid_t, frame: CGRect, title: String?,
                            expectedPID: pid_t, expectedFrame: CGRect,
                            expectedTitle: String?) -> Bool {
    pid == expectedPID && frame == expectedFrame && validBounds(frame)
        && title == expectedTitle
}

private final class CaptureResult<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T?
    private var failed = false
    let ready = DispatchSemaphore(value: 0)

    func complete(_ value: T?, failed: Bool) {
        lock.lock()
        self.value = value
        self.failed = failed
        lock.unlock()
        ready.signal()
    }

    func wait(_ deadline: RequestDeadline) throws -> T {
        guard ready.wait(timeout: .now() + deadline.remaining) == .success else {
            throw helperError("deadline_exceeded")
        }
        lock.lock()
        defer { lock.unlock() }
        guard !failed, let value else { throw helperError("capture_failed") }
        return value
    }
}

@available(macOS 14.0, *)
private func captureContent(_ deadline: RequestDeadline) throws -> SCShareableContent {
    let result = CaptureResult<SCShareableContent>()
    SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) {
        content, error in result.complete(content, failed: error != nil)
    }
    return try result.wait(deadline)
}

private func encodedCapture(_ image: CGImage) throws -> Data {
    let bitmap = NSBitmapImageRep(cgImage: image)
    for quality in [0.75, 0.5] {
        if let encoded = bitmap.representation(using: .jpeg,
                                               properties: [.compressionFactor: quality]),
           encoded.count <= maxImageBytes {
            return encoded
        }
    }
    throw helperError("image_too_large")
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
        node["identifier"] = NSNull()
        node["value_truncated"] = true
        node["label_truncated"] = true
        node["identifier_truncated"] = true
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

private func helperError(
    _ code: String, stage: String? = nil, attribute: String? = nil,
    axStatus: AXError? = nil
) -> HelperFailure {
    HelperFailure(code: code, stage: stage, attribute: attribute,
                  axStatus: axStatus.map { Int($0.rawValue) })
}

private func diagnosticAttribute(_ name: CFString) -> String {
    // Do not serialize application supplied attribute names.
    let known: [String] = [
        kAXWindowsAttribute, kAXChildrenAttribute, kAXRoleAttribute,
        kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute,
        kAXIdentifierAttribute, kAXValueAttribute, kAXEnabledAttribute,
    ]
    let candidate = name as String
    return known.contains(candidate) ? candidate : "other"
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
        throw helperError("ax_error", stage: "copy_attribute",
                          attribute: diagnosticAttribute(name), axStatus: status)
    }
}

private struct ReadOnlyValue {
    let value: CFTypeRef?
    let comparable: Bool
}

private func observedValue(_ element: AXUIElement) throws -> ReadOnlyValue {
    var value: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(
        element, kAXValueAttribute as CFString, &value
    )
    return try observedValueResult(status, value)
}

private func observedValueResult(_ status: AXError, _ value: CFTypeRef?) throws -> ReadOnlyValue {
    switch status {
    case .success:
        return ReadOnlyValue(value: value, comparable: true)
    case .attributeUnsupported, .noValue:
        return ReadOnlyValue(value: nil, comparable: true)
    case .failure:
        // Finder can return a generic failure for AXValue on an otherwise
        // usable child. Keep it visible, but never infer that its value is absent.
        return ReadOnlyValue(value: nil, comparable: false)
    default:
        throw helperError("ax_error", stage: "copy_attribute",
                          attribute: "AXValue", axStatus: status)
    }
}

private func stringAttribute(
    _ element: AXUIElement,
    _ name: CFString
) throws -> String? {
    guard let value = try copyOptionalAttribute(element, name) else { return nil }
    guard let string = value as? String else {
        throw helperError("ax_error", stage: "attribute_type",
                          attribute: diagnosticAttribute(name))
    }
    return string
}

private func identifierForElement(_ element: AXUIElement) -> (String?, Bool) {
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXIdentifierAttribute as CFString,
                                        &raw) == .success,
          let identifier = raw as? String, !identifier.isEmpty else {
        return (nil, false)
    }
    guard identifier.count <= maxLabelCharacters else { return (nil, true) }
    return (identifier, false)
}

private func boolAttribute(
    _ element: AXUIElement,
    _ name: CFString
) throws -> Bool? {
    guard let value = try copyOptionalAttribute(element, name) else { return nil }
    guard let number = value as? NSNumber, isBooleanNSNumber(number) else {
        throw helperError("ax_error", stage: "attribute_type",
                          attribute: diagnosticAttribute(name))
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
        throw helperError("ax_error", stage: "is_settable",
                          attribute: "AXValue", axStatus: status)
    }
}

private func observedValueIsSettable(_ element: AXUIElement) throws -> Bool {
    var settable = DarwinBoolean(false)
    let status = AXUIElementIsAttributeSettable(
        element, kAXValueAttribute as CFString, &settable
    )
    return try observedValueSettableResult(status, settable.boolValue)
}

private func observedValueSettableResult(_ status: AXError, _ settable: Bool) throws -> Bool {
    switch status {
    case .success:
        return settable
    case .attributeUnsupported, .noValue, .failure:
        return false
    default:
        throw helperError("ax_error", stage: "is_settable",
                          attribute: "AXValue", axStatus: status)
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
            throw helperError("ax_error", stage: "attribute_type",
                              attribute: "AXEnabled")
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
        throw helperError("ax_error", stage: "copy_attribute",
                          attribute: "AXEnabled", axStatus: status)
    }
}

private func elementPID(_ element: AXUIElement) throws -> pid_t {
    var pid: pid_t = 0
    let status = AXUIElementGetPid(element, &pid)
    guard status == .success, pid > 0 else {
        throw helperError("ax_error", stage: "get_pid", axStatus: status)
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
        throw helperError("ax_error", stage: "count_attribute",
                          attribute: diagnosticAttribute(attribute), axStatus: countStatus)
    }
    guard count >= 0 else {
        throw helperError("ax_error", stage: "count_attribute",
                          attribute: diagnosticAttribute(attribute), axStatus: countStatus)
    }
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
        throw helperError("ax_error", stage: "copy_attribute_values",
                          attribute: diagnosticAttribute(attribute), axStatus: status)
    }
    guard let values, let result = values as? [AXUIElement] else {
        throw helperError("ax_error", stage: "attribute_type",
                          attribute: diagnosticAttribute(attribute))
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
        guard timeoutConfigured else {
            throw helperError("ax_error", stage: "set_timeout")
        }
        guard AXIsProcessTrusted() else {
            throw helperError("accessibility_required")
        }
    }

    private func resolveProcess(_ bundleID: String) throws -> (NSRunningApplication, ProcessIdentity, AXUIElement) {
        let candidates = NSWorkspace.shared.runningApplications.filter {
            $0.bundleIdentifier == bundleID && !$0.isTerminated
        }
        // A GUI app can share its bundle ID with background helper processes.
        let running = try uniqueGUIProcess(candidates) { $0.activationPolicy == .regular }
        guard !running.isTerminated, running.processIdentifier > 0 else {
            throw helperError("process_identity_unavailable")
        }
        let pid = running.processIdentifier
        let start = try processStart(pid)
        // Reject a PID that exited or was reused between workspace enumeration
        // and the kernel start-time read, before retaining an AX element for it.
        guard !running.isTerminated,
              let current = NSRunningApplication(processIdentifier: pid),
              !current.isTerminated, current.bundleIdentifier == bundleID,
              current.activationPolicy == running.activationPolicy,
              try processStart(pid) == start else {
            throw helperError("process_identity_changed")
        }
        let identity = ProcessIdentity(
            bundleID: bundleID,
            pid: pid,
            startSeconds: start.seconds,
            startMicroseconds: start.microseconds
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
        var windows = result.0
        // Some macOS apps expose their visible window through AXFocusedWindow or
        // AXMainWindow while AXWindows is an empty array. Use those app-owned
        // references only in that case so a focused dialog remains observable.
        if windows.isEmpty {
            for attribute in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
                try deadline.check()
                var raw: CFTypeRef?
                let status = AXUIElementCopyAttributeValue(application,
                                                           attribute as CFString, &raw)
                switch status {
                case .success:
                    guard let raw, CFGetTypeID(raw) == AXUIElementGetTypeID() else {
                        throw helperError("ax_error", stage: "attribute_type",
                                          attribute: diagnosticAttribute(attribute as CFString))
                    }
                    let window = raw as! AXUIElement
                    guard try stringAttribute(window, kAXRoleAttribute as CFString)
                        == (kAXWindowRole as String) else { continue }
                    if !windows.contains(where: { cfElementsEqual($0, window) }) {
                        windows.append(window)
                    }
                case .attributeUnsupported, .noValue, .failure:
                    continue
                default:
                    throw helperError("ax_error", stage: "copy_attribute",
                                      attribute: diagnosticAttribute(attribute as CFString),
                                      axStatus: status)
                }
            }
        }
        for window in windows {
            try deadline.check()
            guard try elementPID(window) == pid else {
                throw helperError("ax_error", stage: "verify_pid")
            }
        }
        return windows
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
        let observedValue = try observedValue(element)
        let actions = try actionIdentities(element, readOnly: true)
        let press = actions["AXPress"]
        let digest = observedValue.comparable ? valueDigest(observedValue.value) : nil
        let value = jsonScalar(observedValue.value)
        try deadline.check()
        let enabled = try boolAttribute(element, kAXEnabledAttribute as CFString)
        try deadline.check()
        let settable = try observedValueIsSettable(element)
        try deadline.check()
        let identifier: (String?, Bool)
        if (!actions.isEmpty && digest != nil) || settable {
            identifier = identifierForElement(element)
            try deadline.check()
        } else {
            identifier = (nil, false)
        }

        var node: [String: Any] = [
            "element_ref": ref,
            "role": role ?? NSNull(),
            "label": label.0 ?? NSNull(),
            "identifier": identifier.0 ?? NSNull(),
            "value": value.0,
            "enabled": enabled ?? NSNull(),
            "settable": settable,
            "pressable": press != nil && digest != nil,
            "actions": digest == nil ? [] : actions.keys.sorted(),
            "children": [[String: Any]](),
        ]
        if let bounds = elementBounds(element) { node["bounds"] = boundsJSON(bounds) }
        if label.1 { node["label_truncated"] = true }
        if identifier.1 {
            node["identifier_truncated"] = true
        }
        if !label.2.isEmpty { node["label_diagnostics"] = label.2 }
        if value.1 { node["value_truncated"] = true }
        if try !reserveNodeOutput(&node, state: &state) {
            return nil
        }
        refs[ref] = ObservedElement(
            element: element, valueDigest: digest, pressIdentity: press,
            target: SemanticTarget(role: node["role"] as? String,
                                   label: label.1 ? nil : node["label"] as? String,
                                   identifier: node["identifier"] as? String),
            actions: actions
        )

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
                throw helperError("ax_error", stage: "count_attribute",
                                  attribute: "AXChildren", axStatus: status)
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
        var limitExceeded = false
        var queue: [(AXUIElement, Int)] = [(root, 0)]
        var cursor = 0
        while cursor < queue.count {
            try deadline.check()
            let (element, depth) = queue[cursor]
            cursor += 1
            if cfElementsEqual(element, target) { return .found(element) }
            if visited.contains(where: { cfElementsEqual($0, element) }) {
                continue
            }
            if visited.count >= maxTreeNodes {
                limitExceeded = true
                break
            }
            visited.append(element)
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
                    throw helperError("ax_error", stage: "count_attribute",
                                      attribute: "AXChildren", axStatus: status)
                }
                continue
            }
            let children = try elementArray(
                element,
                attribute: kAXChildrenAttribute as CFString,
                maxCount: maxChildrenPerElement
            )
            if children.1 { limitExceeded = true }
            queue.append(contentsOf: children.0.map { ($0, depth + 1) })
        }
        if cursor < queue.count { limitExceeded = true }
        return limitExceeded ? .limitExceeded : .absent
    }

    private func verifyUniqueSelector(
        root: AXUIElement, selected: AXUIElement,
        selector: SemanticSelector, deadline: RequestDeadline
    ) throws {
        var visited: [AXUIElement] = []
        var queue: [(AXUIElement, Int)] = [(root, 0)]
        var cursor = 0
        var match: AXUIElement?
        while cursor < queue.count {
            try deadline.check()
            guard visited.count < maxSelectorScanNodes else {
                throw helperError("tree_limit_exceeded")
            }
            let (element, depth) = queue[cursor]
            cursor += 1
            if visited.contains(where: { cfElementsEqual($0, element) }) { continue }
            visited.append(element)
            do {
                let role = try stringAttribute(element, kAXRoleAttribute as CFString)
                if role == selector.role {
                    let label = try selector.label.map { _ in
                        try labelForElement(element, deadline: deadline)
                    }
                    let identifier = selector.identifier == nil ? nil
                        : identifierForElement(element).0
                    let target = SemanticTarget(
                        role: role, label: label?.1 == true ? nil : label?.0,
                        identifier: identifier
                    )
                    if selector.matches(target) {
                        if match != nil { throw helperError("target_ambiguous") }
                        match = element
                    }
                }
                if depth >= maxSelectorScanDepth {
                    var count: CFIndex = 0
                    let status = AXUIElementGetAttributeValueCount(
                        element, kAXChildrenAttribute as CFString, &count
                    )
                    if status == .success && count > 0 {
                        throw helperError("tree_limit_exceeded")
                    }
                    if status != .success && status != .attributeUnsupported
                        && status != .noValue {
                        throw helperError("ax_error", stage: "count_attribute",
                                          attribute: "AXChildren", axStatus: status)
                    }
                    continue
                }
                let children = try elementArray(
                    element, attribute: kAXChildrenAttribute as CFString,
                    maxCount: maxSelectorChildrenPerElement
                )
                if children.1 { throw helperError("tree_limit_exceeded") }
                queue.append(contentsOf: children.0.map { ($0, depth + 1) })
            } catch let failure as HelperFailure where failure.code == "ax_error"
                && failure.axStatus == Int(AXError.invalidUIElement.rawValue) {
                // Recycled elements from dynamic lists cannot be action targets.
                continue
            }
        }
        guard let match, cfElementsEqual(match, selected) else {
            throw helperError("target_changed")
        }
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
        let captureAvailable: Bool
        if #available(macOS 14.0, *) {
            captureAvailable = CGPreflightScreenCaptureAccess()
        } else {
            captureAvailable = false
        }
        return [
            "process_id": Int(identity.pid),
            "windows": listed,
            "capabilities": [
                "screen_capture": captureAvailable,
                "screen_capture_requires": "existing Screen Recording permission and macOS 14+",
                "keyboard": false,
                "click": false,
                "press": true,
                "secondary_actions": true,
                "scroll": "observed_AX_page_actions_only",
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

    private func observeTargetsResult(
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
        var targets: [[String: Any]] = []
        var queue: [(AXUIElement, Int)] = [(window, 0)]
        var cursor = 0
        // Breadth-first traversal keeps toolbars and dialogs reachable even when
        // a long list in the first child exhausts the bounded observation.
        while cursor < queue.count && state.nodeCount < maxTreeNodes {
            try deadline.check()
            let (element, depth) = queue[cursor]
            cursor += 1
            if seen(element, in: state) { continue }
            state.visited.append(element)
            state.nodeCount += 1

            do {
                let actions = try actionIdentities(element, readOnly: true)
                let press = actions["AXPress"]
                let settable = try observedValueIsSettable(element)
                if !actions.isEmpty || settable {
                    let value = try observedValue(element)
                    let digest = value.comparable ? valueDigest(value.value) : nil
                    let role = try stringAttribute(element, kAXRoleAttribute as CFString)
                    let label = try labelForElement(element, deadline: deadline)
                    let identifier = identifierForElement(element)
                    let enabled = try boolAttribute(element, kAXEnabledAttribute as CFString)
                    let ref = UUID().uuidString.lowercased()
                    var target: [String: Any] = [
                        "element_ref": ref,
                        "role": role ?? NSNull(),
                        "label": NSNull(),
                        "identifier": NSNull(),
                        "label_truncated": label.1,
                        "identifier_truncated": identifier.1,
                        "enabled": enabled ?? NSNull(),
                        "pressable": press != nil && digest != nil,
                        "actions": digest == nil ? [] : actions.keys.sorted(),
                        "settable": settable,
                    ]
                    if let bounds = elementBounds(element) {
                        target["bounds"] = boundsJSON(bounds)
                    }
                    if !label.1, let value = label.0 { target["label"] = value }
                    if !identifier.1, let value = identifier.0 { target["identifier"] = value }
                    let bytes = try JSONSerialization.data(withJSONObject: target).count + 4
                    if state.outputBytes + bytes > 48 * 1024 {
                        state.truncated = true
                        break
                    }
                    state.outputBytes += bytes
                    targets.append(target)
                    refs[ref] = ObservedElement(
                        element: element, valueDigest: digest, pressIdentity: press,
                        target: SemanticTarget(role: role,
                                               label: label.1 ? nil : label.0,
                                               identifier: identifier.1 ? nil : identifier.0),
                        actions: actions
                    )
                }

                if depth >= maxTreeDepth {
                    var count: CFIndex = 0
                    let status = AXUIElementGetAttributeValueCount(
                        element, kAXChildrenAttribute as CFString, &count
                    )
                    if status == .success && count > 0 {
                        state.truncated = true
                    } else if status != .success && status != .attributeUnsupported
                                && status != .noValue {
                        throw helperError("ax_error", stage: "count_attribute",
                                          attribute: "AXChildren", axStatus: status)
                    }
                    continue
                }
                let children = try elementArray(
                    element, attribute: kAXChildrenAttribute as CFString,
                    maxCount: maxChildrenPerElement
                )
                if children.1 { state.truncated = true }
                queue.append(contentsOf: children.0.map { ($0, depth + 1) })
            } catch let failure as HelperFailure where state.nodeCount > 1
                && failure.code == "ax_error"
                && failure.axStatus == Int(AXError.invalidUIElement.rawValue) {
                // Dynamic lists can hand out AX children that have already been
                // recycled. Skip those read-only nodes; they are never leased.
                state.truncated = true
                continue
            }
        }
        if cursor < queue.count { state.truncated = true }
        let observationID = UUID().uuidString.lowercased()
        observations[observationID] = ObservationRecord(
            id: observationID, app: app, windowID: windowID,
            process: record.process, elements: refs,
            expiresAtUptime: uptime() + observationTTL
        )
        return [
            "observation_id": observationID,
            "process_id": Int(record.process.pid),
            "window_id": windowID,
            "ttl_seconds": Int(observationTTL),
            "visited_elements": state.nodeCount,
            "truncated": state.truncated,
            "targets": targets,
            "tree_omitted": true,
        ]
    }

    private func targetRef(
        app: String, windowID: Int, observationID: String,
        selector: SemanticSelector
    ) throws -> String {
        pruneState()
        guard let observation = observations[observationID],
              observation.expiresAtUptime > uptime() else {
            observations.removeValue(forKey: observationID)
            throw helperError("observation_unavailable")
        }
        guard observation.app == app, observation.windowID == windowID else {
            throw helperError("observation_mismatch")
        }
        return try matchingElementRef(observation.elements, selector: selector)
    }

    private func screenshotResult(
        app: String, windowID: Int, observationID: String, deadline: RequestDeadline
    ) throws -> [String: Any] {
        guard #available(macOS 14.0, *) else {
            throw helperError("screen_capture_unavailable")
        }
        guard CGPreflightScreenCaptureAccess() else {
            throw helperError("screen_recording_required")
        }
        // The CLI has no NSApplication event loop. Initialize AppKit's display
        // connection before constructing a desktop-independent SCContentFilter.
        guard !NSScreen.screens.isEmpty else {
            throw helperError("screen_capture_unavailable")
        }
        let record = try windowRecord(windowID, app: app)
        let window = try revalidateWindow(record, deadline: deadline)
        guard let bounds = elementBounds(window) else {
            throw helperError("capture_window_unavailable")
        }
        let title = try stringAttribute(window, kAXTitleAttribute as CFString)
        try requireUniqueCaptureWindow(record, bounds: bounds, title: title, deadline: deadline)
        let content = try captureContent(deadline)
        let matches = content.windows.filter { candidate in
            guard let owner = candidate.owningApplication else { return false }
            return candidate.isOnScreen && captureMatches(
                pid: owner.processID, frame: candidate.frame, title: candidate.title,
                expectedPID: record.process.pid, expectedFrame: bounds, expectedTitle: title
            )
        }
        guard !matches.isEmpty else { throw helperError("capture_window_unavailable") }
        guard matches.count == 1 else { throw helperError("capture_window_ambiguous") }
        let selected = matches[0]
        let filter = SCContentFilter(desktopIndependentWindow: selected)
        let configuration = SCStreamConfiguration()
        // Bound dimensions before capture/encoding. Capture only this window,
        // without the cursor, window shadow, or any surrounding display pixels.
        let scale = min(1.0, 1600 / max(bounds.width, bounds.height))
        configuration.width = max(1, Int((bounds.width * scale).rounded()))
        configuration.height = max(1, Int((bounds.height * scale).rounded()))
        configuration.showsCursor = false
        configuration.ignoreShadowsSingleWindow = true
        configuration.captureResolution = .nominal
        let captured = CaptureResult<CGImage>()
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration) {
            image, error in captured.complete(image, failed: error != nil)
        }
        let image = try captured.wait(deadline)
        guard image.width > 0, image.height > 0,
              image.width <= 1600, image.height <= 1600 else {
            throw helperError("image_too_large")
        }
        try requireUniqueCaptureWindow(record, bounds: bounds, title: title, deadline: deadline)
        let refreshed = try captureContent(deadline)
        guard refreshed.windows.contains(where: {
            $0.windowID == selected.windowID && $0.isOnScreen
                && $0.owningApplication?.processID == record.process.pid
                && $0.frame == bounds && $0.title == title
        }) else {
            throw helperError("capture_target_changed")
        }
        try deadline.check()
        let data = try encodedCapture(image)
        return [
            "content": [["type": "image", "mimeType": "image/jpeg",
                         "data": data.base64EncodedString()]],
            "visual": [
                "kind": "native_window", "capture_mode": "sequential",
                "capture_id": UUID().uuidString.lowercased(),
                "observation_id": observationID,
                "window_id": windowID, "capture_window_id": Int(selected.windowID),
                "process_id": Int(record.process.pid), "bounds": boundsJSON(bounds),
                "width": image.width, "height": image.height,
                "image_origin": "window_top_left", "coordinate_unit": "pixel",
                "mime_type": "image/jpeg", "bytes": data.count,
                "scale_x": Double(image.width) / bounds.width,
                "scale_y": Double(image.height) / bounds.height,
            ],
        ]
    }

    private func requireUniqueCaptureWindow(
        _ record: WindowRecord, bounds: CGRect, title: String?, deadline: RequestDeadline
    ) throws {
        let selected = try revalidateWindow(record, deadline: deadline)
        guard elementBounds(selected) == bounds,
              try stringAttribute(selected, kAXTitleAttribute as CFString) == title else {
            throw helperError("capture_target_changed")
        }
        let application = try confirmProcess(record.process)
        let all = try currentWindows(application, pid: record.process.pid, deadline: deadline)
        var matches = 0
        for candidate in all {
            try deadline.check()
            if elementBounds(candidate) == bounds,
               try stringAttribute(candidate, kAXTitleAttribute as CFString) == title {
                matches += 1
            }
        }
        // Check the AX inventory as well as capture candidates. A hidden second
        // AX window with the same title/frame must not map to a different visible
        // window merely because ScreenCaptureKit excludes the hidden one.
        guard matches == 1 else { throw helperError("capture_window_ambiguous") }
    }

    private func mutateResult(
        app: String,
        windowID: Int,
        observationID: String,
        elementRef: String,
        rawValue: Any?,
        press: Bool = false,
        action: String? = nil,
        selector: SemanticSelector? = nil,
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

        let actionName = press ? "AXPress" : action
        let requested = actionName == nil ? try requestedCFValue(rawValue) : nil
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
        if let selector {
            let currentLabel = try selector.label.map { _ in
                try labelForElement(currentElement, deadline: deadline)
            }
            let currentIdentifier = selector.identifier == nil ? nil
                : identifierForElement(currentElement).0
            let currentTarget = SemanticTarget(
                role: try stringAttribute(currentElement, kAXRoleAttribute as CFString),
                label: currentLabel?.1 == true ? nil : currentLabel?.0,
                identifier: currentIdentifier
            )
            guard selector.matches(currentTarget) else {
                throw helperError("target_changed")
            }
            try verifyUniqueSelector(root: window, selected: currentElement,
                                     selector: selector, deadline: deadline)
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
        if try actionName == nil && !valueIsSettable(currentElement) {
            throw helperError("value_not_settable")
        }
        guard let expected = observedElement.valueDigest else {
            throw helperError("value_not_comparable")
        }
        let before = try copyOptionalAttribute(currentElement, kAXValueAttribute as CFString)
        guard let current = valueDigest(before) else {
            throw helperError("value_not_comparable")
        }
        guard expected == current else { throw helperError("value_changed") }
        if let actionName {
            guard enabledState.value == true else { throw helperError("element_not_enabled") }
            let expected = press ? observedElement.pressIdentity : observedElement.actions[actionName]
            let current = try actionIdentities(currentElement)[actionName]
            try validateObservedAction(expected, current, press: press)
            try deadline.check()
            let status = AXUIElementPerformAction(currentElement, actionName as CFString)
            guard status == .success else {
                throw helperError("ax_error", stage: "perform_action", axStatus: status)
            }
            return ["process_id": Int(record.process.pid), "window_id": windowID,
                    "observation_id": observationID, "element_ref": elementRef,
                    "action": actionName, "action_accepted": true,
                    "postcondition_verified": false]
        }
        // No mutation is attempted after the request budget has expired.
        try deadline.check()
        guard let requested else { throw helperError("invalid_input") }

        let status = AXUIElementSetAttributeValue(
            currentElement,
            kAXValueAttribute as CFString,
            requested
        )
        guard status == .success else {
            throw helperError("ax_error", stage: "set_attribute",
                              attribute: "AXValue", axStatus: status)
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
            "role", "label", "identifier", "action", "include_image",
        ]
        guard Set(object.keys).isSubset(of: allowedKeys) else {
            throw helperError("invalid_input")
        }

        let method = try nonEmptyString(object["method"], maxBytes: 64)
        let app = try nonEmptyString(object["app"], maxBytes: 512)
        if object["action"] != nil && method != "action" {
            throw helperError("invalid_input")
        }
        var includeImage = false
        if let raw = object["include_image"] {
            guard ["observe", "observe_targets"].contains(method),
                  let flag = raw as? NSNumber, isBooleanNSNumber(flag) else {
                throw helperError("invalid_input")
            }
            includeImage = flag.boolValue
        }

        switch method {
        case "windows":
            guard object["window_id"] == nil, object["observation_id"] == nil,
                  object["element_ref"] == nil, object["value"] == nil,
                  object["role"] == nil, object["label"] == nil,
                  object["identifier"] == nil else {
                throw helperError("invalid_input")
            }
            return try windowsResult(app: app, deadline: deadline)

        case "observe", "observe_targets":
            guard object["observation_id"] == nil, object["element_ref"] == nil,
                  object["value"] == nil, object["role"] == nil,
                  object["label"] == nil, object["identifier"] == nil else {
                throw helperError("invalid_input")
            }
            let windowID = try positiveInt(object["window_id"])
            var result = try method == "observe_targets"
                ? observeTargetsResult(app: app, windowID: windowID, deadline: deadline)
                : observeResult(app: app, windowID: windowID, deadline: deadline)
            if includeImage, let observationID = result["observation_id"] as? String {
                do {
                    let picture = try screenshotResult(
                        app: app, windowID: windowID, observationID: observationID,
                        deadline: deadline
                    )
                    result.merge(picture) { _, new in new }
                } catch let failure as HelperFailure {
                    // Preserve useful AX evidence with an explicit visual failure.
                    result["visual_unavailable"] = failure.code
                }
            }
            return result

        case "set_value", "press", "action":
            guard object["role"] == nil, object["label"] == nil,
                  object["identifier"] == nil else {
                throw helperError("invalid_input")
            }
            let windowID = try positiveInt(object["window_id"])
            let observationID = try nonEmptyString(object["observation_id"], maxBytes: 128)
            let elementRef = try nonEmptyString(object["element_ref"], maxBytes: 128)
            guard (method == "set_value") == object.keys.contains("value") else {
                throw helperError("invalid_input")
            }
            let action: String?
            if method == "action" {
                let name = try nonEmptyString(object["action"], maxBytes: 128)
                guard supportedActions.contains(name) else { throw helperError("invalid_input") }
                action = name
            } else {
                action = nil
            }
            return try mutateResult(
                app: app,
                windowID: windowID,
                observationID: observationID,
                elementRef: elementRef,
                rawValue: object["value"],
                press: method == "press",
                action: action,
                deadline: deadline
            )

        case "set_value_target", "press_target":
            guard object["element_ref"] == nil,
                  (method == "set_value_target") == object.keys.contains("value") else {
                throw helperError("invalid_input")
            }
            let role = try nonEmptyString(object["role"], maxBytes: 128)
            let label = try object["label"].map { try nonEmptyString($0, maxBytes: 2048) }
            let identifier = try object["identifier"].map {
                try nonEmptyString($0, maxBytes: 2048)
            }
            guard label != nil || identifier != nil else {
                throw helperError("invalid_input")
            }
            let selector = SemanticSelector(role: role, label: label, identifier: identifier)
            let windowID = try positiveInt(object["window_id"])
            let observationID = try nonEmptyString(object["observation_id"], maxBytes: 128)
            let elementRef = try targetRef(app: app, windowID: windowID,
                                           observationID: observationID, selector: selector)
            return try mutateResult(
                app: app, windowID: windowID, observationID: observationID,
                elementRef: elementRef, rawValue: object["value"],
                press: method == "press_target", selector: selector, deadline: deadline
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

private func emit(_ object: [String: Any], maxBytes: Int = 64 * 1024) {
    let data: Data
    do {
        data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    } catch {
        let fallback = #"{"error":{"code":"internal_error"},"id":null}"#
        FileHandle.standardOutput.write(Data((fallback + "\n").utf8))
        return
    }
    if data.count + 1 > maxBytes {
        emit(["id": object["id"] ?? NSNull(),
              "error": ["code": "response_too_large"]])
        return
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

private func emitError(id: Any?, code: String, failure: HelperFailure? = nil) {
    var error: [String: Any] = ["code": code]
    if let stage = failure?.stage { error["stage"] = stage }
    if let attribute = failure?.attribute { error["attribute"] = attribute }
    if let status = failure?.axStatus { error["ax_status"] = status }
    emit([
        "id": id ?? NSNull(),
        "error": error,
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
        emit(["id": responseID, "result": result],
             maxBytes: object["include_image"] as? Bool == true ? 4 * 1024 * 1024 : 64 * 1024)
    } catch let failure as HelperFailure {
        emitError(id: responseID, code: failure.code, failure: failure)
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
