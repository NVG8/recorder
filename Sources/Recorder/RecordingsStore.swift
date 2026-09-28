import Foundation
import OSLog

private let log = Logger(subsystem: "co.nvg8.recorder", category: "store")

/// Scans the recordings directory and exposes the full history as a list,
/// plus a flat aggregated todos feed across every past recording.
///
/// Source of truth is the filesystem (`*.transcript.json` + `*.todos.json` in
/// `~/Library/Application Support/Recorder/recordings/`). Items are derived
/// solely from those files so the app can be quit/restarted without losing
/// state, and nothing extra needs to be kept in sync.
@MainActor
final class RecordingsStore: ObservableObject {
    @Published private(set) var items: [RecordingItem] = []

    /// Todos from every recording, newest first, each tagged with its source.
    /// `id` is the SwiftUI identity for the current render ("<stem>:<index>").
    /// `completionKey` is content-stable so a "done" mark survives re-extraction
    /// (re-extraction reorders/rewords items, which would break an index key).
    struct AggregatedTodo: Identifiable, Hashable {
        let id: String
        let todo: Todo
        let sourceId: String
        let sourceDate: Date
        let index: Int

        /// Content-stable key: "<stem>:t<hash(text)>". Survives re-extraction.
        var completionKey: String { "\(sourceId):t\(RecordingsStore.stableHash(todo.text))" }
        /// Pre-content-key scheme; used once to migrate existing completions.
        var legacyCompletionKey: String { "\(sourceId):\(index)" }
    }

    var aggregatedTodos: [AggregatedTodo] {
        items.flatMap { item in
            item.todos.enumerated().map { idx, todo in
                AggregatedTodo(
                    id: "\(item.id):\(idx)",
                    todo: todo,
                    sourceId: item.id,
                    sourceDate: item.createdAt,
                    index: idx
                )
            }
        }
    }

    /// Deterministic (non-salted) djb2 hash so completion keys are stable across
    /// app launches — Swift's built-in Hasher is randomized per process.
    nonisolated static func stableHash(_ s: String) -> String {
        var h: UInt64 = 5381
        for byte in s.utf8 { h = (h &* 33) &+ UInt64(byte) }
        return String(h, radix: 36)
    }

    // MARK: - Recurring-series reconciliation

    /// Still-open todos from earlier recordings in the same recurring series as
    /// `newStem`, shaped for `extract_todos.py --prior-todos`. The model dedups
    /// new extraction against these and reports which it resolved.
    ///
    /// `isDone` is injected (the completion state lives in CompletionStore) so
    /// only genuinely-open items carry forward. Deduped by todo text, newest
    /// kept, capped to keep the prompt bounded.
    func priorOpenTodos(
        forStem newStem: String,
        event newEvent: MeetingEvent?,
        isDone: (String) -> Bool,
        limit: Int = 40
    ) -> [[String: String]] {
        guard let newItem = items.first(where: { $0.id == newStem }) else { return [] }
        let series = items.filter {
            $0.id != newStem
                && $0.createdAt <= newItem.createdAt
                && Self.sameSeries(newEvent, $0.event)
        }
        var seenText = Set<String>()
        var out: [[String: String]] = []
        for item in series.sorted(by: { $0.createdAt > $1.createdAt }) {
            for (idx, todo) in item.todos.enumerated() {
                let key = "\(item.id):t\(Self.stableHash(todo.text))"
                if isDone(key) { continue }
                let norm = Self.normalizeText(todo.text)
                if norm.isEmpty || seenText.contains(norm) { continue }
                seenText.insert(norm)
                var rec: [String: String] = ["id": key, "text": todo.text]
                if let o = todo.owner, !o.isEmpty { rec["owner"] = o }
                if let d = todo.due, !d.isEmpty { rec["due"] = d }
                rec["meeting_date"] = Self.dateStamp(item.createdAt)
                if let t = item.event?.title, !t.isEmpty { rec["meeting_title"] = t }
                out.append(rec)
                if out.count >= limit { return out }
                _ = idx
            }
        }
        return out
    }

    /// Other recordings related to `item`: same recurring series, or sharing
    /// non-self attendees. Newest first. Powers the "Related meetings" panel.
    func relatedRecordings(to item: RecordingItem, limit: Int = 8) -> [RecordingItem] {
        items
            .filter { $0.id != item.id && Self.related(item.event, $0.event) }
            .sorted { $0.createdAt > $1.createdAt }
            .prefix(limit)
            .map { $0 }
    }

    /// Looser than `sameSeries`: same series, OR ≥1 shared non-self attendee
    /// (so "meetings with these people" surfaces, not just the exact series).
    static func related(_ a: MeetingEvent?, _ b: MeetingEvent?) -> Bool {
        if sameSeries(a, b) { return true }
        guard let a, let b else { return false }
        return !attendeeEmails(a).isDisjoint(with: attendeeEmails(b))
    }

