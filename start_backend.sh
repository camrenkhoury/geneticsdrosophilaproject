#!/bin/bash

set -e

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export DROSOPHILA_API_KEY="${DROSOPHILA_API_KEY:-change-me}"
export GPIOZERO_PIN_FACTORY="${GPIOZERO_PIN_FACTORY:-lgpio}"

python -m uvicorn pi_backend.api.app:app --host 0.0.0.0 --port 8000
