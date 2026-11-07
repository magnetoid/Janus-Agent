import SwiftUI

struct ChatView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        VStack(spacing: 0) {
            if !store.providerConfigured {
                ProviderBanner()
            }
            if store.transcript.isEmpty && !store.isLoadingThread {
                EmptyStateView()
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 22) {
                            ForEach(store.transcript) { item in
                                ChatItemView(item: item, isLast: item.id == store.transcript.last?.id).id(item.id)
                            }
                            if store.isRunning, !lastIsStreamingText {
                                TypingDots().id("typing")
                            }
                        }
                        .frame(maxWidth: 720)
                        .padding(.horizontal, 32)
                        .padding(.top, 20)
                        .padding(.bottom, 40)
                        .frame(maxWidth: .infinity)
                    }
                    .onChange(of: store.transcript) {
                        withAnimation(.easeOut(duration: 0.15)) {
                            proxy.scrollTo(store.isRunning ? AnyHashable("typing") : AnyHashable(store.transcript.last?.id), anchor: .bottom)
                        }
                    }
                }
            }
            ComposerView()
        }
    }

    private var lastIsStreamingText: Bool {
        guard store.isRunning, let last = store.transcript.last, case .assistant(let t, _) = last.kind else { return false }
        return !t.isEmpty
    }
}

struct ChatItemView: View {
    @Environment(AppStore.self) private var store
    let item: ChatItem
    var isLast = false

    var body: some View {
        switch item.kind {
        case .user(let text):
            HStack {
                Spacer(minLength: 120)
                Text(text)
                    .font(.system(size: 14))
                    .lineSpacing(3)
                    .textSelection(.enabled)
                    .padding(.horizontal, 15)
                    .padding(.vertical, 9)
                    .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color.primary.opacity(0.055)))
            }
        case .assistant(let text, let reasoning):
            AssistantView(text: text, reasoning: reasoning, isLive: isLast && store.isRunning && text.isEmpty)
        case .tool(let tool):
            ToolRow(tool: tool)
        case .approval(let a):
            ApprovalCard(itemID: item.id, request: a)
        case .clarify(let c):
            ClarifyCard(itemID: item.id, request: c)
        case .notice(let text, let isError):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: isError ? "exclamationmark.triangle" : "info.circle")
                Text(text).textSelection(.enabled)
            }
            .font(.system(size: 12.5))
            .foregroundStyle(isError ? Color.orange : Color.secondary)
        }
    }
}

struct AssistantView: View {
    let text: String
    let reasoning: String
    var isLive = false
    @State private var showReasoning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !reasoning.isEmpty {
                Button {
                    withAnimation(.easeOut(duration: 0.15)) { showReasoning.toggle() }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.right")
                            .rotationEffect(.degrees(showReasoning ? 90 : 0))
                        Text(isLive ? "Thinking…" : "Thought process")
                    }
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                if showReasoning {
                    Text(reasoning)
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                        .lineSpacing(4)
                        .textSelection(.enabled)
                        .padding(.leading, 12)
                        .overlay(alignment: .leading) {
                            Rectangle().fill(Color.primary.opacity(0.1)).frame(width: 1)
                        }
                }
            }
            if !text.isEmpty {
                MarkdownText(content: text)
                HStack(spacing: 2) {
                    IconButton(symbol: "doc.on.doc", help: "Copy") { Pasteboard.copy(text) }
                }
                .padding(.leading, -6)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ToolRow: View {
    let tool: ToolActivity
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                if !tool.result.isEmpty { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } }
            } label: {
                HStack(spacing: 8) {
                    icon.frame(width: 14)
                    Text(tool.name)
                        .font(.system(size: 12.5, weight: .medium))
                    Text(tool.summary.isEmpty ? tool.context : tool.summary)
                        .font(.system(size: 12.5, design: tool.summary.isEmpty ? .monospaced : .default))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 0)
                    if let d = tool.duration {
                        Text(d < 1 ? "<1s" : "\(Int(d.rounded()))s")
                            .font(.system(size: 11).monospacedDigit())
                            .foregroundStyle(.tertiary)
                    }
                    if !tool.result.isEmpty {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                ScrollView {
                    Text(tool.result)
                        .font(.system(size: 11.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .frame(maxHeight: 240)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.03)))
            }
        }
    }

    @ViewBuilder private var icon: some View {
        switch tool.state {
        case .running: ProgressView().controlSize(.mini)
        case .done: Image(systemName: symbol).font(.system(size: 11)).foregroundStyle(.secondary)
        case .failed: Image(systemName: "xmark.circle").font(.system(size: 11)).foregroundStyle(.orange)
        }
    }

    private var symbol: String {
        switch tool.name {
        case "terminal", "process", "execute_code": "terminal"
        case "read_file", "search_files": "doc.text.magnifyingglass"
        case "write_file", "patch": "pencil"
        case let n where n.hasPrefix("web"): "globe"
        case let n where n.hasPrefix("browser"): "safari"
        case "memory": "brain"
        case "todo": "checklist"
        case "subagent", "delegate_task": "person.2"
        case let n where n.hasPrefix("skill"): "puzzlepiece.extension"
        default: "wrench.and.screwdriver"
        }
    }
}