    /// Two meetings are the same recurring series when their titles match
    /// (normalized) or their non-self attendee sets strongly overlap.
    static func sameSeries(_ a: MeetingEvent?, _ b: MeetingEvent?) -> Bool {
        guard let a, let b else { return false }
        let ta = normalizeText(a.title ?? ""), tb = normalizeText(b.title ?? "")
        if !ta.isEmpty && ta == tb { return true }
        let ea = attendeeEmails(a), eb = attendeeEmails(b)
        guard ea.count >= 2 && eb.count >= 2 else { return false }
        let shared = ea.intersection(eb)
        let union = ea.union(eb)
        return shared.count >= 2 && Double(shared.count) / Double(union.count) >= 0.6
    }

    private static func attendeeEmails(_ e: MeetingEvent) -> Set<String> {
        Set((e.attendees ?? [])
            .filter { $0.`self` != true }
            .map { $0.email.lowercased() }
            .filter { !$0.isEmpty })
    }

    static func normalizeText(_ s: String) -> String {
        s.lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    private static func dateStamp(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: d)
    }

    func refresh() {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(at: AppPaths.recordingsDir,
                                                        includingPropertiesForKeys: [.creationDateKey],
                                                        options: [.skipsHiddenFiles])
        else {
            items = []
            return
        }

        // Group files by recording stem (`rec-YYYYMMDD-HHMMSS`). Any file whose
        // name starts with the stem belongs to that recording.
        var stems = Set<String>()
        for url in entries {
            let name = url.lastPathComponent
            if let stem = Self.stem(forFilename: name) { stems.insert(stem) }
        }

        let decoder = JSONDecoder()
        var loaded: [RecordingItem] = []
        for stem in stems {
            let dir = AppPaths.recordingsDir
            let systemURL = dir.appendingPathComponent("\(stem).system.wav")
            let micURL = dir.appendingPathComponent("\(stem).mic.wav")
            let mixedURL = dir.appendingPathComponent("\(stem).mixed.wav")
            let transcriptURL = dir.appendingPathComponent("\(stem).transcript.json")
            let todosURL = dir.appendingPathComponent("\(stem).todos.json")
            let eventURL = dir.appendingPathComponent("\(stem).event.json")

            var transcript = ""
            if let data = try? Data(contentsOf: transcriptURL),
               let res = try? decoder.decode(TranscriptResult.self, from: data) {
                transcript = res.text
            }
            var summary = ""
            var todos: [Todo] = []
            var decisions: [Decision] = []
            if let data = try? Data(contentsOf: todosURL),
               let res = try? decoder.decode(TodoExtraction.self, from: data) {
                summary = res.summary
                todos = res.todos
                decisions = res.decisions ?? []
            }
            var event: MeetingEvent? = nil
            if let data = try? Data(contentsOf: eventURL),
               let res = try? decoder.decode(MeetingEvent.self, from: data),
               res.title?.isEmpty == false {
                event = res
            }

            let item = RecordingItem(
                id: stem,
                createdAt: Self.date(fromStem: stem) ?? (try? systemURL.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date.distantPast,
                systemURL: fm.fileExists(atPath: systemURL.path) ? systemURL : nil,
                micURL: fm.fileExists(atPath: micURL.path) ? micURL : nil,
                mixedURL: fm.fileExists(atPath: mixedURL.path) ? mixedURL : nil,
                transcriptURL: fm.fileExists(atPath: transcriptURL.path) ? transcriptURL : nil,
                todosURL: fm.fileExists(atPath: todosURL.path) ? todosURL : nil,
                eventURL: fm.fileExists(atPath: eventURL.path) ? eventURL : nil,
                transcript: transcript,
                summary: summary,
                todos: todos,
                decisions: decisions,
                event: event
            )
            loaded.append(item)
        }
        loaded.sort { $0.createdAt > $1.createdAt }
        items = loaded
        log.info("refreshed: \(loaded.count) recordings")
    }

    private static func stem(forFilename name: String) -> String? {
        guard name.hasPrefix("rec-") else { return nil }
        // Strip everything after the first `.` so e.g. `rec-20260514-130301.mic.wav` → `rec-20260514-130301`.
        if let dot = name.firstIndex(of: ".") {
            return String(name[..<dot])
        }
        return name
    }

    private static func date(fromStem stem: String) -> Date? {
        // stem like rec-20260514-130301
        let parts = stem.split(separator: "-")
        guard parts.count == 3 else { return nil }
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.date(from: "\(parts[1])-\(parts[2])")
    }
}
