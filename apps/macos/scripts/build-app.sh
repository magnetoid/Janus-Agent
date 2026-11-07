#!/bin/bash
# Build Janus.app from the Swift package.
#
#   apps/macos/scripts/build-app.sh            # release build → apps/macos/build/Janus.app
#   JANUS_SOURCE_ROOT=~/c/janus apps/macos/scripts/build-app.sh
#
# JANUS_SOURCE_ROOT is baked into Info.plist as the default backend: a Janus
# checkout with a .venv. Defaults to this repository; Settings can change it.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/../.." && pwd)"
source_root="${JANUS_SOURCE_ROOT:-$repo}"
config="${CONFIG:-release}"
out="$here/build/Janus.app"

cd "$here"
swift build -c "$config"
bin="$(swift build -c "$config" --show-bin-path)/JanusMac"

rm -rf "$out"
mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
cp "$bin" "$out/Contents/MacOS/Janus"

version="$(grep -m1 '^version' "$repo/pyproject.toml" | sed -E 's/.*"(.*)".*/\1/' || true)"
cat > "$out/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Janus</string>
  <key>CFBundleDisplayName</key><string>Janus</string>
  <key>CFBundleIdentifier</key><string>com.cloudindustry.janus.mac</string>
  <key>CFBundleExecutable</key><string>Janus</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${version:-0.0.0}</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>JanusSourceRoot</key><string>${source_root}</string>
</dict>
</plist>
PLIST

# Ad-hoc sign so Gatekeeper lets the local build launch.
codesign --force --sign - "$out" >/dev/null 2>&1 || true
echo "$out"
