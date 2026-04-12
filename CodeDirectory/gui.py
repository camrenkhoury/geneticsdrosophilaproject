#!/usr/bin/env python3
"""Compatibility shim for the relocated GUI entrypoint."""

from host_app.gui.gui import *  # noqa: F401,F403


if __name__ == "__main__":
    import tkinter as tk

    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()
