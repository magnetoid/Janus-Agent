import Foundation

/// Spawns and owns the local `janus dashboard` backend, the same process the
/// Electron app starts (`apps/desktop/electron/main.cjs`): loopback only, a
/// random port, and a per-launch session token passed through the
/// environment. The token never leaves this process and the backend.
@MainActor
final class BackendProcess {
    struct Connection: Equatable {
        let baseURL: URL
        let token: String
    }

    enum LaunchError: LocalizedError {
        case notFound
        case exited(Int32, String)
        case timeout(String)

        var errorDescription: String? {
            switch self {
            case .notFound:
                "Couldn't find Janus. Set the Janus source folder or the janus command in Settings."
            case .exited(let code, let log):
                "The Janus backend exited (\(code)).\n\(log)"
            case .timeout(let log):
                "The Janus backend didn't become ready in time.\n\(log)"
            }
        }
    }

    private(set) var connection: Connection?
    private(set) var label = ""
    private var process: Process?
    private var log = LogBuffer()

    var recentLog: String { log.text }

    func start(settings: BackendSettings) async throws -> Connection {
        stop()
        guard let command = BackendCommand.resolve(settings) else { throw LaunchError.notFound }

        let port = try FreePort.pick()
        let token = Self.randomToken()
        var args = command.prefixArgs
        if !settings.profile.isEmpty { args += ["--profile", settings.profile] }
        args += ["dashboard", "--no-open", "--host", "127.0.0.1", "--port", String(port)]

        var env = ProcessInfo.processInfo.environment
        env.merge(command.env) { _, new in new }
        env["JANUS_DASHBOARD_SESSION_TOKEN"] = token
        // Desktop-spawned dashboards also run the cron tick loop (no gateway here).
        env["JANUS_DESKTOP"] = "1"
        env["PATH"] = Self.loginPath(env["PATH"])

        let p = Process()
        p.executableURL = command.executable
        p.arguments = args
        p.environment = env
        if let cwd = settings.workingDirectory.nonEmpty {
            p.currentDirectoryURL = URL(fileURLWithPath: (cwd as NSString).expandingTildeInPath)
        }
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        let buffer = log
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty { buffer.append(data) }
        }
        log.reset()
        label = command.label
        try p.run()
        process = p

        let base = URL(string: "http://127.0.0.1:\(port)")!
        let conn = Connection(baseURL: base, token: token)
        try await waitUntilReady(conn, process: p)
        connection = conn
        return conn
    }

    func stop() {
        connection = nil
        guard let p = process else { return }
        process = nil
        if p.isRunning {
            p.terminate()
            let pid = p.processIdentifier
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
                if kill(pid, 0) == 0 { kill(pid, SIGKILL) }
            }
        }
    }

    private func waitUntilReady(_ conn: Connection, process p: Process) async throws {
        let deadline = Date().addingTimeInterval(60)
        var req = URLRequest(url: conn.baseURL.appendingPathComponent("api/status"))
        req.setValue("Bearer \(conn.token)", forHTTPHeaderField: "Authorization")
        req.setValue(conn.token, forHTTPHeaderField: "X-Janus-Session-Token")
        req.timeoutInterval = 2
        while Date() < deadline {
            if !p.isRunning { throw LaunchError.exited(p.terminationStatus, log.tail) }
            if let (_, resp) = try? await URLSession.shared.data(for: req),
               (resp as? HTTPURLResponse)?.statusCode == 200 {
                return
            }
            try await Task.sleep(for: .milliseconds(400))
        }
        throw LaunchError.timeout(log.tail)
    }

    private static func randomToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// GUI apps start with a minimal PATH; add the places git, rg, node and
    /// pipx/uv shims usually live so tools the agent runs resolve.
    private static func loginPath(_ current: String?) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let extra = ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin", "\(home)/.cargo/bin"]
        var parts = (current ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
        for e in extra where !parts.contains(e) { parts.append(e) }
        return parts.joined(separator: ":")
    }
}

/// How to run Janus: a source checkout's interpreter, or an installed command.
struct BackendCommand {
    let executable: URL
    let prefixArgs: [String]
    let env: [String: String]
    let label: String

    static func resolve(_ s: BackendSettings) -> BackendCommand? {
        let fm = FileManager.default
        if let root = s.sourceRoot.nonEmpty.map({ ($0 as NSString).expandingTildeInPath }) {
            for py in ["\(root)/.venv/bin/python", "\(root)/venv/bin/python"] where fm.isExecutableFile(atPath: py) {
                return BackendCommand(
                    executable: URL(fileURLWithPath: py),
                    prefixArgs: ["-m", "janus_cli.main"],
                    env: ["PYTHONPATH": root],
                    label: "Janus source at \(root)")
            }
        }
        var candidates: [String] = []
        if let c = s.command.nonEmpty { candidates.append((c as NSString).expandingTildeInPath) }
        let home = fm.homeDirectoryForCurrentUser.path
        candidates += ["\(home)/.local/bin/janus", "/opt/homebrew/bin/janus", "/usr/local/bin/janus"]
        if let path = shellLookup("janus") { candidates.append(path) }
        for c in candidates where fm.isExecutableFile(atPath: c) {
            return BackendCommand(executable: URL(fileURLWithPath: c), prefixArgs: [], env: [:], label: c)
        }
        return nil
    }

    private static func shellLookup(_ name: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "command -v \(name)"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        guard (try? p.run()) != nil else { return nil }
        p.waitUntilExit()
        let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return s?.hasPrefix("/") == true ? s : nil
    }
}

/// Thread-safe ring of the backend's recent output, shown on boot failures.
final class LogBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private let cap = 64 * 1024

    func append(_ d: Data) {
        lock.lock(); defer { lock.unlock() }
        data.append(d)
        if data.count > cap { data.removeFirst(data.count - cap) }
    }

    func reset() {
        lock.lock(); defer { lock.unlock() }
        data.removeAll()
    }

    var text: String {
        lock.lock(); defer { lock.unlock() }
        return String(decoding: data, as: UTF8.self)
    }

    var tail: String {
        text.split(separator: "\n", omittingEmptySubsequences: false).suffix(25).joined(separator: "\n")
    }
}

enum FreePort {
    struct Failure: Error {}

    /// Ask the kernel for an unused loopback port.
    static func pick() throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure() }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let ok = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, len) == 0 && getsockname(fd, $0, &len) == 0
            }
        }
        guard ok else { throw Failure() }
        return Int(UInt16(bigEndian: addr.sin_port))
    }
}

extension String {
    var nonEmpty: String? {
        let t = trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }
}
