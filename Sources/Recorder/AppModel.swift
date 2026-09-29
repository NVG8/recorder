import SwiftUI
import Combine
import OSLog

private let authLog = Logger(subsystem: "co.nvg8.recorder", category: "auth")

/// Selectable lookahead window for the upcoming-events view.
/// Raw values must match the Python sidecar's `--range` choices.
enum PrepRange: String, CaseIterable, Identifiable {
    case today
    case week
    case next7

    var id: String { rawValue }

    var label: String {
        switch self {
        case .today: return "Today"
        case .week: return "This Week"
        case .next7: return "Next 7 Days"
        }
    }
}

/// Reads the prep JSON the Python sidecar caches after every successful run.
///
/// Gathering prep takes ~15s (calendar, notmuch, transcripts, Bedrock), which is
/// far too long to stare at an empty pane on launch. The sidecar already writes
/// its last good response to disk, so we paint that immediately and refresh in
/// the background — stale-while-revalidate. This is a plain file read on purpose:
/// spawning `uv run python` just to reach the cache would cost most of what we're
/// trying to save.
enum PrepCache {
    /// The calendar account prep reads. Passed to the sidecar as `--account` so
    /// the cache file name always matches. Resolved like `recorder_config.py`:
    /// `RECORDER_ACCOUNT`, else `default_account` in the config file, else "work".
    static let account: String = {
        let env = ProcessInfo.processInfo.environment
        if let a = env["RECORDER_ACCOUNT"], !a.isEmpty { return a }
        let path = env["RECORDER_CONFIG"]
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".config/recorder/config.json").path
        if let data = FileManager.default.contents(atPath: (path as NSString).expandingTildeInPath),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let a = obj["default_account"] as? String, !a.isEmpty {
            return a
        }
        return "work"
    }()

    static func url(range: PrepRange) -> URL {
        FileManager.default
            .homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Recorder")
            .appendingPathComponent("prep-cache-\(account)-\(range.rawValue).json")
    }

    static func load(range: PrepRange) -> MeetingPrepResponse? {
        guard let data = try? Data(contentsOf: url(range: range)) else { return nil }
        guard let response = try? JSONDecoder().decode(MeetingPrepResponse.self, from: data) else {
            return nil
        }
        return response.schemaVersion == 2 ? response : nil
    }
}

/// Parse a link that came from calendar data. Anyone can send you an invite,
/// so only http(s) links are clickable; other schemes (file:, custom app
/// handlers) are dropped rather than handed to `Link`.
func webURL(_ string: String) -> URL? {
    guard let url = URL(string: string),
          let scheme = url.scheme?.lowercased(),
          scheme == "https" || scheme == "http" else { return nil }
    return url
}

/// Health of one Google OAuth token, as reported by `google_auth.py status`.
///
/// Surfaced in the UI because the failure mode here is silence: a credential
/// that fails quietly degrades prep and imports without ever raising an error
/// the user sees.
struct GoogleTokenStatus: Identifiable, Codable {
    var id: String { name }
    let name: String
    let purpose: String
    let usedBy: [String]
    let status: String
    let detail: String
    let path: String

    var isHealthy: Bool { status == "ok" }
}

/// One verified citation backing an answer. `recordingId` is guaranteed by the
/// sidecar to name a recording that exists on disk — unverifiable citations are
/// dropped there rather than shown, so this can be trusted to select.
struct MeetingAnswerCitation: Identifiable, Hashable, Codable {
    var id: String { recordingId + quote }
    let recordingId: String
    let date: String
    let title: String
    let quote: String
}

struct MeetingAnswer: Codable {
    let question: String
    let answer: String
    let citations: [MeetingAnswerCitation]
    let confidence: String
    let meetingsSearched: Int
    /// How many citations the sidecar rejected as naming a non-existent meeting.
    /// Surfaced because a model that fabricates one may have strayed elsewhere.
    let droppedCitations: Int
}

