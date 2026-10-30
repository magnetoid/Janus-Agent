// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "JanusMac",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "JanusMac", path: "Sources/JanusMac")
    ]
)
