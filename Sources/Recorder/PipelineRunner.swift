import Foundation
import OSLog

private let log = Logger(subsystem: "co.nvg8.recorder", category: "pipeline")

/// Runs the Python sidecar scripts (transcribe → extract todos) using `uv run`.
///
/// We assume the project layout from this repo and locate the `python/` directory
/// relative to the running executable. If the app is run from the build dir,
/// we walk up to find it; if bundled inside an .app, we look in Resources.
struct PipelineRunner {
    let pythonDir: URL

    static func locate() -> PipelineRunner? {
        // 1. Env override (useful for development).
        if let override = ProcessInfo.processInfo.environment["RECORDER_PYTHON_DIR"] {
            let url = URL(fileURLWithPath: override)
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("pyproject.toml").path) {
                return PipelineRunner(pythonDir: url)
            }
        }
        // 2. Inside an .app bundle: Contents/Resources/python
        let bundleResources = Bundle.main.resourceURL?.appendingPathComponent("python")
        if let b = bundleResources,
           FileManager.default.fileExists(atPath: b.appendingPathComponent("pyproject.toml").path) {
            return PipelineRunner(pythonDir: b)
        }
        // 3. Walk up from the executable to find a sibling `python/`.
        var dir = Bundle.main.executableURL?.deletingLastPathComponent()
        for _ in 0..<8 {
            guard let d = dir else { break }
            let candidate = d.appendingPathComponent("python")
            if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("pyproject.toml").path) {
                return PipelineRunner(pythonDir: candidate)
            }
            dir = d.deletingLastPathComponent()
        }
        return nil
    }

    /// Look up the calendar event that overlaps the recording window, using
    /// the calendar-matching OAuth token. Soft-fails: returns `nil` if no event matches
    /// or the lookup errors out — the rest of the pipeline carries on.
    func fetchMeeting(stem: String, audio: URL) async -> URL? {
        let url = audio.deletingLastPathComponent().appendingPathComponent("\(stem).event.json")
        do {
            let data = try await run(args: [
                "run", "python", "fetch_meeting.py",
                stem,
                "--audio", audio.path,
            ])
            // The script always prints a JSON object — empty {} on miss.
            // Persist only if it has a title (i.e. an actual match).
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let title = obj["title"] as? String, !title.isEmpty {
                try data.write(to: url)
                log.info("matched event: \(title, privacy: .private)")
                return url
            }
            log.info("no calendar event matched recording \(stem, privacy: .public)")
            return nil
        } catch {
            log.error("fetch_meeting failed: \(error.localizedDescription, privacy: .public)")
            return nil
        }
    }

    func transcribe(
        system: URL,
        mic: URL,
        onProgressLine: (@Sendable (String) -> Void)? = nil
    ) async throws -> (TranscriptResult, URL) {
        // Pass both tracks; Python transcribes each separately and merges them
        // into one speaker-labeled (You/Remote) transcript, falling back to a
        // single unlabeled transcript if a track is silent.
        let out = try await runStreaming(
            args: ["run", "python", "transcribe.py", system.path, "--mic", mic.path],
            onStderrLine: onProgressLine
        )
        let result = try JSONDecoder().decode(TranscriptResult.self, from: out)
        // Strip the `.system.wav` suffix when picking a transcript filename so
        // we end up with `rec-YYYYMMDD-HHMMSS.transcript.json` alongside the
        // `.mixed.wav` Python emitted.
        let stem: String = {
            let name = system.lastPathComponent
            if name.hasSuffix(".system.wav") { return String(name.dropLast(".system.wav".count)) }
            return system.deletingPathExtension().lastPathComponent
        }()
        let transcriptURL = system.deletingLastPathComponent()
            .appendingPathComponent("\(stem).transcript.json")
        try out.write(to: transcriptURL)
        return (result, transcriptURL)
    }

    func extractTodos(
        transcriptURL: URL,
        eventURL: URL? = nil,
        priorTodosURL: URL? = nil
    ) async throws -> (TodoExtraction, URL) {
        var args = ["run", "python", "extract_todos.py", transcriptURL.path]
        if let eventURL { args.append(contentsOf: ["--event", eventURL.path]) }
        if let priorTodosURL { args.append(contentsOf: ["--prior-todos", priorTodosURL.path]) }
        let out = try await run(args: args)
        let result = try JSONDecoder().decode(TodoExtraction.self, from: out)
        // transcriptURL is `<stem>.transcript.json` → emit `<stem>.todos.json`.
        let name = transcriptURL.lastPathComponent
        let stem = name.hasSuffix(".transcript.json")
            ? String(name.dropLast(".transcript.json".count))
            : transcriptURL.deletingPathExtension().lastPathComponent
        let todosURL = transcriptURL.deletingLastPathComponent()
            .appendingPathComponent("\(stem).todos.json")
        try out.write(to: todosURL)
        return (result, todosURL)
    }

    func importGeminiNotes() async throws -> Data {
        try await run(args: [
            "run", "python", "import_gemini_notes.py",
            "--recordings-dir", AppPaths.recordingsDir.path,
        ])
    }

    func fetchMeetingPrep(range: String = "today") async throws -> Data {
        try await run(args: [
            "run", "python", "fetch_meeting_prep.py",
            "--range", range, "--account", PrepCache.account,
        ])
    }

    func allowlistPrepTitle(_ title: String) async throws -> Data {
        try await run(args: [
            "run", "python", "fetch_meeting_prep.py",
            "--allowlist-title", title, "--account", PrepCache.account,
        ])
    }

    /// Answer a question across every recorded meeting. Slow (10–30s) — the
    /// whole archive goes to the model — so callers should show progress.
    func askMeetings(_ question: String) async throws -> Data {
        try await run(args: ["run", "python", "ask_meetings.py", question])
    }

    /// Pull Gemini notes from the calendar event's Drive attachment.
    ///
    /// Runs alongside the email importer rather than instead of it: the Drive
    /// copy appears sooner, but a stale or series-level attachment is declined,
    /// and the email is the per-instance source of record. Each path skips a
    /// meeting the other already claimed.
    func importMeetNotes(days: Int = 7) async throws -> Data {
        try await run(args: ["run", "python", "import_meet_notes.py", "--days", String(days)])
    }

    /// Health of every Google token, as JSON. `deep` forces a refresh, which is
    /// the only way to catch a revoked token whose access token is still cached.
    func googleAuthStatus(deep: Bool = true) async throws -> Data {
        var args = ["run", "python", "google_auth.py", "status", "--json"]
        if deep { args.append("--deep") }
        return try await run(args: args)
    }

    private func run(args: [String]) async throws -> Data {
        try await runStreaming(args: args, onStderrLine: nil)
    }

    private final class StderrState: @unchecked Sendable {
        let lock = NSLock()
        var buffer = Data()
    }

    private final class StdoutState: @unchecked Sendable {
        let lock = NSLock()
        var buffer = Data()
    }

    private static func emitStderrLine(
        _ line: String,
        onStderrLine: (@Sendable (String) -> Void)?
    ) {
        guard !line.isEmpty else { return }
        onStderrLine?(line)
    }

    private static func drainStderrBuffer(
        state: StderrState,
        onStderrLine: (@Sendable (String) -> Void)?,
        final: Bool = false
    ) {
        state.lock.lock()
        defer { state.lock.unlock() }

        while let newlineIndex = state.buffer.firstIndex(of: 0x0A) {
            let lineData = state.buffer.prefix(upTo: newlineIndex)
            let removeThrough = state.buffer.index(after: newlineIndex)
            state.buffer.removeSubrange(state.buffer.startIndex..<removeThrough)
            if let line = String(data: lineData, encoding: .utf8) {
                emitStderrLine(line, onStderrLine: onStderrLine)
            }
        }

        if final, !state.buffer.isEmpty {
            if let line = String(data: state.buffer, encoding: .utf8) {
                emitStderrLine(line, onStderrLine: onStderrLine)
            }
            state.buffer.removeAll(keepingCapacity: false)
        }
    }

    private func runStreaming(
        args: [String],
        onStderrLine: (@Sendable (String) -> Void)?
    ) async throws -> Data {
        // Process does no PATH lookup, so a missing uv has to be caught here.
        guard let uv = Self.uvPath() else {
            throw PipelineError.subprocess("uv not found. Install it (brew install uv) or set RECORDER_UV_PATH.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: uv)
        process.arguments = args
        process.currentDirectoryURL = pythonDir

        // GUI-launched apps inherit a minimal env. Build one that's enough for
        // uv + parakeet-mlx + boto3 to function.
        var env = ProcessInfo.processInfo.environment
        let home = env["HOME"] ?? NSHomeDirectory()
        env["HOME"] = home
        // Pin the uv project venv outside the (read-only-ish) .app bundle.
        let venvDir = AppPaths.supportDir.appendingPathComponent("venv").path
        env["UV_PROJECT_ENVIRONMENT"] = venvDir
        // Persistent caches in a stable location.
        env["UV_CACHE_DIR"] = AppPaths.supportDir.appendingPathComponent("uv-cache").path
        env["HF_HOME"] = home + "/.cache/huggingface"
        // Privacy lockdown: the transcription model is already cached locally, so
        // cut all Hugging Face network access — both model fetches and the
        // anonymous download-telemetry ping. Transcription runs fully offline;
        // audio never leaves the Mac. (To pull a new/updated model, temporarily
        // unset HF_HUB_OFFLINE; the cached parakeet model needs no network.)
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        // Ensure subprocesses can find common CLIs.
        let extraPaths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env["PATH"] = ((env["PATH"]?.split(separator: ":").map(String.init) ?? []) + extraPaths)
            .reduce(into: [String]()) { acc, p in if !acc.contains(p) { acc.append(p) } }
            .joined(separator: ":")
        process.environment = env

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        let stdoutHandle = stdout.fileHandleForReading
        let stdoutState = StdoutState()
        let stderrHandle = stderr.fileHandleForReading
        let stderrState = StderrState()

        stdoutHandle.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { return }
            stdoutState.lock.lock()
            stdoutState.buffer.append(data)
            stdoutState.lock.unlock()
        }

        stderrHandle.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                Self.drainStderrBuffer(state: stderrState, onStderrLine: onStderrLine, final: true)
                return
            }
            stderrState.lock.lock()
            stderrState.buffer.append(data)
            stderrState.lock.unlock()
            Self.drainStderrBuffer(state: stderrState, onStderrLine: onStderrLine)
        }

        log.info("running uv \(args.first(where: { $0.hasSuffix(".py") }) ?? "?", privacy: .public) args=\(args.joined(separator: " "), privacy: .private)")
        try process.run()

        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Data, Error>) in
            DispatchQueue.global().async {
                process.waitUntilExit()
                stdoutHandle.readabilityHandler = nil
                stderrHandle.readabilityHandler = nil
                stdoutState.lock.lock()
                let outData = stdoutState.buffer
                stdoutState.buffer.removeAll(keepingCapacity: false)
                stdoutState.lock.unlock()
                Self.drainStderrBuffer(state: stderrState, onStderrLine: onStderrLine, final: true)
                let errData = (try? stderr.fileHandleForReading.readToEnd()) ?? Data()
                let errStr = String(data: errData, encoding: .utf8) ?? ""
                if !errStr.isEmpty {
                    log.info("sidecar stderr: \(errStr, privacy: .private)")
                }
                if process.terminationStatus != 0 {
                    cont.resume(throwing: PipelineError.subprocess(Self.summarizeError(stderr: errStr, exitCode: process.terminationStatus)))
                } else {
                    cont.resume(returning: outData)
                }
            }
        }
    }

    /// Pull the most useful chunk out of a noisy stderr — the Python traceback
    /// or the last 12 lines, whichever is more informative.
    private static func summarizeError(stderr: String, exitCode: Int32) -> String {
        let trimmed = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return "exit \(exitCode) (no stderr)" }
        if let tb = trimmed.range(of: "Traceback (most recent call last):") {
            return String(trimmed[tb.lowerBound...])
        }
        let lines = trimmed.split(separator: "\n")
        let tail = lines.suffix(12).joined(separator: "\n")
        return "exit \(exitCode):\n\(tail)"
    }

    private static func uvPath() -> String? {
        if let env = ProcessInfo.processInfo.environment["RECORDER_UV_PATH"] {
            return env
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let pathDirs = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        let dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", home + "/.local/bin", home + "/.cargo/bin"] + pathDirs
        for dir in dirs {
            let candidate = dir + "/uv"
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }
}

enum PipelineError: LocalizedError {
    case subprocess(String)
    case sidecarNotFound
    var errorDescription: String? {
        switch self {
        case .subprocess(let m): return m
        case .sidecarNotFound: return "Could not locate the python/ sidecar directory."
        }
    }
}
