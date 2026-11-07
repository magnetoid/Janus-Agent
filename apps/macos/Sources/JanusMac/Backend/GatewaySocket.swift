import Foundation

/// JSON-RPC 2.0 over the dashboard's `/api/ws` (backed by `tui_gateway/server.py`).
/// One JSON object per text frame. Responses resolve the pending call with the
/// same numeric id; `{"method":"event"}` frames go to `onEvent`.
@MainActor
final class GatewaySocket {
    struct RPCError: LocalizedError {
        let code: Int
        let message: String
        var errorDescription: String? { message }
    }

    struct Closed: LocalizedError {
        var errorDescription: String? { "Lost the connection to Janus." }
    }

    /// (type, runtime session id, payload)
    var onEvent: ((String, String, JSONObject) -> Void)?
    var onClose: (() -> Void)?

    private var task: URLSessionWebSocketTask?
    private var nextID = 1
    private var pending: [Int: CheckedContinuation<JSONObject, Error>] = [:]

    func connect(_ c: BackendProcess.Connection) async throws {
        close()
        var comps = URLComponents(url: c.baseURL.appendingPathComponent("api/ws"), resolvingAgainstBaseURL: false)!
        comps.scheme = "ws"
        comps.queryItems = [URLQueryItem(name: "token", value: c.token)]
        let t = URLSession.shared.webSocketTask(with: comps.url!)
        t.maximumMessageSize = 64 * 1024 * 1024
        task = t
        t.resume()
        receive(t)
        // The server greets with gateway.ready; any round trip proves the socket is up.
        _ = try await call("setup.status", [:])
    }

    func close() {
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        failAll(Closed())
    }

    @discardableResult
    func call(_ method: String, _ params: JSONObject) async throws -> JSONObject {
        guard let task else { throw Closed() }
        let id = nextID
        nextID += 1
        let frame: JSONObject = ["jsonrpc": "2.0", "id": id, "method": method, "params": params]
        let text = String(decoding: try JSONSerialization.data(withJSONObject: frame), as: UTF8.self)
        return try await withCheckedThrowingContinuation { cont in
            pending[id] = cont
            task.send(.string(text)) { [weak self] err in
                guard let err else { return }
                Task { @MainActor in self?.pending.removeValue(forKey: id)?.resume(throwing: err) }
            }
        }
    }

    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.task === t else { return }
                switch result {
                case .success(let msg):
                    switch msg {
                    case .string(let s): self.handle(Data(s.utf8))
                    case .data(let d): self.handle(d)
                    @unknown default: break
                    }
                    self.receive(t)
                case .failure:
                    self.task = nil
                    self.failAll(Closed())
                    self.onClose?()
                }
            }
        }
    }

    private func handle(_ data: Data) {
        guard let j = try? JSONSerialization.jsonObject(with: data) as? JSONObject else { return }
        if let id = j.int("id"), let cont = pending.removeValue(forKey: id) {
            if let e = j.obj("error") {
                cont.resume(throwing: RPCError(code: e.int("code") ?? -1, message: e.str("message") ?? "Request failed"))
            } else {
                cont.resume(returning: j.obj("result") ?? [:])
            }
            return
        }
        if j.str("method") == "event", let p = j.obj("params"), let type = p.str("type") {
            onEvent?(type, p.str("session_id") ?? "", p.obj("payload") ?? [:])
        }
    }

    private func failAll(_ e: Error) {
        let all = pending
        pending.removeAll()
        for c in all.values { c.resume(throwing: e) }
    }
}
