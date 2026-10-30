import SwiftUI

struct ComposerView: View {
    @Environment(AppStore.self) private var store
    @State private var draft = ""
    @FocusState private var focused: Bool

    private var canSend: Bool { draft.nonEmpty != nil && store.phase == .ready }

    var body: some View {
        VStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 8) {
                TextField(store.isRunning ? "Janus is working…" : "Message Janus", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14))
                    .lineLimit(1...10)
                    .focused($focused)
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) { return .ignored }
                        submit()
                        return .handled
                    }

                HStack(spacing: 4) {
                    Image(systemName: "folder")
                        .font(.system(size: 11))
                        .foregroundStyle(.tertiary)
                    Text(folderLabel)
                        .font(.system(size: 11))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.head)
                        .help(store.settings.workingDirectory)
                    Spacer()
                    Button(action: store.isRunning ? stop : submit) {
                        ZStack {
                            Circle().fill(store.isRunning || canSend ? Color.primary : Color.primary.opacity(0.18))
                            if store.isRunning {
                                RoundedRectangle(cornerRadius: 2).frame(width: 9, height: 9)
                                    .foregroundStyle(.background)
                            } else {
                                Image(systemName: "arrow.up")
                                    .font(.system(size: 13, weight: .bold))
                                    .foregroundStyle(.background)
                            }
                        }
                        .frame(width: 30, height: 30)
                        .animation(.easeOut(duration: 0.15), value: canSend)
                    }
                    .buttonStyle(.plain)
                    .help(store.isRunning ? "Stop (⌘.)" : "Send")
                }
            }
            .padding(.leading, 16)
            .padding([.trailing, .top], 12)
            .padding(.bottom, 10)
            .background(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .fill(Color(nsColor: .textBackgroundColor))
                    .shadow(color: .black.opacity(0.05), radius: 5, y: 2)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5)
            )

            Text(footer)
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: 720)
        .padding(.horizontal, 32)
        .padding(.bottom, 22)
        .frame(maxWidth: .infinity)
        .onAppear { focused = true }
        .onChange(of: store.selection) { focused = true }
    }

    private var folderLabel: String {
        let p = store.settings.workingDirectory
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return p.hasPrefix(home) ? "~" + p.dropFirst(home.count) : p
    }

    private var footer: String {
        var parts = ["Janus can run commands on this Mac; risky ones ask first."]
        if let cost = store.usage.costUSD, cost > 0 { parts.append(String(format: "$%.3f this chat", cost)) }
        return parts.joined(separator: "  ·  ")
    }

    private func submit() {
        guard canSend, !store.isRunning else { return }
        let text = draft
        draft = ""
        Task { await store.send(text) }
    }

    private func stop() {
        Task { await store.stop() }
    }
}
