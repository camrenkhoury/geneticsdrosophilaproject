#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

CONFIG_FILE="pi_app/deployment/config/backend.env"
CONFIG_COMPAT_FILE="deployment/pi/config/backend.env"
CONFIG_EXAMPLE_FILE="pi_app/deployment/config/backend.env.example"
CONFIG_EXAMPLE_COMPAT_FILE="deployment/pi/config/backend.env.example"
if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1091
  source "$CONFIG_FILE"
elif [ -f "$CONFIG_COMPAT_FILE" ]; then
  # shellcheck disable=SC1091
  source "$CONFIG_COMPAT_FILE"
elif [ -f "$CONFIG_EXAMPLE_FILE" ]; then
  # shellcheck disable=SC1091
  source "$CONFIG_EXAMPLE_FILE"
elif [ -f "$CONFIG_EXAMPLE_COMPAT_FILE" ]; then
  # shellcheck disable=SC1091
  source "$CONFIG_EXAMPLE_COMPAT_FILE"
fi

VENV_PATH="${DROSOPHILA_VENV_PATH:-.venv}"
case "$VENV_PATH" in
  /*) ;;
  *) VENV_PATH="$REPO_ROOT/$VENV_PATH" ;;
esac
if [ -d "$VENV_PATH" ]; then
  # shellcheck disable=SC1091
  source "$VENV_PATH/bin/activate"
fi

export DROSOPHILA_API_KEY="${DROSOPHILA_API_KEY:-change-me}"
export GPIOZERO_PIN_FACTORY="${GPIOZERO_PIN_FACTORY:-lgpio}"
DROSOPHILA_BACKEND_APP="${DROSOPHILA_BACKEND_APP:-pi_backend.api.app:app}"
DROSOPHILA_BACKEND_HOST="${DROSOPHILA_BACKEND_HOST:-0.0.0.0}"
DROSOPHILA_BACKEND_PORT="${DROSOPHILA_BACKEND_PORT:-8000}"
DROSOPHILA_BACKEND_RELOAD="${DROSOPHILA_BACKEND_RELOAD:-0}"

STATE_DIR="${DROSOPHILA_STATE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/drosophila-api}"
mkdir -p "$STATE_DIR"

handle_user_stop() {
  if [ -n "${backend_pid:-}" ]; then
    kill -TERM "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
  fi
  exit 0
}

trap handle_user_stop TERM INT HUP

uvicorn_args=(
  -m uvicorn
  "$DROSOPHILA_BACKEND_APP"
  --host "$DROSOPHILA_BACKEND_HOST"
  --port "$DROSOPHILA_BACKEND_PORT"
)

if [ "$DROSOPHILA_BACKEND_RELOAD" = "1" ]; then
  uvicorn_args+=(--reload)
fi

python "${uvicorn_args[@]}" &
backend_pid=$!
set +e
wait "$backend_pid"
exit_code=$?
set -e

if [ "$exit_code" -eq 0 ]; then
  :
fi

exit "$exit_code"
