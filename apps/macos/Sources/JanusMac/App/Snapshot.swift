import AppKit

/// Development aid: `JANUS_MAC_SNAPSHOT=/path/shot.png` writes a PNG of the
/// main window after `JANUS_MAC_SNAPSHOT_DELAY` seconds (default 8), and
/// again every few seconds while `JANUS_MAC_SNAPSHOT_REPEAT` is set. The app
/// renders its own window, so no screen-recording permission is involved.
enum Snapshot {
    static func scheduleFromEnvironment() {
        let env = ProcessInfo.processInfo.environment
        guard let path = env["JANUS_MAC_SNAPSHOT"], !path.isEmpty else { return }
        let delay = Double(env["JANUS_MAC_SNAPSHOT_DELAY"] ?? "") ?? 8
        let repeatEvery = Double(env["JANUS_MAC_SNAPSHOT_REPEAT"] ?? "")
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { capture(to: path, repeatEvery: repeatEvery) }
    }

    @MainActor
    private static func capture(to path: String, repeatEvery: Double?) {
        if let window = NSApp.windows.first(where: { $0.isVisible && $0.contentView != nil && $0.level == .normal }),
           let view = window.contentView?.superview ?? window.contentView,
           let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
        }
        if let every = repeatEvery, every > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + every) { capture(to: path, repeatEvery: repeatEvery) }
        }
    }
}
