#!/usr/bin/env bash
set -euo pipefail

sudo apt update
sudo apt install -y \
    python3-picamera2 \
    python3-opencv \
    python3-pil \
    python3-pil.imagetk \
    python3-tk \
    python3-pandas \
    python3-scipy \
    python3-matplotlib \
    ffmpeg

if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
print("[OK] Raspberry Pi dependencies installed.")
PY
fi
