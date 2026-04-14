#!/usr/bin/env python3
"""Compatibility launcher for the unified stitch operator application.

The old debug-heavy Tkinter shell has been replaced by the stitch-first
operator application in ``stitch_operator``. Advanced controls, logs,
manual recovery, and diagnostics now live under Debug / Advanced inside
that shell.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stitch_operator.app import main  # noqa: E402


if __name__ == "__main__":
    main()
