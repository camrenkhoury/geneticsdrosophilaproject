#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" "CodeDirectory/gui.py"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "CodeDirectory/gui.py"
fi

if command -v python >/dev/null 2>&1; then
  exec python "CodeDirectory/gui.py"
fi

osascript -e 'display dialog "Could not find Python. Install Python 3 or create the local .venv first." buttons {"OK"} default button "OK"' >/dev/null 2>&1 || true
exit 1
