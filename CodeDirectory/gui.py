#!/usr/bin/env python3
"""Compatibility shim for the relocated GUI entrypoint."""

import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from host_app.gui.gui import *  # noqa: F401,F403


if __name__ == "__main__":
    import tkinter as tk

    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()
