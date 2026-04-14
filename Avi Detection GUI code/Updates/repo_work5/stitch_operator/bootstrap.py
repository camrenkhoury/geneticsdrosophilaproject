from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_repo_paths() -> Path:
    root = project_root()
    for path in [root, root / "CodeDirectory", root / "fin6"]:
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    return root


PROJECT_ROOT = ensure_repo_paths()
