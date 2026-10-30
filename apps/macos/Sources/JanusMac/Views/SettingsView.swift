import SwiftUI

struct SettingsView: View {
    @Environment(AppStore.self) private var store
    @State private var draft = BackendSettings.load()

    var body: some View {
        Form {
            Section {
                PathField(label: "Janus source folder", text: $draft.sourceRoot, directory: true,
                          prompt: "~/c/janus")
                TextField("janus command", text: $draft.command, prompt: Text("Auto-detect"))
                Text("A source folder with a .venv wins; otherwise the janus command (or the first one found on your PATH) is used.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Backend")
            }

            Section {
                TextField("Profile", text: $draft.profile, prompt: Text("default"))
                PathField(label: "Working folder", text: $draft.workingDirectory, directory: true, prompt: "~")
            } header: {
                Text("Agent")
            } footer: {
                Text("New chats run in the working folder; tools see it as their current directory.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                LabeledContent("Status", value: statusText)
                if !store.backendLabel.isEmpty { LabeledContent("Running", value: store.backendLabel) }
                if !store.version.isEmpty { LabeledContent("Version", value: store.version) }
                HStack {
                    Spacer()
                    Button("Save and Restart Backend") {
                        Task { await store.restart(with: draft) }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(draft == store.settings && store.phase == .ready)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .fixedSize()
        .onAppear { draft = store.settings }
    }

    private var statusText: String {
        switch store.phase {
        case .ready: "Connected"
        case .booting(let s): s
        case .failed(let s, _): s
        }
    }
}

private struct PathField: View {
    let label: String
    @Binding var text: String
    let directory: Bool
    let prompt: String

    var body: some View {
        HStack {
            TextField(label, text: $text, prompt: Text(prompt))
            Button("Choose…") {
                let panel = NSOpenPanel()
                panel.canChooseDirectories = directory
                panel.canChooseFiles = !directory
                panel.allowsMultipleSelection = false
                if !text.isEmpty {
                    panel.directoryURL = URL(fileURLWithPath: (text as NSString).expandingTildeInPath)
                }
                if panel.runModal() == .OK, let url = panel.url { text = url.path }
            }
        }
    }
}