@MainActor
final class AppModel: ObservableObject {
    @Published var stage: PipelineStage = .idle
    @Published var elapsed: TimeInterval = 0
    /// Selected recording shown in the transcript pane. nil = live status / new recording placeholder.
    @Published var selectedID: String?
    @Published var selectedPrepID: String?
    @Published var prepItems: [MeetingPrepItem] = []
    @Published var prepRange: PrepRange = .today
    @Published var prepLastRefreshed: String?
    @Published var prepError: String?
    @Published var prepIsStale = false
    @Published var prepIsLoading = false
    /// True until the first refresh (or cache read) has produced something to show.
    @Published var prepHasLoadedOnce = false
    @Published var activityDetail: String = ""
    @Published var transcribeProgress: Double?
    @Published var transcribeHeartbeatAt: Date?
    // Cross-meeting Q&A.
    @Published var askSheetShown = false
    @Published var askQuestion: String = ""
    @Published var askAnswer: MeetingAnswer?
    @Published var askError: String?
    @Published var askIsRunning = false
    @Published var askHistory: [MeetingAnswer] = []
    /// Only unhealthy tokens — healthy ones need no UI.
    @Published var authIssues: [GoogleTokenStatus] = []
    let store = RecordingsStore()
    let completion = CompletionStore()

    private let recorder = SystemAudioRecorder()
    private var startedAt: Date?
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()
    /// When the last successful prep refresh completed, for foreground debouncing.
    private var prepRefreshedAt: Date?
    /// Don't re-gather on every window focus — cmd-tabbing shouldn't re-run notmuch.
    private let prepRefreshDebounce: TimeInterval = 300
    /// Persisted so the import debounce survives relaunches — otherwise quitting
    /// and reopening would re-import (and re-extract) every time.
    private var geminiImportedAt: Date? {
        get { UserDefaults.standard.object(forKey: "geminiImportedAt") as? Date }
        set { UserDefaults.standard.set(newValue, forKey: "geminiImportedAt") }
    }
    private let geminiImportInterval: TimeInterval = 6 * 60 * 60
    private var authCheckedAt: Date? {
        get { UserDefaults.standard.object(forKey: "authCheckedAt") as? Date }
        set { UserDefaults.standard.set(newValue, forKey: "authCheckedAt") }
    }
    /// A deep check forces a refresh on every token, so once a day is plenty —
    /// and it's what makes the check meaningful, since a cached access token can
    /// mask a revoked refresh token for up to an hour.
    private let authCheckInterval: TimeInterval = 24 * 60 * 60

    init() {
        refreshStore()
        // Re-render the view when the nested ObservableObjects change.
        completion.objectWillChange
            .sink { [weak self] in self?.objectWillChange.send() }
            .store(in: &cancellables)
        store.objectWillChange
            .sink { [weak self] in self?.objectWillChange.send() }
            .store(in: &cancellables)
        // Paint the last good response instantly, then revalidate in the background.
        applyCachedPrep()
        refreshMeetingPrep()
    }

    /// Show the sidecar's cached prep immediately, if any. Marked stale so the UI
    /// can say so until the background refresh lands.
    private func applyCachedPrep() {
        guard let response = PrepCache.load(range: prepRange) else { return }
        apply(response, isCache: true)
    }

    /// Each range has its own cache file, so a range switch gets the same
    /// paint-then-revalidate treatment as launch rather than blanking the list.
    func switchPrepRange() {
        prepItems = []
        prepHasLoadedOnce = false
        applyCachedPrep()
        refreshMeetingPrep()
    }

    /// Ambient refresh when the window comes forward. Debounced — see
    /// `refreshMeetingPrep(force:)`.
    func refreshMeetingPrepIfStale() {
        refreshMeetingPrep(force: false)
    }

    // MARK: - Cross-meeting Q&A

