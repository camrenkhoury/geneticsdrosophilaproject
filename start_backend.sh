#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/bash "$SCRIPT_DIR/pi_app/scripts/start_backend.sh" "$@"
