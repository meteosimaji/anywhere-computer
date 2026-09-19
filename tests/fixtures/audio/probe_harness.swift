import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// Synthetic PCM only: no capture session, microphone, or screen access.
@main
struct ProbeHarness {
    static func main() throws {
        guard #available(macOS 15.0, *) else { return }
        let directory = URL(fileURLWithPath: CommandLine.arguments[1])
        let sink = AudioSink(directory: directory, seconds: 1)
        let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
        let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 30000)!
        pcm.frameLength = 30000
        for index in 0..<30000 { pcm.floatChannelData![0][index] = 0.25 }
        var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: 48000),
                                        presentationTimeStamp: .zero, decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        precondition(CMSampleBufferCreate(
            allocator: kCFAllocatorDefault, dataBuffer: nil, dataReady: false,
            makeDataReadyCallback: nil, refcon: nil, formatDescription: format.formatDescription,
            sampleCount: 30000, sampleTimingEntryCount: 1, sampleTimingArray: &timing,
            sampleSizeEntryCount: 0, sampleSizeArray: nil, sampleBufferOut: &sample
        ) == noErr)
        precondition(CMSampleBufferSetDataBufferFromAudioBufferList(
            sample!, blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0,
            bufferList: pcm.audioBufferList
        ) == noErr)
        precondition(CMSampleBufferSetDataReady(sample!) == noErr)
        sink.queue.sync {
            for source in [SCStreamOutputType.audio, .microphone] {
                // Cross the one-second limit, then deliver another full callback.
                for _ in 0..<3 { sink.consume(sample!, type: source) }
            }
            sink.files.removeAll()
            precondition(sink.failure == nil)
        }
        for name in ["system", "microphone"] {
            let file = try AVAudioFile(forReading: directory.appendingPathComponent(name + ".caf"))
            precondition(file.length == 48000)
            precondition(sink.frames[name] == 48000)
            precondition(sink.peaks[name] == 0.25)
            precondition(sink.sumSquares[name] == 3000)
        }
        print("synthetic duration and measurements passed")
    }
}
