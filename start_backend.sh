#!/bin/bash

set -euo pipefail

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export DROSOPHILA_API_KEY="${DROSOPHILA_API_KEY:-change-me}"
export GPIOZERO_PIN_FACTORY="${GPIOZERO_PIN_FACTORY:-lgpio}"

STATE_DIR="/run/drosophila-api"
STOP_COUNT_FILE="$STATE_DIR/user-stop-count"
STOP_TIME_FILE="$STATE_DIR/user-stop-last-epoch"
STOP_RESET_WINDOW_SEC=300
mkdir -p "$STATE_DIR"

reset_stop_counter_if_stale() {
  if [ ! -f "$STOP_COUNT_FILE" ] || [ ! -f "$STOP_TIME_FILE" ]; then
    return
  fi

  local now_epoch last_epoch
  now_epoch="$(date +%s)"
  last_epoch="$(cat "$STOP_TIME_FILE" 2>/dev/null || echo 0)"
  if [ $((now_epoch - last_epoch)) -gt "$STOP_RESET_WINDOW_SEC" ]; then
    rm -f "$STOP_COUNT_FILE" "$STOP_TIME_FILE"
  fi
}

handle_user_stop() {
  reset_stop_counter_if_stale

  local stop_count now_epoch
  stop_count="$(cat "$STOP_COUNT_FILE" 2>/dev/null || echo 0)"
  stop_count=$((stop_count + 1))
  now_epoch="$(date +%s)"
  printf '%s\n' "$stop_count" > "$STOP_COUNT_FILE"
  printf '%s\n' "$now_epoch" > "$STOP_TIME_FILE"

  if [ -n "${backend_pid:-}" ]; then
    kill -TERM "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
  fi

  if [ "$stop_count" -ge 3 ]; then
    exit 77
  fi
  exit 0
}

trap handle_user_stop TERM INT HUP

python -m uvicorn pi_backend.api.app:app --host 0.0.0.0 --port 8000 &
backend_pid=$!
set +e
wait "$backend_pid"
exit_code=$?
set -e

if [ "$exit_code" -eq 0 ]; then
  rm -f "$STOP_COUNT_FILE" "$STOP_TIME_FILE"
fi

exit "$exit_code"
