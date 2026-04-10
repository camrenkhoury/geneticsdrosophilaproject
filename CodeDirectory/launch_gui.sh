#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" "gui.py"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "gui.py"
fi

if command -v python >/dev/null 2>&1; then
  exec python "gui.py"
fi

echo "Could not find Python. Install Python 3 or create the local .venv first." >&2
exit 1
