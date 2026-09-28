import Foundation

struct Todo: Codable, Identifiable, Hashable {
    var id = UUID()
    let text: String
    let owner: String?
    let due: String?
    let context: String?
    /// Relationship to the user: "mine" | "waiting_on" | "fyi". Optional —
    /// absent on todos extracted before bucketing existed. Treated as a soft
    /// hint (attribution is heuristic without speaker diarization).
    let bucket: String?
    /// Due date resolved to ISO `YYYY-MM-DD` when derivable; `due` keeps the
    /// verbatim phrase. Used for sorting.
    let dueISO: String?

    enum CodingKeys: String, CodingKey {
        case text, owner, due, context, bucket
        case dueISO = "due_iso"
    }

    /// Coarse grouping for the UI, defaulting unknown/legacy todos to `.fyi`.
    enum Bucket: String { case mine, waiting_on, fyi }
    var bucketKind: Bucket { Bucket(rawValue: bucket ?? "") ?? .fyi }
}

/// One prior open todo this meeting marked done (id echoes the completion key
/// we passed in via `--prior-todos`).
struct ResolvedPrior: Codable, Hashable {
    let id: String
    let reason: String?
}

/// A settled choice the group landed on, as distinct from a task someone owes.
/// `context` is the snippet of transcript it was settled in.
struct Decision: Codable, Identifiable, Hashable {
    var id: String { text }
    let text: String
    let context: String?
}

struct TodoExtraction: Codable {
    let summary: String
    let todos: [Todo]
    /// Prior-series items this meeting resolved. Absent on older files.
    let resolvedPrior: [ResolvedPrior]?
    /// Absent on recordings extracted before decisions were captured, so the
    /// UI must treat "no decisions" and "not yet extracted" the same way.
    let decisions: [Decision]?

    enum CodingKeys: String, CodingKey {
        case summary, todos, decisions
        case resolvedPrior = "resolved_prior"
    }
}

struct TranscriptSegment: Codable, Hashable {
    let start: Double
    let end: Double
    let text: String
    /// "you" (microphone) or "remote" (system output) when the transcript was
    /// speaker-attributed; nil for legacy/single-track transcripts.
    let speaker: String?
}

struct TranscriptResult: Codable {
    let text: String
    let segments: [TranscriptSegment]
}

struct MeetingAttendee: Codable, Hashable {
    let email: String
    let name: String?
    let organizer: Bool?
    let `self`: Bool?

    var label: String {
        if let n = name, !n.isEmpty { return n }
        return email
    }
}

struct MeetingEvent: Codable, Hashable {
    let title: String?
    let start: String?
    let end: String?
    let attendees: [MeetingAttendee]?
    let description: String?
    let meet_url: String?
    let calendar_id: String?
    let event_id: String?
    let match_confidence: Double?
}

/// A single past recording with its transcript + todos loaded from disk.
struct RecordingItem: Identifiable, Hashable {
    let id: String                 // the rec-YYYYMMDD-HHMMSS stem
    let createdAt: Date
    let systemURL: URL?
    let micURL: URL?
    let mixedURL: URL?
    let transcriptURL: URL?
    let todosURL: URL?
    let eventURL: URL?
    let transcript: String
    let summary: String
    let todos: [Todo]
    let decisions: [Decision]
    let event: MeetingEvent?

    var displayDate: String {
        let f = DateFormatter()
        f.dateFormat = "MMM d, h:mm a"
        return f.string(from: createdAt)
    }

    /// Sidebar headline: meeting title when present, otherwise the timestamp.
    var headline: String {
        if let t = event?.title, !t.isEmpty { return t }
        return displayDate
    }

    /// Lowercased searchable blob: title, attendees, summary, transcript, and
    /// every todo's text/owner/context. Built lazily per query (not stored).
    private var searchBlob: String {
        var parts = [event?.title ?? "", summary, transcript]
        for a in event?.attendees ?? [] {
            parts.append(a.name ?? "")
            parts.append(a.email)
        }
        for d in decisions {
            parts.append(d.text)
            if let c = d.context { parts.append(c) }
        }
        for t in todos {
            parts.append(t.text)
            if let o = t.owner { parts.append(o) }
            if let c = t.context { parts.append(c) }
        }
        return parts.joined(separator: "\n").lowercased()
    }

    /// True when every whitespace-separated term in `query` appears somewhere in
    /// this recording (case-insensitive AND match). Empty query matches all.
    func matches(_ query: String) -> Bool {
        let terms = query.lowercased().split(whereSeparator: { $0.isWhitespace }).map(String.init)
        guard !terms.isEmpty else { return true }
        let blob = searchBlob
        return terms.allSatisfy { blob.contains($0) }
    }

    /// Single-line preview (first ~80 chars of transcript).
    var preview: String {
        if transcriptURL == nil {
            if mixedURL != nil {
                return "(transcription pending)"
            }
            return "(recording captured)"
        }
        let t = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return "(no speech detected)" }
        return t.count > 80 ? String(t.prefix(80)) + "…" : t
    }

    /// Coarse status for recordings that have not fully finished processing.
    var processingStatus: String {
        if transcriptURL != nil {
            return todosURL != nil ? "Ready" : "Transcript ready"
        }
        if mixedURL != nil {
            return "Pending transcription"
        }
        if systemURL != nil || micURL != nil {
            return "Saving recording"
        }
        return "Captured"
    }
}

enum PipelineStage: Equatable {
    case idle
    case recording
    case saving
    case transcribing
    case extractingTodos
    case importingGemini
    case done
    case failed(String)

    var label: String {
        switch self {
        case .idle: return "Idle"
        case .recording: return "Recording"
        case .saving: return "Saving"
        case .transcribing: return "Transcribing"
        case .extractingTodos: return "Extracting todos"
        case .importingGemini: return "Importing Gemini"
        case .done: return "Done"
        case .failed(let m): return "Failed: \(m)"
        }
    }
}

struct AppPaths {
    static let supportDir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = base.appendingPathComponent("Recorder", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    static let recordingsDir: URL = {
        let dir = supportDir.appendingPathComponent("recordings", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()
}
