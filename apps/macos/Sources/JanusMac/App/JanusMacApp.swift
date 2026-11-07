import AppKit
import SwiftUI

@main
struct JanusMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var store = AppStore()

    var body: some Scene {
        WindowGroup("Janus") {
            ContentView()
                .environment(store)
                .frame(minWidth: 820, minHeight: 560)
                .task { await store.boot() }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1220, height: 800)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Chat") { store.newChat() }
                    .keyboardShortcut("n")
            }
            CommandMenu("Agent") {
                Button("Stop") { Task { await store.stop() } }
                    .keyboardShortcut(".", modifiers: .command)
                    .disabled(!store.isRunning)
                Button("Restart Backend") { Task { await store.boot() } }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }

        Settings {
            SettingsView()
                .environment(store)
        }
    }
}

/// A SwiftPM executable starts as a background process; make it a regular
/// app with a Dock icon and menu bar, and quit when the window closes.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        Snapshot.scheduleFromEnvironment()
        // `kill`/Ctrl-C skip willTerminate; route them through terminate so the
        // backend child is stopped instead of left running.
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { NSApp.terminate(nil) }
            src.resume()
            signalSources.append(src)
        }
    }

    private var signalSources: [DispatchSourceSignal] = []

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
