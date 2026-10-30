import AppKit
import SwiftUI

enum SidebarItem: Hashable {
    case newChat
    case session(String)
    case skills
    case cron
}

@Observable
@MainActor
final class AppStore {
    enum Phase: Equatable {
        case booting(String)
        case failed(String, log: String)
        case ready
    }

    // Connection
    var phase: Phase = .booting("Starting Janus…")
    var settings = BackendSettings.load()
    var backendLabel = ""
    var version = ""
    var providerConfigured = true

    // Sidebar
    var sessions: [SessionSummary] = []
    var selection: SidebarItem? = .newChat
    var search = ""

    // Current conversation
    var transcript: [ChatItem] = []
    var storedID: String?
    var isRunning = false
    var isLoadingThread = false
    var usage = Usage()

    // Models
    var catalog = ModelCatalog()
    var modelName = ""

    // Library views
    var skills: [SkillInfo] = []
    var cronJobs: [CronJob] = []

    private let backend = BackendProcess()
    private let socket = GatewaySocket()
    private var api: JanusAPI?
    private var runtimeSID: String?
    private var runtimeByStored: [String: String] = [:]
    private var sawTextThisTurn = false

    init() {
        socket.onEvent = { [weak self] type, sid, payload in self?.handle(type, sid, payload) }
        socket.onClose = { [weak self] in
            guard let self, self.phase == .ready else { return }
            self.phase = .failed("Lost the connection to the Janus backend.", log: self.backend.recentLog)
        }
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.shutdown() }
        }
    }

    // MARK: Lifecycle

    func boot() async {
        phase = .booting("Starting Janus…")
        socket.close()
        runtimeSID = nil
        runtimeByStored.removeAll()
        do {
            let conn = try await backend.start(settings: settings)
            backendLabel = backend.label
            api = JanusAPI(connection: conn)
            phase = .booting("Connecting…")
            try await socket.connect(conn)
            let status = try? await api?.status()
            version = status?.str("version") ?? ""
            let setup = try? await socket.call("setup.status", [:])
            providerConfigured = setup?.bool("provider_configured") ?? true
            phase = .ready
            await refreshSessions()
            await refreshModels()
            if case .session(let id) = selection { await open(id) }
            // Development aids (see Snapshot): open a chat / send a probe on launch.
            // JANUS_MAC_OPEN=first|<session id>
            if let want = ProcessInfo.processInfo.environment["JANUS_MAC_OPEN"]?.nonEmpty,
               let target = want == "first" ? sessions.first?.id : want {
                selection = .session(target)
            }
            if let probe = ProcessInfo.processInfo.environment["JANUS_MAC_PROBE"]?.nonEmpty {
                await send(probe)
            }
        } catch {
            phase = .failed(error.localizedDescription, log: backend.recentLog)
        }
    }

    func restart(with new: BackendSettings) async {
        settings = new
        new.save()
        await boot()
    }

    func shutdown() {
        socket.close()
        backend.stop()
    }

    // MARK: Sidebar

    var filteredSessions: [SessionSummary] {
        let q = search.lowercased()
        guard !q.isEmpty else { return sessions }
        return sessions.filter { $0.displayTitle.lowercased().contains(q) || $0.preview.lowercased().contains(q) }
    }

    func refreshSessions() async {
        guard let api else { return }
        if let list = try? await api.sessions() { sessions = list }
    }

    func select(_ item: SidebarItem?) async {
        switch item {
        case .newChat?: newChat()
        case .session(let id)?: if id != storedID { await open(id) }
        case .skills?: await loadSkills()
        case .cron?: await loadCron()
        case nil: break
        }
    }

    func newChat() {
        storedID = nil
        runtimeSID = nil
        transcript = []
        usage = Usage()
        isRunning = false
        if selection != .newChat { selection = .newChat }
    }

    func open(_ id: String) async {
        guard let api else { return }
        storedID = id
        runtimeSID = runtimeByStored[id]
        transcript = []
        isLoadingThread = true
        defer { isLoadingThread = false }
        do {
            let rows = try await api.messages(sessionID: id)
            guard storedID == id else { return }
            transcript = TranscriptDecoder.items(from: rows)
        } catch {
            transcript = [ChatItem(kind: .notice(error.localizedDescription, isError: true))]
        }
        if runtimeSID == nil, let r = try? await socket.call("session.resume", ["session_id": id, "cols": 96]) {
            guard storedID == id, let sid = r.str("session_id") else { return }
            runtimeSID = sid
            runtimeByStored[id] = sid
            isRunning = r.bool("running")
            applyInfo(r.obj("info"))
        }
    }

    func delete(_ id: String) async {
        guard let api else { return }
        if let sid = runtimeByStored.removeValue(forKey: id) {
            _ = try? await socket.call("session.close", ["session_id": sid])
        }
        try? await api.delete(sessionID: id)
        if storedID == id { newChat() }
        await refreshSessions()
    }

    func rename(_ id: String, to title: String) async {
        guard let api, let t = title.nonEmpty else { return }
        try? await api.rename(sessionID: id, title: t)
        await refreshSessions()
    }

    // MARK: Chat

    func send(_ raw: String) async {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isRunning, phase == .ready else { return }
        transcript.append(ChatItem(kind: .user(text)))
        isRunning = true
        sawTextThisTurn = false
        do {
            let sid = try await ensureRuntimeSession()
            _ = try await socket.call("prompt.submit", ["session_id": sid, "text": text])
        } catch let e as GatewaySocket.RPCError where e.code == 4009 {
            isRunning = false
            notice("Janus is still busy with the previous message.", error: true)
        } catch {
            isRunning = false
            notice(error.localizedDescription, error: true)
        }
    }

    func stop() async {
        guard let sid = runtimeSID else { return }
        _ = try? await socket.call("session.interrupt", ["session_id": sid])
    }

    func respondApproval(itemID: ChatItem.ID, choice: String) async {
        guard let sid = runtimeSID, let i = transcript.firstIndex(where: { $0.id == itemID }),
              case .approval(var a) = transcript[i].kind else { return }
        _ = try? await socket.call("approval.respond", ["session_id": sid, "choice": choice])
        a.resolved = choice
        transcript[i].kind = .approval(a)
    }

    func respondClarify(itemID: ChatItem.ID, answer: String) async {
        guard let i = transcript.firstIndex(where: { $0.id == itemID }),
              case .clarify(var c) = transcript[i].kind else { return }
        _ = try? await socket.call("clarify.respond", ["request_id": c.requestID, "answer": answer])
        c.answer = answer
        transcript[i].kind = .clarify(c)
    }

    private func ensureRuntimeSession() async throws -> String {
        if let sid = runtimeSID { return sid }
        var params: JSONObject = ["cols": 96]
        if let cwd = settings.workingDirectory.nonEmpty { params["cwd"] = (cwd as NSString).expandingTildeInPath }
        let r = try await socket.call("session.create", params)
        guard let sid = r.str("session_id") else { throw GatewaySocket.RPCError(code: -1, message: "No session id") }
        runtimeSID = sid
        if let stored = r.str("stored_session_id") {
            storedID = stored
            runtimeByStored[stored] = sid
        }
        applyInfo(r.obj("info"))
        return sid
    }

    // MARK: Models

    func refreshModels() async {
        guard let api else { return }
        if let sid = runtimeSID, let r = try? await socket.call("model.options", ["session_id": sid]) {
            catalog = ModelCatalog(r)
        } else if let c = try? await api.modelOptions() {
            catalog = c
        }
        if catalog.current.isEmpty, let (provider, model) = try? await api.modelInfo() {
            catalog.current = model
            catalog.currentProvider = provider
        }
        if modelName.isEmpty { modelName = catalog.current }
    }

    func switchModel(provider: String, model: String) async {
        do {
            if let sid = runtimeSID {
                _ = try await socket.call("slash.exec", ["session_id": sid, "command": "/model \(model) --provider \(provider)"])
            } else {
                try await api?.setDefaultModel(provider: provider, model: model)
            }
            modelName = model
            catalog.current = model
            catalog.currentProvider = provider
        } catch {
            notice("Couldn't switch model: \(error.localizedDescription)", error: true)
        }
    }

    // MARK: Library

    func loadSkills() async {
        guard let api else { return }
        skills = (try? await api.skills()) ?? []
    }

    func toggleSkill(_ name: String, enabled: Bool) async {
        guard let api, let i = skills.firstIndex(where: { $0.name == name }) else { return }
        skills[i].enabled = enabled
        try? await api.toggleSkill(name, enabled: enabled)
    }

    func loadCron() async {
        guard let api else { return }
        cronJobs = (try? await api.cronJobs()) ?? []
    }

    // MARK: Events

    private func handle(_ type: String, _ sid: String, _ p: JSONObject) {
        if type == "session.info" {
            if sid == runtimeSID { applyInfo(p) }
            return
        }
        guard !sid.isEmpty, sid == runtimeSID else {
            if type == "message.complete" { Task { await refreshSessions() } }
            return
        }
        switch type {
        case "message.start":
            break
        case "message.delta":
            if let t = p.str("text"), !t.isEmpty { appendAssistant(text: t) }
        case "reasoning.delta":
            if let t = p.str("text"), !t.isEmpty { appendAssistant(reasoning: t) }
        case "tool.start":
            let tool = ToolActivity(toolID: p.str("tool_id") ?? UUID().uuidString, name: p.str("name") ?? "tool",
                                    context: p.str("context")?.nonEmpty ?? p.str("args_text") ?? "", state: .running)
            transcript.append(ChatItem(kind: .tool(tool)))
        case "tool.complete":
            updateTool(p.str("tool_id")) { t in
                t.state = .done
                t.summary = p.str("summary") ?? ""
                t.result = String((p.str("result_text") ?? (p["result"] as? String) ?? "").prefix(4000))
                t.duration = p.num("duration_s")
            }
        case "subagent.start":
            let tool = ToolActivity(toolID: p.str("subagent_id") ?? UUID().uuidString, name: "subagent",
                                    context: p.str("goal") ?? "", state: .running)
            transcript.append(ChatItem(kind: .tool(tool)))
        case "subagent.complete":
            updateTool(p.str("subagent_id")) { t in
                t.state = p.str("status") == "failed" ? .failed : .done
                t.summary = p.str("summary") ?? ""
                t.duration = p.num("duration_seconds")
            }
        case "approval.request":
            transcript.append(ChatItem(kind: .approval(ApprovalRequest(
                command: p.str("command") ?? "", description: p.str("description") ?? ""))))
        case "clarify.request":
            transcript.append(ChatItem(kind: .clarify(ClarifyRequest(
                requestID: p.str("request_id") ?? "", question: p.str("question") ?? "",
                choices: p["choices"] as? [String] ?? []))))
        case "message.complete":
            if !sawTextThisTurn, let t = p.str("text")?.nonEmpty { appendAssistant(text: t) }
            switch p.str("status") {
            case "interrupted": notice("Stopped.", error: false)
            case "error": if let w = p.str("warning") { notice(w, error: true) }
            default: break
            }
            usage = Usage(p.obj("usage"))
            isRunning = false
            finishRunningTools()
            Task {
                await refreshSessions()
                // A new chat's row exists once its first turn lands; select it.
                if let s = storedID, selection != .session(s) { selection = .session(s) }
            }
        case "error":
            notice(p.str("message") ?? "Something went wrong.", error: true)
            isRunning = false
            finishRunningTools()
        default:
            break
        }
    }

    private func applyInfo(_ info: JSONObject?) {
        guard let info else { return }
        if let m = info.str("model")?.nonEmpty { modelName = m }
        if let u = info.obj("usage") { usage = Usage(u) }
    }

    private func appendAssistant(text: String = "", reasoning: String = "") {
        if !text.isEmpty { sawTextThisTurn = true }
        if let last = transcript.indices.last, case .assistant(let t, let r) = transcript[last].kind {
            transcript[last].kind = .assistant(text: t + text, reasoning: r + reasoning)
        } else {
            transcript.append(ChatItem(kind: .assistant(text: text, reasoning: reasoning)))
        }
    }

    private func updateTool(_ id: String?, _ change: (inout ToolActivity) -> Void) {
        guard let i = transcript.lastIndex(where: {
            if case .tool(let t) = $0.kind { return id == nil ? t.state == .running : t.toolID == id }
            return false
        }), case .tool(var t) = transcript[i].kind else { return }
        change(&t)
        transcript[i].kind = .tool(t)
    }

    private func finishRunningTools() {
        for i in transcript.indices {
            if case .tool(var t) = transcript[i].kind, t.state == .running {
                t.state = .done
                transcript[i].kind = .tool(t)
            }
        }
    }

    private func notice(_ s: String, error: Bool) {
        transcript.append(ChatItem(kind: .notice(s, isError: error)))
    }
}