    func askMeetings() {
        let question = askQuestion.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, !askIsRunning else { return }
        askIsRunning = true
        askError = nil
        Task {
            defer { askIsRunning = false }
            guard let runner = PipelineRunner.locate() else {
                askError = "Could not find python/ sidecar directory."
                return
            }
            do {
                let data = try await runner.askMeetings(question)
                // The sidecar reports its own failures as {"error": ...} so the
                // user sees the cause instead of an opaque decode failure.
                if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let message = obj["error"] as? String {
                    askError = message
                    return
                }
                let answer = try JSONDecoder().decode(MeetingAnswer.self, from: data)
                askAnswer = answer
                askHistory.insert(answer, at: 0)
                askQuestion = ""
            } catch {
                askError = error.localizedDescription
            }
        }
    }

    /// Jump to the meeting behind a citation. Safe because the sidecar validated
    /// the id against the recordings on disk.
    func openCitation(_ citation: MeetingAnswerCitation) {
        guard store.items.contains(where: { $0.id == citation.recordingId }) else { return }
        selectRecording(citation.recordingId)
        askSheetShown = false
    }

    var isRecording: Bool {
        if case .recording = stage { return true }
        return false
    }

    var isBusy: Bool {
        switch stage {
        case .recording, .saving, .transcribing, .extractingTodos, .importingGemini: return true
        default: return false
        }
    }

