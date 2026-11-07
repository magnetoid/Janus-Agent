import SwiftUI

struct ContentView: View {
    @Environment(AppStore.self) private var store
    @State private var columns: NavigationSplitViewVisibility = .all

    var body: some View {
        switch store.phase {
        case .ready:
            shell
        case .booting(let text):
            BootView(text: text, failure: nil)
        case .failed(let message, let log):
            BootView(text: message, failure: log)
        }
    }

    private var shell: some View {
        NavigationSplitView(columnVisibility: $columns) {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 340)
        } detail: {
            Group {
                switch store.selection {
                case .skills?: SkillsView()
                case .cron?: CronView()
                default: ChatView()
                }
            }
            .background(Color(nsColor: .textBackgroundColor))
        }
        .toolbar {
            ToolbarItem(placement: .navigation) {
                if columns == .detailOnly {
                    Button { store.newChat() } label: { Image(systemName: "square.and.pencil") }
                        .help("New chat")
                }
            }
            ToolbarItem(placement: .principal) {
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
            }
            ToolbarItemGroup(placement: .primaryAction) {
                if case .session = store.selection, let pct = store.usage.contextPercent {
                    ContextMeter(percent: pct)
                }
                ModelMenu()
            }
        }
        .toolbarBackground(.hidden, for: .windowToolbar)
    }

    private var title: String {
        switch store.selection {
        case .skills?: return "Skills"
        case .cron?: return "Scheduled"
        case .session(let id)?: return store.sessions.first { $0.id == id }?.displayTitle ?? ""
        default: return ""
        }
    }
}

struct ContextMeter: View {
    let percent: Double

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .trim(from: 0, to: min(max(percent / 100, 0), 1))
                .stroke(percent > 80 ? Color.orange : Color.secondary, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .background(Circle().stroke(Color.primary.opacity(0.1), lineWidth: 2))
                .frame(width: 12, height: 12)
            Text("\(Int(percent.rounded()))%")
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .help("Context window used")
    }
}

struct ModelMenu: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        Menu {
            if store.catalog.providers.isEmpty {
                Text("No models available")
            }
            ForEach(store.catalog.providers, id: \.slug) { provider in
                Section(provider.name) {
                    ForEach(provider.models, id: \.self) { m in
                        Button {
                            Task { await store.switchModel(provider: provider.slug, model: m) }
                        } label: {
                            if m == store.modelName && provider.slug == store.catalog.currentProvider {
                                Label(m, systemImage: "checkmark")
                            } else {
                                Text(m)
                            }
                        }
                    }
                }
            }
        } label: {
            Text(store.modelName.isEmpty ? "Model" : store.modelName)
                .font(.system(size: 12))
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .foregroundStyle(.secondary)
        .disabled(store.isRunning)
        .onAppear { Task { await store.refreshModels() } }
    }
}

struct BootView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openSettings) private var openSettings
    let text: String
    let failure: String?

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            BrandMark(size: 44)
            if failure == nil {
                ProgressView().controlSize(.small)
            }
            Text(text)
                .font(.system(size: 14))
                .foregroundStyle(failure == nil ? .secondary : .primary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
                .textSelection(.enabled)
            if let log = failure {
                if !log.isEmpty {
                    ScrollView {
                        Text(log)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                    }
                    .frame(maxWidth: 640, maxHeight: 220)
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.primary.opacity(0.1), lineWidth: 0.5))
                }
                HStack(spacing: 10) {
                    Button("Try Again") { Task { await store.boot() } }
                        .buttonStyle(ChipStyle(prominent: true))
                    Button("Settings…") { openSettings() }
                        .buttonStyle(ChipStyle())
                }
            }
            Spacer()
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .textBackgroundColor))
    }
}
