import Foundation
import OSLog

private let log = Logger(subsystem: "co.nvg8.recorder", category: "completion")

/// Tracks which todos are done, without mutating the Bedrock-written
/// `*.todos.json` files.
///
/// Keys are **content-stable**: `"<recording-stem>:t<djb2(todo.text)>"`, built by
/// `RecordingsStore.AggregatedTodo.completionKey`. They deliberately do not
/// encode the todo's position, so re-running extraction over an existing
/// recording — to pick up a prompt improvement, say — cannot remap a completed
/// item onto a different task. The worst case is that a *reworded* todo gets a
/// new key and reverts to unchecked, which loses a tick rather than lying about
/// one.
///
/// An earlier scheme keyed by array index; `migrateLegacyKeys` converts those
/// and runs idempotently from `AppModel.refreshStore()`.
@MainActor
final class CompletionStore: ObservableObject {
    @Published private(set) var completed: Set<String> = []
    /// Prior-series todos a later meeting *suggested* are done (key → reason).
    /// These are NOT auto-closed — the user confirms or keeps them open, so we
    /// never silently hide a real action item on a noisy model signal.
    @Published private(set) var pendingResolved: [String: String] = [:]

    private let url = AppPaths.supportDir.appendingPathComponent("completed-todos.json")
    private let pendingURL = AppPaths.supportDir.appendingPathComponent("pending-resolved.json")

    init() {
        load()
    }

    func isDone(_ key: String) -> Bool { completed.contains(key) }
    func resolvedReason(_ key: String) -> String? { pendingResolved[key] }

    func toggle(_ key: String) {
        if completed.contains(key) {
            completed.remove(key)
        } else {
            completed.insert(key)
        }
        save()
    }

    /// Record that a later meeting *suggested* these prior items are done. They
    /// surface as a "Resolved?" prompt; the user confirms or keeps them open.
    /// Skips items already done or already pending.
    func suggestResolved(_ items: [(key: String, reason: String)]) {
        var changed = false
        for item in items where !completed.contains(item.key) && pendingResolved[item.key] == nil {
            pendingResolved[item.key] = item.reason
            changed = true
        }
        if changed { savePending() }
    }

    /// User accepted a suggestion: mark done and clear the prompt.
    func confirmResolved(_ key: String) {
        pendingResolved[key] = nil
        completed.insert(key)
        save()
        savePending()
    }

    /// User rejected a suggestion: clear the prompt, leave the item open.
    func dismissResolved(_ key: String) {
        guard pendingResolved[key] != nil else { return }
        pendingResolved[key] = nil
        savePending()
    }

    /// One-time remap of legacy "<stem>:<index>" keys to content-stable keys.
    /// Idempotent: once migrated the legacy key is gone and won't match again.
    func migrateLegacyKeys(_ pairs: [(legacy: String, stable: String)]) {
        var changed = false
        for (legacy, stable) in pairs where completed.contains(legacy) {
            completed.remove(legacy)
            completed.insert(stable)
            changed = true
        }
        if changed { save() }
    }

    private func load() {
        if let data = try? Data(contentsOf: url),
           let list = try? JSONDecoder().decode([String].self, from: data) {
            completed = Set(list)
        }
        if let data = try? Data(contentsOf: pendingURL),
           let map = try? JSONDecoder().decode([String: String].self, from: data) {
            pendingResolved = map
        }
        log.info("loaded \(self.completed.count) completed, \(self.pendingResolved.count) pending")
    }

    private func save() {
        do {
            let data = try JSONEncoder().encode(Array(completed).sorted())
            try data.write(to: url, options: .atomic)
        } catch {
            log.error("save failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func savePending() {
        do {
            let data = try JSONEncoder().encode(pendingResolved)
            try data.write(to: pendingURL, options: .atomic)
        } catch {
            log.error("pending save failed: \(error.localizedDescription, privacy: .public)")
        }
    }
}
