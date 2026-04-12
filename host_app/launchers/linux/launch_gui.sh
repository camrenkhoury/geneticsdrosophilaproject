#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
GUI_PATH="host_app/gui/gui.py"
cd "$REPO_ROOT"

if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" "$GUI_PATH"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$GUI_PATH"
fi

if command -v python >/dev/null 2>&1; then
  exec python "$GUI_PATH"
fi

echo "Could not find Python. Install Python 3 or create the local .venv first." >&2
exit 1