struct ApprovalCard: View {
    @Environment(AppStore.self) private var store
    let itemID: ChatItem.ID
    let request: ApprovalRequest

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(request.resolved == nil ? "Janus wants to run a command" : resolvedText,
                  systemImage: request.resolved == nil ? "hand.raised" : "checkmark.shield")
                .font(.system(size: 13, weight: .medium))
            if !request.description.isEmpty {
                Text(request.description).font(.system(size: 12.5)).foregroundStyle(.secondary)
            }
            Text(request.command)
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.04)))
            if request.resolved == nil {
                HStack(spacing: 8) {
                    Button("Allow once") { respond("once") }.buttonStyle(ChipStyle(prominent: true))
                    Button("Allow for this chat") { respond("session") }.buttonStyle(ChipStyle())
                    Button("Always allow") { respond("always") }.buttonStyle(ChipStyle())
                    Spacer()
                    Button("Deny") { respond("deny") }.buttonStyle(ChipStyle())
                }
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color(nsColor: .textBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5))
        .shadow(color: .black.opacity(request.resolved == nil ? 0.06 : 0), radius: 6, y: 2)
    }

    private var resolvedText: String {
        request.resolved == "deny" ? "Denied" : "Approved (\(request.resolved ?? ""))"
    }

    private func respond(_ choice: String) {
        Task { await store.respondApproval(itemID: itemID, choice: choice) }
    }
}

struct ClarifyCard: View {
    @Environment(AppStore.self) private var store
    let itemID: ChatItem.ID
    let request: ClarifyRequest
    @State private var answer = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Janus has a question", systemImage: "questionmark.bubble")
                .font(.system(size: 13, weight: .medium))
            Text(request.question).font(.system(size: 14)).textSelection(.enabled)
            if let a = request.answer {
                Text(a).font(.system(size: 13)).foregroundStyle(.secondary)
            } else {
                if !request.choices.isEmpty {
                    FlowChoices(choices: request.choices) { send($0) }
                }
                HStack {
                    TextField("Answer", text: $answer)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { send(answer) }
                    Button("Send") { send(answer) }
                        .buttonStyle(ChipStyle(prominent: true))
                        .disabled(answer.nonEmpty == nil)
                }
            }
        }
        .padding(14)
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5))
    }

    private func send(_ s: String) {
        guard let s = s.nonEmpty else { return }
        Task { await store.respondClarify(itemID: itemID, answer: s) }
    }
}

private struct FlowChoices: View {
    let choices: [String]
    let pick: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(choices, id: \.self) { c in
                Button(c) { pick(c) }.buttonStyle(ChipStyle())
            }
        }
    }
}

struct EmptyStateView: View {
    @Environment(AppStore.self) private var store
    private let suggestions = [
        "What can you do on this machine?",
        "Summarize this folder",
        "Plan my week from my notes",
        "Set up a daily briefing",
    ]

    var body: some View {
        VStack(spacing: 26) {
            Spacer()
            BrandMark(size: 40)
            Text(greeting)
                .font(.system(size: 28, weight: .medium))
                .tracking(-0.5)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 220, maximum: 280), spacing: 8)], spacing: 8) {
                ForEach(suggestions, id: \.self) { s in
                    Button(s) { Task { await store.send(s) } }
                        .buttonStyle(ChipStyle())
                }
            }
            .frame(maxWidth: 600)
            Spacer()
        }
        .padding(.bottom, 60)
        .frame(maxWidth: .infinity)
    }

    private var greeting: String {
        let name = NSFullUserName().components(separatedBy: " ").first ?? ""
        let h = Calendar.current.component(.hour, from: Date())
        let part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"
        return name.isEmpty ? part : "\(part), \(name)"
    }
}

struct ProviderBanner: View {
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "key")
            Text("No model provider is configured yet. Run `janus model` in a terminal to pick one, then restart the backend (⇧⌘R).")
                .textSelection(.enabled)
            Spacer()
        }
        .font(.system(size: 12.5))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
        .background(Color.orange.opacity(0.08))
    }
}
