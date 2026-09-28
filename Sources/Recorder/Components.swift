import SwiftUI
import AppKit

/// Persistent notice that a Google credential has stopped working.
///
/// Deliberately not dismissible: a dead token silently degrades prep, imports,
/// and the weekly update, and the last one went unnoticed for five months. It
/// clears itself when the credential is fixed.
struct AuthIssueBanner: View {
    @ObservedObject var model: AppModel
    @State private var copied = false

    private let fixCommand =
        "cd \(PipelineRunner.locate()?.pythonDir.path ?? "python") && uv run python google_auth.py reauth --all"

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "key.slash")
                    .foregroundStyle(.orange)
                Text(headline)
                    .font(.callout.weight(.medium))
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(fixCommand, forType: .string)
                    copied = true
                } label: {
                    Label(copied ? "Copied" : "Copy fix", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                Button("Re-check") { model.refreshAuthStatus(force: true) }
                    .font(.caption)
                    .buttonStyle(.bordered)
            }
            ForEach(model.authIssues) { issue in
                Text("\(issue.name) — \(issue.purpose). Affects: \(issue.usedBy.joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12))
    }

    private var headline: String {
        let names = model.authIssues.map(\.name).joined(separator: ", ")
        return model.authIssues.count == 1
            ? "Google credential needs re-authorizing: \(names)"
            : "\(model.authIssues.count) Google credentials need re-authorizing: \(names)"
    }
}

/// Ask a question across every recorded meeting, with citations you can click
/// through to the source recording.
struct AskMeetingsView: View {
    @ObservedObject var model: AppModel
    @FocusState private var questionFocused: Bool

    private static let examples = [
        "What did we decide about the pricing change?",
        "Which partners raised pricing objections, and what did they want?",
        "What am I still waiting on from other people?",
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let error = model.askError {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .font(.callout)
                            .foregroundStyle(.orange)
                    }
                    if model.askIsRunning {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Reading every meeting…")
                        }
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    }
                    if let answer = model.askAnswer {
                        answerBody(answer)
                    } else if !model.askIsRunning && model.askError == nil {
                        emptyState
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(width: 720, height: 560)
        .onAppear { questionFocused = true }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkles")
                .foregroundStyle(.secondary)
            TextField("Ask about any meeting…", text: $model.askQuestion)
                .textFieldStyle(.plain)
                .font(.system(size: 15))
                .focused($questionFocused)
                .onSubmit { model.askMeetings() }
            if model.askIsRunning {
                ProgressView().controlSize(.small)
            } else {
                Button("Ask") { model.askMeetings() }
                    .keyboardShortcut(.return, modifiers: [])
                    .disabled(model.askQuestion.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            Button {
                model.askSheetShown = false
            } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Ask across all \(model.store.items.count) recorded meetings.")
                .font(.callout)
                .foregroundStyle(.secondary)
            ForEach(Self.examples, id: \.self) { example in
                Button {
                    model.askQuestion = example
                    model.askMeetings()
                } label: {
                    Text(example)
                        .font(.callout)
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.link)
            }
        }
    }

    @ViewBuilder
    private func answerBody(_ answer: MeetingAnswer) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(answer.question)
                .font(.headline)

            Text(.init(answer.answer))
                .font(.body)
                .textSelection(.enabled)

            HStack(spacing: 10) {
                Label(answer.confidence.capitalized, systemImage: confidenceIcon(answer.confidence))
                    .foregroundStyle(confidenceColor(answer.confidence))
                Text("\(answer.meetingsSearched) meetings searched")
                    .foregroundStyle(.secondary)
                if answer.droppedCitations > 0 {
                    // The sidecar caught the model naming meetings that don't
                    // exist. Say so — it's a signal to read the answer harder.
                    Label(
                        "\(answer.droppedCitations) unverifiable citation\(answer.droppedCitations == 1 ? "" : "s") discarded",
                        systemImage: "exclamationmark.triangle"
                    )
                    .foregroundStyle(.orange)
                }
            }
            .font(.caption)

            if !answer.citations.isEmpty {
                Divider()
                Text("Sources")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(answer.citations) { citation in
                    Button {
                        model.openCitation(citation)
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(citation.date)
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.secondary)
                                Text(citation.title)
                                    .font(.callout.weight(.medium))
                                Image(systemName: "arrow.up.right.square")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            if !citation.quote.isEmpty {
                                Text("“\(citation.quote)”")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .multilineTextAlignment(.leading)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func confidenceIcon(_ c: String) -> String {
        switch c {
        case "high": return "checkmark.seal"
        case "low": return "questionmark.circle"
        default: return "info.circle"
        }
    }

    private func confidenceColor(_ c: String) -> Color {
        switch c {
        case "high": return .green
        case "low": return .orange
        default: return .secondary
        }
    }
}

struct MeetingPrepRow: View {
    let item: MeetingPrepItem
    let isSelected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(item.timeRange)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 72, alignment: .leading)
                Text(item.title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(2)
            }
            HStack(spacing: 8) {
                Label(item.prepState, systemImage: "doc.text")
                Label("\(item.relatedTranscripts.count)", systemImage: "waveform")
                Label("\(item.relatedEmails.count)", systemImage: "envelope")
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)
        }
        .padding(6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(isSelected ? Color.accentColor.opacity(0.14) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct RecordingRow: View {
    let item: RecordingItem
    let isSelected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(item.headline)
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(2)
            if item.event?.title != nil {
                Text(item.displayDate)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Text(item.preview)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
            HStack(spacing: 8) {
                if !item.todos.isEmpty {
                    Label("\(item.todos.count)", systemImage: "checkmark.circle")
                }
                if let n = item.event?.attendees?.count, n > 0 {
                    Label("\(n)", systemImage: "person.2")
                }
                Label(item.processingStatus, systemImage: item.transcriptURL == nil ? "hourglass" : "checkmark.circle")
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)
        }
        .padding(6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(isSelected ? Color.accentColor.opacity(0.14) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct PrepSection<Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Label(title, systemImage: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
            content
        }
    }
}

struct BulletList: View {
    let items: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(items, id: \.self) { item in
                HStack(alignment: .top, spacing: 8) {
                    Text("•")
                        .foregroundStyle(.secondary)
                    Text(item)
                        .font(.callout)
                        .textSelection(.enabled)
                }
            }
        }
    }
}

struct RelatedGroup: View {
    let title: String
    let icon: String
    let items: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
            ForEach(items, id: \.self) { item in
                Text(item)
                    .font(.callout)
                    .lineLimit(3)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
                    .textSelection(.enabled)
            }
        }
    }
}

struct PrepAttendeeList: View {
    let attendees: [MeetingPrepAttendee]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(attendees) { attendee in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(attendee.label)
                        .font(.callout.weight(.semibold))
                    if !attendee.email.isEmpty {
                        Text(attendee.email)
                            .font(.callout.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    if !attendee.company.isEmpty {
                        Text("- \(attendee.company)")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

struct MeetingHeaderView: View {
    let event: MeetingEvent
    let fallbackDate: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(event.title ?? "Meeting").font(.headline).lineLimit(2)
                Spacer()
                Text(fallbackDate).font(.caption).foregroundStyle(.secondary)
            }
            if let attendees = event.attendees, !attendees.isEmpty {
                AttendeeChips(attendees: attendees)
            }
            if let meet = event.meet_url, let url = webURL(meet) {
                Link(destination: url) {
                    Label(meet, systemImage: "video")
                        .font(.caption)
                }
                .lineLimit(1)
            }
        }
    }
}

struct AttendeeChips: View {
    let attendees: [MeetingAttendee]

    private let columns = [GridItem(.adaptive(minimum: 110, maximum: 220), spacing: 6)]

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 6) {
            ForEach(attendees, id: \.email) { a in
                HStack(spacing: 4) {
                    Image(systemName: a.organizer == true ? "person.crop.circle.badge.checkmark" : "person.crop.circle")
                        .foregroundStyle(.secondary)
                    Text(a.label)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Color.gray.opacity(0.12), in: Capsule())
                .help(a.email)
            }
        }
    }
}

struct TodoRow: View {
    let todo: Todo
    let sourceLabel: String
    let isDone: Bool
    let isFromSelected: Bool
    /// Non-nil when a later meeting suggested this item is done (the reason).
    var resolvedReason: String? = nil
    let onToggle: () -> Void
    let onSelect: () -> Void
    var onConfirmResolved: () -> Void = {}
    var onKeepOpen: () -> Void = {}

    /// Soft relationship hint. `.fyi` (the default/legacy case) shows no chip to
    /// keep the list clean — only the actionable "mine"/"waiting" stand out.
    private var bucketChip: (label: String, color: Color)? {
        switch todo.bucketKind {
        case .mine: return ("Mine", .accentColor)
        case .waiting_on: return ("Waiting", .orange)
        case .fyi: return nil
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
        HStack(alignment: .top, spacing: 10) {
            Button(action: onToggle) {
                Image(systemName: isDone ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 18))
                    .foregroundStyle(isDone ? Color.green : Color.secondary)
                    .padding(.top, 1)
            }
            .buttonStyle(.plain)
            .help(isDone ? "Mark as not done" : "Mark as done")

            Button(action: onSelect) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(todo.text)
                        .font(.body)
                        .strikethrough(isDone, color: .secondary)
                        .foregroundStyle(isDone ? .secondary : .primary)
                        .multilineTextAlignment(.leading)
                    HStack(spacing: 8) {
                        if let chip = bucketChip {
                            Text(chip.label)
                                .font(.caption2.weight(.medium))
                                .foregroundStyle(chip.color)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 1)
                                .background(chip.color.opacity(0.15), in: Capsule())
                        }
                        if let owner = todo.owner, !owner.isEmpty {
                            Label(owner, systemImage: "person")
                        }
                        if let due = todo.due, !due.isEmpty {
                            Label(due, systemImage: "calendar")
                        }
                        Label(sourceLabel, systemImage: "waveform")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    if let ctx = todo.context, !ctx.isEmpty {
                        Text(ctx)
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                            .italic()
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.plain)
        }
        if let reason = resolvedReason {
            resolvedBanner(reason)
        }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            (resolvedReason != nil
                ? Color.orange.opacity(0.10)
                : (isFromSelected ? Color.accentColor.opacity(0.10) : Color.gray.opacity(0.08))),
            in: RoundedRectangle(cornerRadius: 8)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.orange.opacity(resolvedReason != nil ? 0.4 : 0), lineWidth: 1)
        )
        .opacity(isDone ? 0.65 : 1.0)
    }

    @ViewBuilder
    private func resolvedBanner(_ reason: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label {
                Text("A later meeting suggests this is done"
                     + (reason.isEmpty ? "." : ": \(reason)"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
            } icon: {
                Image(systemName: "sparkles")
                    .foregroundStyle(.orange)
            }
            HStack(spacing: 8) {
                Button("Mark done", action: onConfirmResolved)
                    .controlSize(.small)
                    .buttonStyle(.borderedProminent)
                Button("Keep open", action: onKeepOpen)
                    .controlSize(.small)
                    .buttonStyle(.bordered)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
    }
}
