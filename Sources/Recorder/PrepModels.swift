import Foundation

struct MeetingPrepResponse: Codable {
    let schemaVersion: Int
    let generatedAt: String?
    let range: String?
    let items: [MeetingPrepItem]
    let skippedItems: [MeetingPrepItem]?
    let stale: Bool?
    let error: String?
}

struct MeetingPrepAction: Identifiable, Hashable, Codable {
    var id = UUID()
    let owner: String
    let text: String
    let source: String

    enum CodingKeys: String, CodingKey {
        case owner, text, source
    }
}

struct MeetingPrepAttendee: Identifiable, Hashable, Codable {
    let name: String
    let email: String
    let company: String

    var id: String {
        if !email.isEmpty { return email }
        return name
    }

    var label: String {
        if !name.isEmpty { return name }
        return email
    }
}

struct MeetingPrepItem: Identifiable, Hashable, Codable {
    let id: String
    let title: String
    let timeRange: String
    var location: String? = nil
    var calendarLink: String? = nil
    var attendees: [MeetingPrepAttendee]? = nil
    var skipReason: String? = nil
    var sourceState: String? = nil
    var prepSources: [String] = []
    var briefPath: String? = nil
    var background: [String]? = nil
    var suggestedAsk: [MeetingPrepAction]? = nil
    var dayLabel: String? = nil
    var startEpoch: Double? = nil
    let prepState: String
    let why: String
    let leftOff: [String]
    let actions: [MeetingPrepAction]
    let talkingPoints: [String]
    let openQuestions: [String]
    let relatedTranscripts: [String]
    let relatedEmails: [String]
    let context: [String]

    /// True when a model wrote this item's prose (a daily brief or
    /// Bedrock synthesis) rather than it being assembled from raw sources.
    ///
    /// Drives section naming: authored prose yields a real "Suggested Ask", while
    /// deterministic prep can only offer other people's open items from past
    /// meetings — calling those an "ask" for the reader misrepresents them.
    var hasAuthoredNarrative: Bool {
        sourceState == "Brief" || sourceState == "Synthesized"
    }

    var copyText: String {
        var lines = ["# \(title)", "", "- When: \(timeRange)"]
        if let location, !location.isEmpty {
            lines.append("- Location: \(location)")
        }
        if let calendarLink, !calendarLink.isEmpty {
            lines.append("- Calendar: \(calendarLink)")
        }
        if let skipReason, !skipReason.isEmpty {
            lines.append("")
            lines.append("## Skip Reason")
            lines.append(skipReason)
            return lines.joined(separator: "\n")
        }
        if let attendees, !attendees.isEmpty {
            lines.append("")
            lines.append("## Attendees")
            lines.append(contentsOf: attendees.map { "- \($0.label)\($0.email.isEmpty ? "" : " <\($0.email)>")" })
        }
        lines.append("")
        lines.append("## Why This Matters")
        lines.append(why)
        let backgroundItems = background ?? leftOff
        if !backgroundItems.isEmpty {
            lines.append("")
            lines.append("## Background")
            lines.append(contentsOf: backgroundItems.map { "- \($0)" })
        }
        let asks = suggestedAsk ?? actions
        if !asks.isEmpty {
            lines.append("")
            lines.append("## Suggested Ask")
            lines.append(contentsOf: asks.map { "- \($0.text)" })
        }
        if !talkingPoints.isEmpty {
            lines.append("")
            lines.append("## Talking Points")
            lines.append(contentsOf: talkingPoints.map { "- \($0)" })
        }
        if !openQuestions.isEmpty {
            lines.append("")
            lines.append("## Open Questions")
            lines.append(contentsOf: openQuestions.map { "- \($0)" })
        }
        return lines.joined(separator: "\n")
    }
}