    static func parseTranscribeProgress(_ line: String) -> (progress: Double, label: String)? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.contains("chunk"), trimmed.contains("/") else { return nil }
        let regex = try? NSRegularExpression(pattern: #"chunk\s+(\d+)\s*/\s*(\d+)s\s+\((\d+)%\)"#)
        guard
            let regex,
            let match = regex.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)),
            match.numberOfRanges >= 4
        else { return nil }

        func capture(_ index: Int) -> Int? {
            let range = match.range(at: index)
            guard let swiftRange = Range(range, in: trimmed) else { return nil }
            return Int(trimmed[swiftRange])
        }

        guard let current = capture(1), let total = capture(2), total > 0 else { return nil }
        let progress = min(max(Double(current) / Double(total), 0), 1)
        return (progress, "Transcribing chunk \(current) of \(total)")
    }

    static func isTranscribeHeartbeat(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return trimmed.hasPrefix("loading model") || trimmed.hasPrefix("transcribing")
            || trimmed.hasPrefix("mixed →") || trimmed.hasPrefix("chunk ")
    }

    var selectedRecording: RecordingItem? {
        guard let id = selectedID else { return nil }
        return store.items.first(where: { $0.id == id })
    }

    var selectedPrep: MeetingPrepItem? {
        guard let id = selectedPrepID else { return nil }
        return prepItems.first(where: { $0.id == id })
    }

    /// Non-skipped prep items bucketed by day, preserving chronological order.
    /// One group ("Today") when the range is today; multiple when it spans days.
    var prepGroups: [(label: String, items: [MeetingPrepItem])] {
        let visible = prepItems
            .filter { $0.prepState != "Skipped" }
            .sorted { ($0.startEpoch ?? 0) < ($1.startEpoch ?? 0) }
        var groups: [(label: String, items: [MeetingPrepItem])] = []
        for item in visible {
            let label = item.dayLabel ?? "Today"
            if groups.last?.label == label {
                groups[groups.count - 1].items.append(item)
            } else {
                groups.append((label, [item]))
            }
        }
        return groups
    }

    func selectPrep(_ id: String) {
        selectedPrepID = id
        selectedID = nil
    }

    func selectRecording(_ id: String) {
        selectedID = id
        selectedPrepID = nil
    }

    func toggle() {
        Task {
            if isRecording {
                await stopAndProcess()
            } else if !isBusy {
                await start()
            }
        }
    }

    /// Import Gemini Notes from email.
    ///
    /// `silent: true` is the ambient path: it leaves the current selection alone
    /// and reports failure to the log rather than the status line, so a
    /// background import can't yank the user out of what they're reading or
    /// paint an error over an idle app.
    func importGeminiNotes(silent: Bool = false) {
        guard !isBusy else { return }
        Task {
            stage = .importingGemini
            guard let runner = PipelineRunner.locate() else {
                stage = silent ? .idle : .failed("Could not find python/ sidecar directory.")
                return
            }
            do {
                _ = try await runner.importGeminiNotes()
                // Drive-attached notes land sooner than the email; the two
                // importers dedupe on calendar event id, so running both is
                // safe and covers whichever source arrives first.
                _ = try? await runner.importMeetNotes()
                store.refresh()
                if !silent {
                    selectedID = store.items.first?.id
                }
                stage = .done
                geminiImportedAt = Date()
                // New notes are new evidence for upcoming meetings.
                refreshMeetingPrep()
            } catch {
                if silent {
                    stage = .idle
                } else {
                    stage = .failed("import Gemini: \(error.localizedDescription)")
                }
            }
        }
    }

    /// Check every Google token and surface the broken ones.
    ///
    /// `force` skips the daily debounce — used by the banner's retry, so a user
    /// who has just re-authorized sees the banner clear immediately rather than
    /// waiting out the interval.
    func refreshAuthStatus(force: Bool = false) {
        if !force, let last = authCheckedAt,
           Date().timeIntervalSince(last) < authCheckInterval {
            return
        }
        Task {
            guard let runner = PipelineRunner.locate() else { return }
            do {
                let data = try await runner.googleAuthStatus(deep: true)
                let all = try JSONDecoder().decode([GoogleTokenStatus].self, from: data)
                authIssues = all.filter { !$0.isHealthy }
                authCheckedAt = Date()
            } catch {
                // A failure to *check* is not itself a broken credential; leave
                // whatever we last knew rather than crying wolf.
                authLog.error("auth status check failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// Ambient Gemini import when the window comes forward.
    ///
    /// Notes arrive minutes-to-hours after a meeting, so a few checks a day is
    /// ample — and each one costs a mail round-trip plus an extraction per new
    /// note.
    func importGeminiNotesIfStale() {
        guard !isBusy else { return }
        if let last = geminiImportedAt,
           Date().timeIntervalSince(last) < geminiImportInterval {
            return
        }
        importGeminiNotes(silent: true)
    }

    /// Refresh prep from the sidecar.
    ///
    /// `force: false` is the ambient path (window focus) and no-ops if we refreshed
    /// recently — gathering hits notmuch and the transcript archive, so it isn't
    /// free even when Bedrock synthesis is served from cache. Explicit user actions
    /// pass `force: true`.
    func refreshMeetingPrep(force: Bool = true) {
        if !force, let last = prepRefreshedAt,
           Date().timeIntervalSince(last) < prepRefreshDebounce {
            return
        }
        guard !prepIsLoading else { return }
        let range = prepRange
        prepIsLoading = true
        Task {
            defer { prepIsLoading = false }
            guard let runner = PipelineRunner.locate() else {
                prepError = "Could not find python/ sidecar directory."
                prepHasLoadedOnce = true
                return
            }
            do {
                let data = try await runner.fetchMeetingPrep(range: range.rawValue)
                let response = try JSONDecoder().decode(MeetingPrepResponse.self, from: data)
                guard response.schemaVersion == 2 else {
                    prepError = "Unsupported prep schema \(response.schemaVersion)"
                    prepHasLoadedOnce = true
                    return
                }
                // Ignore a response for a range the user has since switched away from.
                guard range == prepRange else { return }
                apply(response, isCache: false)
                prepRefreshedAt = Date()
            } catch {
                prepError = error.localizedDescription
                prepHasLoadedOnce = true
            }
        }
    }

    /// Fold a sidecar response into published state.
    ///
    /// Unlike the cache path, a live response replaces `prepItems` even when empty —
    /// "no meetings today" is a real answer, and holding onto yesterday's list would
    /// quietly show meetings that already happened.
    private func apply(_ response: MeetingPrepResponse, isCache: Bool) {
        prepError = response.error
        prepIsStale = isCache || (response.stale ?? false)
        prepLastRefreshed = response.generatedAt
        let allItems = response.items + (response.skippedItems ?? [])
        if isCache && allItems.isEmpty { return }
        prepItems = allItems
        prepHasLoadedOnce = true
        let fallback = response.items.first?.id ?? allItems.first?.id
        if selectedPrepID == nil && selectedID == nil {
            selectedPrepID = fallback
        } else if let current = selectedPrepID,
                  !allItems.contains(where: { $0.id == current }) {
            selectedPrepID = fallback
        }
    }

    func alwaysPrep(_ prep: MeetingPrepItem) {
        guard prep.prepState == "Skipped", !prep.title.isEmpty else { return }
        Task {
            guard let runner = PipelineRunner.locate() else {
                prepError = "Could not find python/ sidecar directory."
                return
            }
            do {
                _ = try await runner.allowlistPrepTitle(prep.title)
                refreshMeetingPrep()
            } catch {
                prepError = "allowlist: \(error.localizedDescription)"
            }
        }
    }

    func resumeProcessing(_ recording: RecordingItem) {
        guard !isBusy else { return }
        guard recording.transcriptURL == nil, let systemURL = recording.systemURL, let micURL = recording.micURL else {
            return
        }

        Task {
            stage = .transcribing
            activityDetail = "Resuming transcription"
            transcribeProgress = 0
            transcribeHeartbeatAt = Date()
            selectedID = recording.id
            selectedPrepID = nil
            startTranscribeMonitor()

            guard let runner = PipelineRunner.locate() else {
                timer?.invalidate(); timer = nil
                stage = .failed("Could not find python/ sidecar directory.")
                return
            }

            let stem = recording.id
            async let eventURL: URL? = runner.fetchMeeting(stem: stem, audio: systemURL)
            let transcriptURL: URL
            do {
                (_, transcriptURL) = try await runner.transcribe(system: systemURL, mic: micURL) { line in
                    Task { @MainActor in
                        self.transcribeHeartbeatAt = Date()
                        if let parsed = AppModel.parseTranscribeProgress(line) {
                            self.transcribeProgress = parsed.progress
                            self.activityDetail = parsed.label
                        } else if AppModel.isTranscribeHeartbeat(line) {
                            self.activityDetail = line
                        }
                    }
                }
            } catch {
                timer?.invalidate(); timer = nil
                stage = .failed("transcribe: \(error.localizedDescription)")
                store.refresh()
                return
            }

            stage = .extractingTodos
            activityDetail = "Extracting todos"
            do {
                try await extractTodosWithSeries(runner: runner, transcriptURL: transcriptURL, eventURL: await eventURL)
                stage = .done
                activityDetail = "Done"
                transcribeProgress = nil
                // New todos change the evidence future briefs are built from.
                refreshMeetingPrep()
            } catch {
                stage = .failed("todos: \(error.localizedDescription)")
                activityDetail = "Failed"
            }
            timer?.invalidate(); timer = nil
            refreshStore()
            if let id = store.items.first?.id {
                selectRecording(id)
            }
        }
    }

    /// Refresh the store and migrate any legacy index-based completion keys to
    /// the content-stable scheme (idempotent).
    func refreshStore() {
        store.refresh()
        let pairs = store.aggregatedTodos.map {
            (legacy: $0.legacyCompletionKey, stable: $0.completionKey)
        }
        completion.migrateLegacyKeys(pairs)
    }

    static func stem(fromTranscript url: URL) -> String {
        let name = url.lastPathComponent
        return name.hasSuffix(".transcript.json")
            ? String(name.dropLast(".transcript.json".count))
            : url.deletingPathExtension().lastPathComponent
    }

    /// Extract todos with recurring-series reconciliation: carry forward this
    /// series' still-open items so the model dedups against them, then auto-close
    /// any it reports resolved.
    private func extractTodosWithSeries(
        runner: PipelineRunner,
        transcriptURL: URL,
        eventURL: URL?
    ) async throws {
        let stem = Self.stem(fromTranscript: transcriptURL)
        // The recording's event.json must be visible for the series lookup.
        refreshStore()
        let event = store.items.first(where: { $0.id == stem })?.event
        let priors = store.priorOpenTodos(
            forStem: stem, event: event, isDone: { completion.isDone($0) }
        )
        var priorURL: URL?
        if !priors.isEmpty,
           let data = try? JSONSerialization.data(withJSONObject: priors, options: []) {
            let u = AppPaths.recordingsDir.appendingPathComponent("\(stem).priors.json")
            if (try? data.write(to: u)) != nil { priorURL = u }
        }
        defer { if let priorURL { try? FileManager.default.removeItem(at: priorURL) } }

        let (extraction, _) = try await runner.extractTodos(
            transcriptURL: transcriptURL, eventURL: eventURL, priorTodosURL: priorURL
        )
        if let resolved = extraction.resolvedPrior, !resolved.isEmpty {
            completion.suggestResolved(resolved.map { ($0.id, $0.reason ?? "") })
        }
    }

    private func start() async {
        do {
            _ = try await recorder.start()
            startedAt = Date()
            stage = .recording
            elapsed = 0
            activityDetail = ""
            transcribeProgress = nil
            transcribeHeartbeatAt = nil
            selectedID = nil // show live status instead of an old recording
            selectedPrepID = nil
            timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
                Task { @MainActor in
                    guard let self, let s = self.startedAt else { return }
                    self.elapsed = Date().timeIntervalSince(s)
                }
            }
        } catch {
            stage = .failed(error.localizedDescription)
        }
    }

    private func stopAndProcess() async {
        stage = .saving
        activityDetail = "Saving recording"
        timer?.invalidate(); timer = nil
        let out: SystemAudioRecorder.Output
        do {
            out = try await recorder.stop()
        } catch {
            stage = .failed("stop: \(error.localizedDescription)")
            return
        }

        guard let runner = PipelineRunner.locate() else {
            stage = .failed("Could not find python/ sidecar directory.")
            return
        }

        // Calendar lookup runs in parallel with transcription — both depend on
        // the recording but not on each other, and the lookup is cheap (~1 s).
        let stem = out.systemURL.lastPathComponent
            .replacingOccurrences(of: ".system.wav", with: "")
        async let eventURL: URL? = runner.fetchMeeting(stem: stem, audio: out.systemURL)

        stage = .transcribing
        activityDetail = "Transcribing"
        transcribeProgress = 0
        transcribeHeartbeatAt = Date()
        startTranscribeMonitor()
        let (_, transcriptURL): (TranscriptResult, URL)
        do {
            (_, transcriptURL) = try await runner.transcribe(system: out.systemURL, mic: out.micURL) { line in
                Task { @MainActor in
                    self.transcribeHeartbeatAt = Date()
                    if let parsed = AppModel.parseTranscribeProgress(line) {
                        self.transcribeProgress = parsed.progress
                        self.activityDetail = parsed.label
                    } else if AppModel.isTranscribeHeartbeat(line) {
                        self.activityDetail = line
                    }
                }
            }
        } catch {
            timer?.invalidate(); timer = nil
            stage = .failed("transcribe: \(error.localizedDescription)")
            store.refresh() // still surface the partial recording
            return
        }

        stage = .extractingTodos
        activityDetail = "Extracting todos"
        do {
            try await extractTodosWithSeries(runner: runner, transcriptURL: transcriptURL, eventURL: await eventURL)
            stage = .done
            activityDetail = "Done"
            transcribeProgress = nil
            // New todos change the evidence future briefs are built from.
            refreshMeetingPrep()
        } catch {
            timer?.invalidate(); timer = nil
            stage = .failed("todos: \(error.localizedDescription)")
            activityDetail = "Failed"
        }
        timer?.invalidate(); timer = nil
        refreshStore()
        // Select the most recent recording (the one we just finished).
        if let id = store.items.first?.id {
            selectRecording(id)
        }
    }

    private func startTranscribeMonitor() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.stage == .transcribing else { return }
                guard let last = self.transcribeHeartbeatAt else { return }
                let staleFor = Date().timeIntervalSince(last)
                if staleFor >= 60 {
                    let minutes = Int(staleFor) / 60
                    let seconds = Int(staleFor) % 60
                    self.activityDetail = String(format: "No transcription progress for %dm %02ds", minutes, seconds)
                }
            }
        }
    }
}
