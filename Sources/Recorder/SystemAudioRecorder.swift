import Foundation
import AVFoundation
import ScreenCaptureKit
import OSLog

private let log = Logger(subsystem: "co.nvg8.recorder", category: "audio")

/// Captures system audio output AND microphone via ScreenCaptureKit and writes
/// each stream to its own .wav file. The two files are mixed by the Python
/// sidecar before transcription so meetings (mic = you, system = remote
/// participants in Zoom/Meet/Teams) end up in a single transcript.
///
/// Requires macOS 15+ (for `captureMicrophone`).
final class SystemAudioRecorder: NSObject, @unchecked Sendable, SCStreamDelegate, SCStreamOutput {
    struct Output {
        let systemURL: URL
        let micURL: URL
    }

    private let queue = DispatchQueue(label: "co.nvg8.recorder.audio")
    private var stream: SCStream?

    private var systemWriter: AVAssetWriter?
    private var systemInput: AVAssetWriterInput?
    private var systemStarted = false
    private var systemURL: URL?
    private var systemSampleCount = 0

    private var micWriter: AVAssetWriter?
    private var micInput: AVAssetWriterInput?
    private var micStarted = false
    private var micURL: URL?
    private var micSampleCount = 0

    /// Begin recording. Returns the file URLs the audio will be written to.
    func start() async throws -> Output {
        guard stream == nil else { throw RecorderError.alreadyRunning }

        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw RecorderError.noDisplay
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.captureMicrophone = true
        config.excludesCurrentProcessAudio = false
        config.sampleRate = 48_000
        config.channelCount = 2
        // Minimize video work — we don't consume video frames.
        config.width = 64
        config.height = 64
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 6
        config.showsCursor = false

        let stamp = Self.timestamp()
        let sysURL = AppPaths.recordingsDir.appendingPathComponent("rec-\(stamp).system.wav")
        let micURL = AppPaths.recordingsDir.appendingPathComponent("rec-\(stamp).mic.wav")
        try? FileManager.default.removeItem(at: sysURL)
        try? FileManager.default.removeItem(at: micURL)

        (systemWriter, systemInput) = try Self.makeWriter(url: sysURL, channels: 2)
        (micWriter, micInput) = try Self.makeWriter(url: micURL, channels: 2)
        systemURL = sysURL
        self.micURL = micURL
        systemStarted = false
        micStarted = false
        systemSampleCount = 0
        micSampleCount = 0

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        // Register a screen output so the video path drains; we drop the buffers.
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        self.stream = stream

        try await stream.startCapture()
        log.info("recording started → system=\(sysURL.path, privacy: .public), mic=\(micURL.path, privacy: .public)")
        return Output(systemURL: sysURL, micURL: micURL)
    }

    /// Stop recording. Resolves when both .wav files are fully written and closed.
    func stop() async throws -> Output {
        guard let stream else { throw RecorderError.notRunning }
        try await stream.stopCapture()
        self.stream = nil

        guard let systemWriter, let systemInput, let systemURL,
              let micWriter, let micInput, let micURL else {
            throw RecorderError.writerSetup
        }
        systemInput.markAsFinished()
        micInput.markAsFinished()
        await systemWriter.finishWriting()
        await micWriter.finishWriting()

        log.info("stopped: system samples=\(self.systemSampleCount), mic samples=\(self.micSampleCount)")

        self.systemWriter = nil; self.systemInput = nil; self.systemURL = nil
        self.micWriter = nil; self.micInput = nil; self.micURL = nil

        if systemWriter.status == .failed { throw systemWriter.error ?? RecorderError.writerSetup }
        if micWriter.status == .failed { throw micWriter.error ?? RecorderError.writerSetup }
        return Output(systemURL: systemURL, micURL: micURL)
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        switch type {
        case .audio:
            handle(buffer: sampleBuffer,
                   writer: systemWriter, input: systemInput,
                   startedFlag: &systemStarted, counter: &systemSampleCount, label: "system")
        case .microphone:
            handle(buffer: sampleBuffer,
                   writer: micWriter, input: micInput,
                   startedFlag: &micStarted, counter: &micSampleCount, label: "mic")
        case .screen:
            return
        @unknown default:
            return
        }
    }

    private func handle(buffer: CMSampleBuffer,
                        writer: AVAssetWriter?, input: AVAssetWriterInput?,
                        startedFlag: inout Bool, counter: inout Int, label: String) {
        guard let writer, let input else { return }
        if writer.status == .unknown {
            let pts = CMSampleBufferGetPresentationTimeStamp(buffer)
            if writer.startWriting() {
                writer.startSession(atSourceTime: pts)
                startedFlag = true
                log.info("\(label) writer session started at \(CMTimeGetSeconds(pts))")
            } else {
                log.error("\(label) writer.startWriting failed: \(String(describing: writer.error), privacy: .public)")
                return
            }
        }
        guard startedFlag, writer.status == .writing, input.isReadyForMoreMediaData else { return }
        if input.append(buffer) {
            counter += 1
        }
    }

    // MARK: - SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log.error("stream stopped with error: \(error.localizedDescription, privacy: .public)")
    }

    private static func makeWriter(url: URL, channels: Int) throws -> (AVAssetWriter, AVAssetWriterInput) {
        let writer = try AVAssetWriter(url: url, fileType: .wav)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVNumberOfChannelsKey: channels,
            AVSampleRateKey: 48_000,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else { throw RecorderError.writerSetup }
        writer.add(input)
        return (writer, input)
    }

    private static func timestamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.string(from: Date())
    }
}

enum RecorderError: LocalizedError {
    case alreadyRunning
    case notRunning
    case noDisplay
    case writerSetup

    var errorDescription: String? {
        switch self {
        case .alreadyRunning: return "Recorder is already running."
        case .notRunning: return "Recorder is not running."
        case .noDisplay: return "No display available for ScreenCaptureKit."
        case .writerSetup: return "Audio writer setup failed."
        }
    }
}
