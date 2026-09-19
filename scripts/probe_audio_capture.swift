// Experimental macOS 15+ capture probe. Not a public Anywhere tool yet.
// Compile: swiftc -parse-as-library scripts/probe_audio_capture.swift -o /tmp/ac-audio-probe
// Inspect without requesting permission: /tmp/ac-audio-probe --check
// Capture: /tmp/ac-audio-probe system|microphone|both SECONDS NEW_OUTPUT_DIRECTORY [MIC_DEVICE_ID]
// Microphone/both require an explicitly selected ID from --list-devices; no default input.
import AVFoundation
import CoreGraphics
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

enum CaptureFailure: String, Error {
    case invalidArguments, permissionRequired, noDisplay, invalidAudio, writeFailed, noSamples
}

@available(macOS 15.0, *)
final class AudioSink: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let queue = DispatchQueue(label: "anywhere.audio.probe")
    let directory: URL
    let seconds: Double
    var files: [String: AVAudioFile] = [:]
    var frames: [String: Int64] = [:]
    var rates: [String: Double] = [:]
    var peaks: [String: Float] = [:]
    var sumSquares: [String: Double] = [:]
    var values: [String: Int64] = [:]
    var failure: CaptureFailure?

    init(directory: URL, seconds: Double) { self.directory = directory; self.seconds = seconds }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.failure = .writeFailed }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer,
                of type: SCStreamOutputType) {
        consume(sample, type: type)
    }

    // Called only on queue, including by the synthetic-buffer regression harness.
    func consume(_ sample: CMSampleBuffer, type: SCStreamOutputType) {
        guard failure == nil, type == .audio || type == .microphone else { return }
        let name = type == .audio ? "system" : "microphone"
        let count = CMSampleBufferGetNumSamples(sample)
        guard CMSampleBufferDataIsReady(sample), count > 0, count <= Int(Int32.max),
              let description = CMSampleBufferGetFormatDescription(sample)
        else { failure = .invalidAudio; return }
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        guard format.sampleRate.isFinite, format.sampleRate > 0,
              format.commonFormat == .pcmFormatFloat32,
              rates[name] == nil || rates[name] == format.sampleRate
        else { failure = .invalidAudio; return }
        let remaining = Int64(seconds * format.sampleRate) - frames[name, default: 0]
        guard remaining > 0 else { return }
        let savedCount = min(count, Int(remaining))
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                            frameCapacity: AVAudioFrameCount(savedCount))
        else { failure = .invalidAudio; return }
        buffer.frameLength = AVAudioFrameCount(savedCount)
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sample, at: 0, frameCount: Int32(savedCount), into: buffer.mutableAudioBufferList
        ) == noErr else { failure = .invalidAudio; return }
        do {
            if files[name] == nil {
                var settings = format.settings
                settings[AVLinearPCMIsNonInterleaved] = false
                files[name] = try AVAudioFile(
                    forWriting: directory.appendingPathComponent(name + ".caf"),
                    settings: settings, commonFormat: format.commonFormat,
                    interleaved: format.isInterleaved
                )
            }
            try files[name]!.write(from: buffer)
            guard let channels = buffer.floatChannelData else {
                failure = .invalidAudio; return
            }
            for channel in 0..<Int(format.channelCount) {
                for frame in 0..<savedCount {
                    let value = channels[channel][frame * buffer.stride]
                    guard value.isFinite else { failure = .invalidAudio; return }
                    peaks[name] = max(peaks[name, default: 0], abs(value))
                    sumSquares[name, default: 0] += Double(value) * Double(value)
                }
            }
            values[name, default: 0] += Int64(savedCount) * Int64(format.channelCount)
            rates[name] = format.sampleRate
            frames[name, default: 0] += Int64(savedCount)
        } catch { failure = .writeFailed }
    }
}

