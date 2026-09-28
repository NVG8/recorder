import SwiftUI

struct ContentView: View {
    @ObservedObject var model: AppModel
    @State private var relatedFilter = "All"
    /// Soft bucket filter for the Todos pane. Defaults to All — attribution is a
    /// heuristic (no speaker diarization), so buckets are hints, not a hard split.
    @State private var bucketFilter = "All"
    /// Local keyword search over recordings (title/transcript/summary/todos).
    @State private var recordingsSearch = ""
    /// The full transcript is collapsed by default — it's an artifact behind a
    /// link, not the main view. Expanded per-recording on demand.
    @State private var transcriptExpanded = false

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
        } detail: {
            VStack(spacing: 0) {
                header
                if !model.authIssues.isEmpty {
                    Divider()
                    AuthIssueBanner(model: model)
                }
                Divider()
                HSplitView {
                    primaryPane
                        .frame(minWidth: 320)
                    secondaryPane
                        .frame(minWidth: 300)
                }
            }
        }
        .frame(minWidth: 980, minHeight: 560)
        .onChange(of: model.selectedID) { transcriptExpanded = false }
        .sheet(isPresented: $model.askSheetShown) {
            AskMeetingsView(model: model)
        }
        .background {
            // Global ⌘K to open Ask, without stealing a visible toolbar slot.
            Button("") { model.askSheetShown = true }
                .keyboardShortcut("k", modifiers: .command)
                .hidden()
        }
    }

    // MARK: - Sidebar

    private var sidebar: some View {
        List {
            Section {
                Picker("Upcoming", selection: $model.prepRange) {
                    ForEach(PrepRange.allCases) { range in
                        Text(range.label).tag(range)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .onChange(of: model.prepRange) {
                    model.switchPrepRange()
                }
            }

            let groups = model.prepGroups
            if groups.isEmpty {
                Section(model.prepRange.label) {
                    // Distinguish "still working" from "genuinely nothing" — an
                    // empty pane during a 15s gather reads as a broken app.
                    if !model.prepHasLoadedOnce && model.prepIsLoading {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Gathering prep…")
                        }
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    } else {
                        Text("No meetings in this range")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            } else {
                ForEach(groups, id: \.label) { group in
                    Section(group.label) {
                        ForEach(group.items) { item in
                            Button {
                                model.selectPrep(item.id)
                            } label: {
                                MeetingPrepRow(item: item, isSelected: model.selectedPrepID == item.id)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }

            let skipped = model.prepItems.filter { $0.prepState == "Skipped" }
            if !skipped.isEmpty {
                Section("Skipped") {
                    ForEach(skipped) { item in
                        Button {
                            model.selectPrep(item.id)
                        } label: {
                            MeetingPrepRow(item: item, isSelected: model.selectedPrepID == item.id)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }

            Section("Recordings") {
                if model.store.items.isEmpty {
                    Text("No recordings yet")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    recordingsSearchField
                    let q = recordingsSearch.trimmingCharacters(in: .whitespaces)
                    let recs = q.isEmpty
                        ? model.store.items
                        : model.store.items.filter { $0.matches(q) }
                    if recs.isEmpty {
                        Text("No recordings match “\(q)”")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(recs) { item in
                            Button {
                                model.selectRecording(item.id)
                            } label: {
                                RecordingRow(item: item, isSelected: model.selectedID == item.id)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .listStyle(.sidebar)
    }

    private var recordingsSearchField: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField("Search recordings", text: $recordingsSearch)
                .textFieldStyle(.plain)
                .font(.callout)
            if !recordingsSearch.isEmpty {
                Button {
                    recordingsSearch = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Clear search")
            }
        }
        .padding(.vertical, 4)
        .padding(.horizontal, 8)
        .background(Color.gray.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 16) {
            Button(action: model.toggle) {
                HStack(spacing: 8) {
                    Image(systemName: model.isRecording ? "stop.circle.fill" : "record.circle")
                        .font(.system(size: 22))
                    Text(model.isRecording ? "Stop" : "Record")
                        .font(.system(size: 15, weight: .semibold))
                }
                .frame(minWidth: 120)
                .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .tint(model.isRecording ? .red : .accentColor)
            .disabled(model.isBusy && !model.isRecording)

            Button { model.importGeminiNotes() } label: {
                Image(systemName: "sparkles")
                    .font(.system(size: 18))
            }
            .buttonStyle(.bordered)
            .disabled(model.isBusy)
            .help("Import Gemini Notes from email")

            Button {
                model.askSheetShown = true
            } label: {
                Image(systemName: "magnifyingglass.circle")
                    .font(.system(size: 18))
            }
            .buttonStyle(.bordered)
            .help("Ask across all meetings (⌘K)")

            VStack(alignment: .leading, spacing: 2) {
                Text(model.stage.label)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(stageColor)
                if !model.activityDetail.isEmpty {
                    Text(model.activityDetail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                if model.isRecording {
                    Text(formatElapsed(model.elapsed))
                        .font(.system(.callout, design: .monospaced))
                        .foregroundStyle(.secondary)
                } else if let progress = model.transcribeProgress, model.stage == .transcribing {
                    ProgressView(value: progress, total: 1.0)
                        .frame(width: 140)
                } else if model.stage == .transcribing, let last = model.transcribeHeartbeatAt {
                    Text("Last update \(relativeTime(from: last)) ago")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if let prep = model.selectedPrep {
                    Text(prepHeaderDetail(prep))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                } else if let rec = model.selectedRecording {
                    Text(rec.id)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
            Spacer()
            Button { model.refreshMeetingPrep() } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 16))
            }
            .buttonStyle(.borderless)
            .disabled(model.isBusy)
            .help("Refresh meeting prep")
            if let rec = model.selectedRecording, let url = rec.systemURL ?? rec.mixedURL {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([url])
                } label: {
                    Image(systemName: "folder")
                }
                .buttonStyle(.borderless)
                .help("Reveal in Finder")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    // MARK: - Main panes

    private var primaryPane: some View {
        if let prep = model.selectedPrep {
            AnyView(prepPane(prep))
        } else {
            AnyView(transcriptPane)
        }
    }

    private var secondaryPane: some View {
        if let prep = model.selectedPrep {
            AnyView(prepContextPane(prep))
        } else {
            AnyView(recordingContextPane)
        }
    }

    // MARK: - Prep pane

    private func prepPane(_ prep: MeetingPrepItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(prep.title).font(.headline).lineLimit(2)
                    Spacer()
                    Text(prep.timeRange).font(.caption).foregroundStyle(.secondary)
                }
                HStack(spacing: 8) {
                    Label(prep.prepState, systemImage: "doc.text.magnifyingglass")
                    if let sourceState = prep.sourceState, sourceState != prep.prepState {
                        Label(sourceState, systemImage: "tray.full")
                    }
                    Label("\(prep.relatedTranscripts.count)", systemImage: "waveform")
                    Label("\(prep.relatedEmails.count)", systemImage: "envelope")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    if let location = prep.location, !location.isEmpty {
                        Label(location, systemImage: "mappin.and.ellipse")
                    }
                    if let link = prep.calendarLink, let url = webURL(link) {
                        Link(destination: url) {
                            Label("Open in Google Calendar", systemImage: "calendar")
                        }
                    }
                    if let briefPath = prep.briefPath, !briefPath.isEmpty {
                        Button {
                            NSWorkspace.shared.open(URL(fileURLWithPath: briefPath))
                        } label: {
                            Label("Open source brief", systemImage: "doc")
                        }
                        .buttonStyle(.link)
                    }
                    Button {
                        copyPrep(prep)
                    } label: {
                        Label("Copy prep", systemImage: "doc.on.doc")
                    }
                    .buttonStyle(.link)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let reason = prep.skipReason, !reason.isEmpty {
                        PrepSection(title: "Skip Reason", icon: "minus.circle") {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(reason)
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                                Button {
                                    model.alwaysPrep(prep)
                                } label: {
                                    Label("Always prep this series", systemImage: "plus.circle")
                                }
                                .buttonStyle(.bordered)
                                .disabled(model.isBusy)
                            }
                        }
                    }
                    if let attendees = prep.attendees, !attendees.isEmpty {
                        PrepSection(title: "Attendees", icon: "person.2") {
                            PrepAttendeeList(attendees: attendees)
                        }
                    }
                    if !prep.why.isEmpty && prep.prepState != "Skipped" {
                        PrepSection(title: "Why This Matters", icon: "target") {
                            Text(prep.why)
                                .font(.callout)
                                .textSelection(.enabled)
                        }
                    }
                    if !prep.leftOff.isEmpty {
                        PrepSection(title: prep.hasAuthoredNarrative ? "Background" : "Where We Left Off", icon: "arrow.uturn.left") {
                            BulletList(items: prep.leftOff)
                        }
                    }
                    if !prep.actions.isEmpty {
                        PrepSection(title: prep.hasAuthoredNarrative ? "Suggested Ask" : "Carryover Actions", icon: "checklist") {
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(prep.actions) { action in
                                    HStack(alignment: .top, spacing: 8) {
                                        Image(systemName: "circle")
                                            .font(.system(size: 13))
                                            .foregroundStyle(.secondary)
                                            .padding(.top, 2)
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(action.text).font(.callout)
                                            HStack(spacing: 8) {
                                                Label(action.owner, systemImage: "person")
                                                Label(action.source, systemImage: "doc.text")
                                            }
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    if !prep.talkingPoints.isEmpty {
                        PrepSection(title: "Talking Points", icon: "text.bubble") {
                            BulletList(items: prep.talkingPoints)
                        }
                    }
                    if !prep.openQuestions.isEmpty {
                        PrepSection(title: "Open Questions", icon: "questionmark.circle") {
                            BulletList(items: prep.openQuestions)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func prepContextPane(_ prep: MeetingPrepItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Related").font(.headline)
                Spacer()
                Picker("", selection: $relatedFilter) {
                    Text("All").tag("All")
                    Text("Transcripts").tag("Transcripts")
                    Text("Email").tag("Email")
                    Text("Context").tag("Context")
                }
                .pickerStyle(.segmented)
                .frame(width: 260)
                Button {
                    model.importGeminiNotes()
                } label: {
                    Image(systemName: "sparkles")
                }
                .buttonStyle(.borderless)
                .disabled(model.isBusy)
                .help("Import Gemini Notes from email")

            Button { model.refreshMeetingPrep() } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 18))
            }
            .buttonStyle(.bordered)
            .disabled(model.isBusy)
            .help("Refresh meeting prep")
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if relatedFilter == "All" || relatedFilter == "Transcripts" {
                        RelatedGroup(title: "Transcripts", icon: "waveform", items: prep.relatedTranscripts)
                    }
                    if relatedFilter == "All" || relatedFilter == "Email" {
                        RelatedGroup(title: "Email Threads", icon: "envelope", items: prep.relatedEmails)
                    }
                    if relatedFilter == "All" || relatedFilter == "Context" {
                        RelatedGroup(title: "Context", icon: "person.2", items: prep.context + prep.prepSources)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    // MARK: - Transcript pane

    private var transcriptPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.isRecording {
                Text("Recording in progress…")
                    .font(.headline)
                    .foregroundStyle(.red)
                Text("Transcript will appear after you press Stop.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
            } else if let rec = model.selectedRecording {
                if let event = rec.event {
                    MeetingHeaderView(event: event, fallbackDate: rec.displayDate)
                        .padding(.bottom, 4)
                } else {
                    HStack {
                        Text("Transcript").font(.headline)
                        Spacer()
                        Text(rec.displayDate).font(.caption).foregroundStyle(.secondary)
                    }
                }
                if rec.transcriptURL == nil, rec.systemURL != nil, rec.micURL != nil {
                    HStack(spacing: 10) {
                        Text("Transcription has not completed for this recording.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        Button("Resume transcription") {
                            model.resumeProcessing(rec)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.isBusy)
                    }
                    .padding(.bottom, 4)
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if !rec.summary.isEmpty {
                            Text(rec.summary)
                                .font(.body)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        decisionsSection(for: rec)
                        promisedSection(for: rec)
                        transcriptDisclosure(for: rec)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: .infinity)
            } else {
                Text("Select a recording")
                    .font(.headline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    /// What the group settled, as distinct from what anyone owes.
    ///
    /// Kept above commitments because it answers the question people actually
    /// come back to a meeting for — "what did we land on?" — and because a
    /// decision is what makes the todos beneath it make sense. Silent when the
    /// recording predates decision extraction, which is indistinguishable from
    /// a meeting that decided nothing.
    @ViewBuilder
    private func decisionsSection(for rec: RecordingItem) -> some View {
        if !rec.decisions.isEmpty {
            PrepSection(title: "Decisions", icon: "checkmark.seal") {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(rec.decisions) { decision in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(decision.text)
                                .font(.callout)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            if let context = decision.context, !context.isEmpty {
                                Text(context)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
    }

    /// Top 3-5 commitments from THIS meeting (mine/waiting first). Display only —
    /// no tracking. Ask for a cross-meeting rollup instead of checking these off.
    @ViewBuilder
    private func promisedSection(for rec: RecordingItem) -> some View {
        let ranked = rec.todos.sorted { a, b in
            func rank(_ t: Todo) -> Int {
                switch t.bucketKind { case .mine: return 0; case .waiting_on: return 1; case .fyi: return 2 }
            }
            return rank(a) < rank(b)
        }
        let top = Array(ranked.prefix(5))
        if !top.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Promised")
                    .font(.subheadline.weight(.semibold))
                ForEach(Array(top.enumerated()), id: \.offset) { _, todo in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: "circle.fill")
                            .font(.system(size: 5))
                            .foregroundStyle(.secondary)
                            .padding(.top, 5)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(todo.text).font(.callout)
                            let meta = [todo.owner, todo.due].compactMap { $0 }.filter { !$0.isEmpty }
                            if !meta.isEmpty {
                                Text(meta.joined(separator: " · "))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func transcriptDisclosure(for rec: RecordingItem) -> some View {
        DisclosureGroup(isExpanded: $transcriptExpanded) {
            Text(rec.transcript.isEmpty ? "(no speech detected)" : rec.transcript)
                .font(.system(.body))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 6)
        } label: {
            Label("Full transcript", systemImage: "text.alignleft")
                .font(.subheadline.weight(.semibold))
        }
    }

    // MARK: - Recording context pane (related meetings)

    private var recordingContextPane: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Related")
                .font(.headline)
            if let rec = model.selectedRecording {
                let related = model.store.relatedRecordings(to: rec)
                if related.isEmpty {
                    Text("No related meetings yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Spacer()
                } else {
                    Text("Meetings with these people")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(related) { item in
                                Button {
                                    model.selectRecording(item.id)
                                } label: {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(item.headline)
                                            .font(.callout)
                                            .foregroundStyle(.primary)
                                            .lineLimit(2)
                                            .multilineTextAlignment(.leading)
                                        Text(item.displayDate)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(8)
                                    .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    // Slice 2 will add a "Related emails" group here, wired through
                    // the email-vector socket boundary (no direct import).
                }
            } else {
                Spacer()
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    // MARK: - Todos pane (aggregated across all recordings)

    private static let bucketFilters = ["All", "Mine", "Waiting", "FYI"]

    private static func bucketKind(forFilter filter: String) -> Todo.Bucket? {
        switch filter {
        case "Mine": return .mine
        case "Waiting": return .waiting_on
        case "FYI": return .fyi
        default: return nil // "All"
        }
    }

    private var todosPane: some View {
        let all = model.store.aggregatedTodos
        let (allActive, allDone) = split(all)
        // Soft bucket filter; sort open items by resolved due date (soonest first).
        let kind = Self.bucketKind(forFilter: bucketFilter)
        let active = allActive
            .filter { kind == nil || $0.todo.bucketKind == kind }
            .sorted { a, b in
                // Items a later meeting flagged "resolved?" float to the top so
                // the user acts on them; then soonest due date; then recency.
                let pa = model.completion.resolvedReason(a.completionKey) != nil
                let pb = model.completion.resolvedReason(b.completionKey) != nil
                if pa != pb { return pa }
                let da = a.todo.dueISO ?? "9999-99-99"
                let db = b.todo.dueISO ?? "9999-99-99"
                return da == db ? a.sourceDate > b.sourceDate : da < db
            }
        let done = allDone.filter { kind == nil || $0.todo.bucketKind == kind }

        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Todos").font(.headline)
                Text("\(active.count) open")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(Color.gray.opacity(0.15), in: Capsule())
                if !done.isEmpty {
                    Text("\(done.count) done")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Color.green.opacity(0.15), in: Capsule())
                }
                Spacer()
            }
            Picker("Bucket", selection: $bucketFilter) {
                ForEach(Self.bucketFilters, id: \.self) { f in
                    Text(bucketFilterLabel(f, active: allActive)).tag(f)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            if all.isEmpty {
                Text("Todos will accumulate across recordings.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(active) { agg in row(for: agg) }
                        if !done.isEmpty {
                            Text("Done")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .padding(.top, 8)
                            ForEach(done) { agg in row(for: agg) }
                        }
                    }
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func bucketFilterLabel(_ f: String, active: [RecordingsStore.AggregatedTodo]) -> String {
        guard let kind = Self.bucketKind(forFilter: f) else { return "All" }
        let n = active.filter { $0.todo.bucketKind == kind }.count
        return n > 0 ? "\(f) (\(n))" : f
    }

    private func row(for agg: RecordingsStore.AggregatedTodo) -> some View {
        TodoRow(
            todo: agg.todo,
            sourceLabel: shortLabel(for: agg.sourceDate),
            isDone: model.completion.isDone(agg.completionKey),
            isFromSelected: agg.sourceId == model.selectedID,
            resolvedReason: model.completion.resolvedReason(agg.completionKey),
            onToggle: { model.completion.toggle(agg.completionKey) },
            onSelect: { model.selectedID = agg.sourceId },
            onConfirmResolved: { model.completion.confirmResolved(agg.completionKey) },
            onKeepOpen: { model.completion.dismissResolved(agg.completionKey) }
        )
    }

    private func split(_ todos: [RecordingsStore.AggregatedTodo])
    -> ([RecordingsStore.AggregatedTodo], [RecordingsStore.AggregatedTodo]) {
        var active: [RecordingsStore.AggregatedTodo] = []
        var done: [RecordingsStore.AggregatedTodo] = []
        for t in todos {
            if model.completion.isDone(t.completionKey) { done.append(t) } else { active.append(t) }
        }
        return (active, done)
    }

    // MARK: - Helpers

    private var stageColor: Color {
        switch model.stage {
        case .idle, .done: return .secondary
        case .recording: return .red
        case .saving, .transcribing, .extractingTodos, .importingGemini: return .orange
        case .failed: return .red
        }
    }

    private func formatElapsed(_ t: TimeInterval) -> String {
        let total = Int(t)
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    private func relativeTime(from date: Date) -> String {
        let delta = max(Int(Date().timeIntervalSince(date)), 0)
        if delta < 60 { return "\(delta)s" }
        return String(format: "%dm %02ds", delta / 60, delta % 60)
    }

    private func shortLabel(for date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "MMM d, h:mm a"
        return f.string(from: date)
    }

    private static func parseTranscribeProgress(_ line: String) -> (progress: Double, label: String)? {
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

    private static func isTranscribeHeartbeat(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return trimmed.hasPrefix("loading model") || trimmed.hasPrefix("transcribing")
            || trimmed.hasPrefix("mixed →") || trimmed.hasPrefix("chunk ")
    }

    private func prepHeaderDetail(_ prep: MeetingPrepItem) -> String {
        var parts = [prep.id]
        if let refreshed = model.prepLastRefreshed {
            parts.append("refreshed \(refreshed)")
        }
        if model.prepIsStale {
            parts.append("stale cache")
        }
        if let error = model.prepError {
            parts.append(error)
        }
        return parts.joined(separator: " | ")
    }

    private func copyPrep(_ prep: MeetingPrepItem) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(prep.copyText, forType: .string)
    }
}

// MARK: - Subviews
