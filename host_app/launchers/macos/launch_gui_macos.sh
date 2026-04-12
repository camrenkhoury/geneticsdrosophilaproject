#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
LOG_PATH="$SCRIPT_DIR/launch_gui_macos.log"

cd "$REPO_ROOT"

PYTHON_BIN=""
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
fi

if [ -z "$PYTHON_BIN" ]; then
  printf '%s\n' "Could not find Python. Install Python 3 or create the local .venv first." >"$LOG_PATH"
  osascript -e "display dialog \"Could not find Python. See $LOG_PATH for details.\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1 || true
  exit 1
fi

if "$PYTHON_BIN" "CodeDirectory/gui.py" >>"$LOG_PATH" 2>&1; then
  exit 0
fi

osascript -e "display dialog \"Drosophila GUI failed to launch. See $LOG_PATH for details.\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1 || true
exit 1
