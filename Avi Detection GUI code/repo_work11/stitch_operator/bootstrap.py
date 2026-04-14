from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_repo_paths() -> Path:
    root = project_root()
    ordered_paths = [root, root / "CodeDirectory", root / "fin6"]
    for path in reversed(ordered_paths):
        text = str(path)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    return root


PROJECT_ROOT = ensure_repo_paths()