#if !AUDIO_PROBE_TESTS
@main
struct AudioProbe {
    static func emit(_ value: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) { print(text) }
    }

    static func main() async {
        guard #available(macOS 15.0, *) else {
            emit(["state": "unsupported", "minimum_macos": "15"]); exit(2)
        }
        let screenAllowed = CGPreflightScreenCaptureAccess()
        let micAllowed = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments == ["--list-devices"] {
            let devices = AVCaptureDevice.DiscoverySession(
                deviceTypes: [.microphone], mediaType: .audio, position: .unspecified
            ).devices
            emit(["state": "device_list", "capture_started": false,
                  "devices": devices.map { ["id": $0.uniqueID, "name": $0.localizedName] }])
            return
        }
        if arguments == ["--check"] {
            emit(["state": "permission_check", "screen_capture_allowed": screenAllowed,
                  "microphone_allowed": micAllowed, "capture_started": false,
                  "microphone_mode_requires_screen_permission_in_probe": true])
            return
        }
        var stream: SCStream?
        var sink: AudioSink?
        do {
            guard (3...4).contains(arguments.count),
                  ["system", "microphone", "both"].contains(arguments[0]),
                  let seconds = Double(arguments[1]), seconds.isFinite, seconds >= 1, seconds <= 30,
                  arguments[2].hasPrefix("/") else { throw CaptureFailure.invalidArguments }
            let source = arguments[0]
            var microphoneID: String?
            if source != "system" {
                guard arguments.count == 4 else { throw CaptureFailure.invalidArguments }
                let available = AVCaptureDevice.DiscoverySession(
                    deviceTypes: [.microphone], mediaType: .audio, position: .unspecified
                ).devices
                guard available.contains(where: { $0.uniqueID == arguments[3] }) else {
                    throw CaptureFailure.invalidArguments
                }
                microphoneID = arguments[3]
            } else if arguments.count != 3 { throw CaptureFailure.invalidArguments }
            guard screenAllowed, source == "system" || micAllowed else {
                throw CaptureFailure.permissionRequired
            }
            let directory = URL(fileURLWithPath: arguments[2], isDirectory: true)
            // Refuse existing output directories; never overwrite previous recordings.
            guard mkdir(directory.path, 0o700) == 0 else { throw CaptureFailure.writeFailed }
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: true
            )
            guard let display = content.displays.first else { throw CaptureFailure.noDisplay }
            let config = SCStreamConfiguration()
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.capturesAudio = source != "microphone"
            config.captureMicrophone = source != "system"
            config.microphoneCaptureDeviceID = microphoneID
            config.sampleRate = 48000
            config.channelCount = 2
            let receiver = AudioSink(directory: directory, seconds: seconds)
            sink = receiver
            let capture = SCStream(filter: SCContentFilter(display: display, excludingWindows: []),
                                   configuration: config, delegate: receiver)
            stream = capture
            if config.capturesAudio {
                try capture.addStreamOutput(receiver, type: .audio, sampleHandlerQueue: receiver.queue)
            }
            if config.captureMicrophone {
                try capture.addStreamOutput(receiver, type: .microphone,
                                            sampleHandlerQueue: receiver.queue)
            }
            let started = ISO8601DateFormatter().string(from: Date())
            try await capture.startCapture()
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            try await capture.stopCapture()
            stream = nil
            let result = receiver.queue.sync { () -> ([String: Int64], CaptureFailure?) in
                receiver.files.removeAll() // Finalize files before reporting success.
                return (receiver.frames, receiver.failure)
            }
            if let failure = result.1 { throw failure }
            let requested = source == "both" ? ["system", "microphone"] : [source]
            guard requested.allSatisfy({ result.0[$0, default: 0] > 0 }) else {
                throw CaptureFailure.noSamples
            }
            let measurements: [String: Any] = receiver.queue.sync {
                Dictionary(uniqueKeysWithValues: requested.map { name in
                    (name, ["sample_rate": receiver.rates[name]!,
                            "duration_seconds": Double(receiver.frames[name]!) / receiver.rates[name]!,
                            "peak": Double(receiver.peaks[name, default: 0]),
                            "rms": sqrt(receiver.sumSquares[name, default: 0] /
                                        Double(receiver.values[name]!))])
                })
            }
            emit(["state": "captured", "source": source, "started_at": started,
                  "completed_at": ISO8601DateFormatter().string(from: Date()),
                  "requested_seconds": seconds, "frames": result.0, "measurements": measurements,
                  "microphone_device_id": microphoneID ?? "none",
                  "output_directory": directory.path, "speaker_output_verified": false])
        } catch {
            if let capture = stream { try? await capture.stopCapture() }
            if let receiver = sink { receiver.queue.sync { receiver.files.removeAll() } }
            emit(["state": "failed", "error_code": (error as? CaptureFailure)?.rawValue ?? "capture_failed",
                  "partial_files_possible": true, "speaker_output_verified": false])
            exit(1)
        }
    }
}

#endif
