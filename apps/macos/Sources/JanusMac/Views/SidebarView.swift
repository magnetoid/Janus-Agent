import SwiftUI

struct SidebarView: View {
    @Environment(AppStore.self) private var store
    @State private var renaming: SessionSummary?
    @State private var renameText = ""

    var body: some View {
        @Bindable var store = store
        List(selection: $store.selection) {
            Button { store.newChat() } label: {
                HStack {
                    Label("New chat", systemImage: "square.and.pencil")
                    Spacer()
                    Text("⌘N").font(.system(size: 11)).foregroundStyle(.tertiary)
                }
            }
            .buttonStyle(.plain)
            .padding(.vertical, 2)

            Label("Skills", systemImage: "puzzlepiece.extension").tag(SidebarItem.skills)
            Label("Scheduled", systemImage: "clock").tag(SidebarItem.cron)

            ForEach(groups, id: \.0) { label, items in
                Section(label) {
                    ForEach(items) { s in
                        SessionRow(session: s)
                            .tag(SidebarItem.session(s.id))
                            .contextMenu {
                                Button("Rename…") {
                                    renameText = s.title
                                    renaming = s
                                }
                                Button("Copy Session ID") { Pasteboard.copy(s.id) }
                                Divider()
                                Button("Delete", role: .destructive) { Task { await store.delete(s.id) } }
                            }
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .searchable(text: $store.search, placement: .sidebar, prompt: "Search")
        .onChange(of: store.selection) { _, new in Task { await store.select(new) } }
        .refreshable { await store.refreshSessions() }
        .safeAreaInset(edge: .bottom) { footer }
        .alert("Rename chat", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Title", text: $renameText)
            Button("Rename") {
                if let s = renaming { Task { await store.rename(s.id, to: renameText) } }
                renaming = nil
            }
            Button("Cancel", role: .cancel) { renaming = nil }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            BrandMark(size: 24)
            VStack(alignment: .leading, spacing: 1) {
                Text(NSFullUserName()).font(.system(size: 13))
                if !store.settings.profile.isEmpty {
                    Text(store.settings.profile).font(.system(size: 11)).foregroundStyle(.tertiary)
                }
            }
            Spacer()
            SettingsLink { Image(systemName: "gearshape") }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .overlay(alignment: .top) { Divider() }
    }

    private var groups: [(String, [SessionSummary])] {
        let cal = Calendar.current
        let weekAgo = cal.date(byAdding: .day, value: -7, to: Date()) ?? Date()
        var today: [SessionSummary] = [], yesterday: [SessionSummary] = [],
            week: [SessionSummary] = [], older: [SessionSummary] = []
        for s in store.filteredSessions {
            if cal.isDateInToday(s.lastActive) { today.append(s) }
            else if cal.isDateInYesterday(s.lastActive) { yesterday.append(s) }
            else if s.lastActive > weekAgo { week.append(s) }
            else { older.append(s) }
        }
        return [("Today", today), ("Yesterday", yesterday), ("Previous 7 days", week), ("Earlier", older)]
            .filter { !$0.1.isEmpty }
    }
}

private struct SessionRow: View {
    let session: SessionSummary

    var body: some View {
        HStack(spacing: 6) {
            Text(session.displayTitle).lineLimit(1)
            Spacer(minLength: 0)
            if !session.source.isEmpty, !["desktop", "tui", "cli", "dashboard"].contains(session.source) {
                Text(session.source)
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
        }
    }
}
