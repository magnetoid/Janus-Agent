import Foundation

/// REST calls to the dashboard backend (`janus_cli/routers/*`), authenticated
/// with the per-launch session token the same way Electron main does.
struct JanusAPI {
    let connection: BackendProcess.Connection

    struct HTTPError: LocalizedError {
        let status: Int
        let body: String
        var errorDescription: String? { "Janus returned \(status). \(body.prefix(300))" }
    }

    func request(_ method: String, _ path: String, query: [String: String] = [:], body: JSONObject? = nil) async throws -> Any {
        var comps = URLComponents(url: connection.baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.timeoutInterval = 30
        req.setValue(connection.token, forHTTPHeaderField: "X-Janus-Session-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw HTTPError(status: status, body: String(decoding: data, as: UTF8.self))
        }
        if data.isEmpty { return [:] as JSONObject }
        return try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
    }

    func status() async throws -> JSONObject {
        try await request("GET", "api/status") as? JSONObject ?? [:]
    }

    func sessions(limit: Int = 80) async throws -> [SessionSummary] {
        let r = try await request("GET", "api/sessions", query: [
            "limit": String(limit), "offset": "0", "min_messages": "1",
            "archived": "exclude", "order": "recent",
            // Cron runs live under Scheduled, like the Electron app's split.
            "exclude_sources": "cron",
        ]) as? JSONObject ?? [:]
        return r.arr("sessions").map(SessionSummary.init)
    }

    func messages(sessionID: String) async throws -> [JSONObject] {
        let r = try await request("GET", "api/sessions/\(sessionID)/messages") as? JSONObject ?? [:]
        return r.arr("messages")
    }

    func rename(sessionID: String, title: String) async throws {
        _ = try await request("PATCH", "api/sessions/\(sessionID)", body: ["title": title])
    }

    func archive(sessionID: String) async throws {
        _ = try await request("PATCH", "api/sessions/\(sessionID)", body: ["archived": true])
    }

    func delete(sessionID: String) async throws {
        _ = try await request("DELETE", "api/sessions/\(sessionID)")
    }

    func modelOptions() async throws -> ModelCatalog {
        ModelCatalog(try await request("GET", "api/model/options") as? JSONObject ?? [:])
    }

    /// The configured default model: `(provider, model)`.
    func modelInfo() async throws -> (String, String) {
        let r = try await request("GET", "api/model/info") as? JSONObject ?? [:]
        return (r.str("provider") ?? "", r.str("model") ?? "")
    }

    func setDefaultModel(provider: String, model: String) async throws {
        _ = try await request("POST", "api/model/set", body: ["scope": "main", "provider": provider, "model": model])
    }

    func skills() async throws -> [SkillInfo] {
        let rows = try await request("GET", "api/skills") as? [JSONObject] ?? []
        return rows.map {
            SkillInfo(name: $0.str("name") ?? "", description: $0.str("description") ?? "",
                      category: $0.str("category") ?? "", enabled: ($0["enabled"] as? Bool) ?? true)
        }.sorted { $0.name < $1.name }
    }

    func toggleSkill(_ name: String, enabled: Bool) async throws {
        _ = try await request("PUT", "api/skills/toggle", body: ["name": name, "enabled": enabled])
    }

    func cronJobs() async throws -> [CronJob] {
        let rows = try await request("GET", "api/cron/jobs") as? [JSONObject] ?? []
        return rows.map {
            CronJob(id: $0.str("id") ?? UUID().uuidString, name: $0.str("name") ?? "",
                    schedule: $0.str("schedule_display") ?? "", prompt: $0.str("prompt") ?? "",
                    enabled: ($0["enabled"] as? Bool) ?? true, state: $0.str("state") ?? "",
                    nextRun: $0.str("next_run_at") ?? "")
        }
    }
}
