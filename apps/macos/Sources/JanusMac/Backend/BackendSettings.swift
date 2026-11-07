import Foundation

/// Where to find Janus and which profile/folder to run it in. Persisted in
/// UserDefaults; the build script seeds `sourceRoot` from the checkout it was
/// built from (Info.plist `JanusSourceRoot`).
struct BackendSettings: Equatable {
    var sourceRoot: String
    var command: String
    var profile: String
    var workingDirectory: String

    private enum Key {
        static let sourceRoot = "backend.sourceRoot"
        static let command = "backend.command"
        static let profile = "backend.profile"
        static let workingDirectory = "backend.workingDirectory"
    }

    static func load(_ d: UserDefaults = .standard) -> BackendSettings {
        let bundled = Bundle.main.object(forInfoDictionaryKey: "JanusSourceRoot") as? String ?? ""
        return BackendSettings(
            sourceRoot: d.string(forKey: Key.sourceRoot) ?? bundled,
            command: d.string(forKey: Key.command) ?? "",
            profile: d.string(forKey: Key.profile) ?? "",
            workingDirectory: d.string(forKey: Key.workingDirectory)
                ?? FileManager.default.homeDirectoryForCurrentUser.path)
    }

    func save(_ d: UserDefaults = .standard) {
        d.set(sourceRoot, forKey: Key.sourceRoot)
        d.set(command, forKey: Key.command)
        d.set(profile, forKey: Key.profile)
        d.set(workingDirectory, forKey: Key.workingDirectory)
    }
}
