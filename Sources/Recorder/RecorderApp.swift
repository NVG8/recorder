import SwiftUI
import AppKit

@main
struct RecorderApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup("Recorder") {
            ContentView(model: model)
        }
        .windowStyle(.titleBar)
        .windowResizability(.contentMinSize)
        .onChange(of: scenePhase) { _, phase in
            // Coming back to the app is the moment prep is most likely stale.
            // Debounced inside the model so cmd-tabbing doesn't re-gather.
            guard phase == .active else { return }
            model.refreshMeetingPrepIfStale()
            // Pull in any Gemini Notes that landed since we last looked. Its own
            // debounce is much longer than prep's, so this is rarely a no-op cost.
            model.importGeminiNotesIfStale()
            // Daily deep check — the one thing that would have caught the Drive
            // token dying in March rather than five months later.
            model.refreshAuthStatus()
        }

        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            Image(systemName: model.isRecording ? "record.circle.fill" : "waveform")
        }
        .menuBarExtraStyle(.menu)
    }
}

struct MenuBarView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Recorder")
                .font(.headline)

            Text(model.stage.label)
                .font(.caption)
                .foregroundStyle(.secondary)

            if model.isRecording {
                Text(String(format: "%02d:%02d", Int(model.elapsed) / 60, Int(model.elapsed) % 60))
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            Divider()

            Button(model.isRecording ? "Stop Recording" : "Start Recording") {
                model.toggle()
            }
            .keyboardShortcut(.defaultAction)

            Button("Show Recorder") {
                NSApp.activate(ignoringOtherApps: true)
                NSApp.windows.first?.makeKeyAndOrderFront(nil)
            }

            Button("Refresh Prep") {
                model.refreshMeetingPrep()
            }
            .disabled(model.isBusy)

            if let selected = model.selectedRecording {
                Button("Reveal in Finder") {
                    if let url = selected.systemURL ?? selected.mixedURL {
                        NSWorkspace.shared.activateFileViewerSelecting([url])
                    }
                }
            }
        }
        .padding(10)
        .frame(minWidth: 220, alignment: .leading)
    }
}
