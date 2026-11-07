import Foundation

typealias JSONObject = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func str(_ k: String) -> String? {
        switch self[k] {
        case let s as String: s
        case let n as NSNumber: n.stringValue
        default: nil
        }
    }
    func num(_ k: String) -> Double? { (self[k] as? NSNumber)?.doubleValue }
    func int(_ k: String) -> Int? { (self[k] as? NSNumber)?.intValue }
    func bool(_ k: String) -> Bool { (self[k] as? NSNumber)?.boolValue ?? false }
    func obj(_ k: String) -> JSONObject? { self[k] as? JSONObject }
    func arr(_ k: String) -> [JSONObject] { self[k] as? [JSONObject] ?? [] }
}

/// A row from `GET /api/sessions`.
struct SessionSummary: Identifiable, Equatable {
    let id: String
    var title: String
    var preview: String
    var lastActive: Date
    var messageCount: Int
    var source: String
    var model: String

    init(_ j: JSONObject) {
        id = j.str("id") ?? UUID().uuidString
        title = j.str("title")?.nonEmpty ?? ""
        preview = j.str("preview") ?? ""
        lastActive = Date(timeIntervalSince1970: j.num("last_active") ?? j.num("started_at") ?? 0)
        messageCount = j.int("message_count") ?? 0
        source = j.str("source") ?? ""
        model = j.str("model") ?? ""
    }

    var displayTitle: String {
        if !title.isEmpty { return title }
        let p = preview.trimmingCharacters(in: .whitespacesAndNewlines)
        return p.isEmpty ? "Untitled" : String(p.prefix(60))
    }
}

struct ToolActivity: Equatable {
    enum State: Equatable { case running, done, failed }
    var toolID: String
    var name: String
    var context: String
    var state: State
    var summary: String = ""
    var result: String = ""
    var duration: Double?
}

struct ApprovalRequest: Equatable {
    var command: String
    var description: String
    var resolved: String?
}

struct ClarifyRequest: Equatable {
    var requestID: String
    var question: String
    var choices: [String]
    var answer: String?
}

/// One thing in the transcript, in the order it happened.
struct ChatItem: Identifiable, Equatable {
    enum Kind: Equatable {
        case user(String)
        case assistant(text: String, reasoning: String)
        case tool(ToolActivity)
        case approval(ApprovalRequest)
        case clarify(ClarifyRequest)
        case notice(String, isError: Bool)
    }

    let id = UUID()
    var kind: Kind
}

struct ModelOption: Identifiable, Hashable {
    var id: String { "\(provider)/\(model)" }
    let provider: String
    let providerName: String
    let model: String
}

struct ModelCatalog: Equatable {
    var current: String = ""
    var currentProvider: String = ""
    var providers: [(slug: String, name: String, models: [String])] = []

    static func == (a: ModelCatalog, b: ModelCatalog) -> Bool {
        a.current == b.current && a.currentProvider == b.currentProvider
            && a.providers.map(\.slug) == b.providers.map(\.slug)
            && a.providers.map(\.models) == b.providers.map(\.models)
    }

    init() {}

    init(_ j: JSONObject) {
        current = j.str("model") ?? ""
        currentProvider = j.str("provider") ?? ""
        providers = j.arr("providers").compactMap { p in
            guard let slug = p.str("slug") else { return nil }
            if p["authenticated"] as? Bool == false { return nil }
            let models = (p["models"] as? [Any] ?? []).compactMap { m -> String? in
                if let s = m as? String { return s }
                return (m as? JSONObject)?.str("id") ?? (m as? JSONObject)?.str("name")
            }
            return models.isEmpty ? nil : (slug, p.str("name") ?? slug, models)
        }
    }
}

struct Usage: Equatable {
    var contextPercent: Double?
    var costUSD: Double?
    var total: Int?

    init() {}
    init(_ j: JSONObject?) {
        guard let j else { return }
        contextPercent = j.num("context_percent")
        costUSD = j.num("cost_usd")
        total = j.int("total")
    }
}

struct SkillInfo: Identifiable, Equatable {
    var id: String { name }
    let name: String
    let description: String
    let category: String
    var enabled: Bool
}

struct CronJob: Identifiable, Equatable {
    let id: String
    let name: String
    let schedule: String
    let prompt: String
    let enabled: Bool
    let state: String
    let nextRun: String
}

/// Turn stored DB rows (`GET /api/sessions/{id}/messages`) into transcript items.
enum TranscriptDecoder {
    static func items(from rows: [JSONObject]) -> [ChatItem] {
        var items: [ChatItem] = []
        var toolIndex: [String: Int] = [:]
        for row in rows {
            let role = row.str("role") ?? ""
            let content = text(of: row["content"])
            switch role {
            case "user":
                if !content.isEmpty { items.append(ChatItem(kind: .user(content))) }
            case "assistant":
                let reasoning = row.str("reasoning") ?? row.str("reasoning_content") ?? ""
                if !content.isEmpty || !reasoning.isEmpty {
                    items.append(ChatItem(kind: .assistant(text: content, reasoning: reasoning)))
                }
                for call in toolCalls(row["tool_calls"]) {
                    let fn = call.obj("function") ?? [:]
                    let id = call.str("id") ?? UUID().uuidString
                    let tool = ToolActivity(toolID: id, name: fn.str("name") ?? "tool",
                                            context: argsPreview(fn.str("arguments") ?? ""), state: .done)
                    toolIndex[id] = items.count
                    items.append(ChatItem(kind: .tool(tool)))
                }
            case "tool":
                if let id = row.str("tool_call_id"), let i = toolIndex[id],
                   case .tool(var t) = items[i].kind {
                    t.result = String(content.prefix(4000))
                    items[i].kind = .tool(t)
                }
            default:
                continue
            }
        }
        return items
    }

    static func text(of content: Any?) -> String {
        if let s = content as? String { return s }
        if let parts = content as? [JSONObject] {
            return parts.compactMap { $0.str("text") }.joined(separator: "\n")
        }
        return ""
    }

    private static func toolCalls(_ v: Any?) -> [JSONObject] {
        if let a = v as? [JSONObject] { return a }
        if let s = v as? String, let d = s.data(using: .utf8),
           let a = try? JSONSerialization.jsonObject(with: d) as? [JSONObject] { return a }
        return []
    }

    /// The first argument value is usually the interesting one (path, command, query).
    static func argsPreview(_ args: String) -> String {
        guard let d = args.data(using: .utf8),
              let j = try? JSONSerialization.jsonObject(with: d) as? JSONObject else {
            return String(args.prefix(120))
        }
        for k in ["command", "action", "path", "file_path", "query", "url", "goal", "name", "pattern"] {
            if let v = j.str(k) { return String(v.prefix(120)) }
        }
        return j.values.compactMap { $0 as? String }.first.map { String($0.prefix(120)) } ?? ""
    }
}
