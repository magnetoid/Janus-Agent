/** True on Apple platforms, where the command key is the menu modifier. */
export function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
}

/** How the palette's shortcut is written on this platform. */
export function commandShortcutLabel(): string {
  return isApplePlatform() ? "⌘K" : "Ctrl K";
}
