#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
APP_PATH="$REPO_ROOT/CodeDirectory/Launch Drosophila GUI (macOS).app"
INNER_LAUNCHER="$APP_PATH/Contents/MacOS/launch_gui_macos"
CANONICAL_LAUNCHER="$REPO_ROOT/host_app/launchers/macos/launch_gui_macos.sh"

chmod +x "$INNER_LAUNCHER"
chmod +x "$CANONICAL_LAUNCHER"

if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$APP_PATH" >/dev/null 2>&1 || true
fi

printf '%s\n' "macOS launcher setup complete."
printf '%s\n' "App: $APP_PATH"
