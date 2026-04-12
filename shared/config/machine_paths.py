from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_DIRECTORY = REPO_ROOT / "CodeDirectory"
ASSETS_DIRECTORY = REPO_ROOT / "assets"
FIN6_DIRECTORY = REPO_ROOT / "fin6"
CHANNEL_OUTPUT_DIRECTORY = FIN6_DIRECTORY / "outputs" / "channel"
DETECTION_RESULT_JSON = CHANNEL_OUTPUT_DIRECTORY / "last_channel_result.json"
DETECTION_ANNOTATED_IMAGE = CHANNEL_OUTPUT_DIRECTORY / "last_channel_annotated.png"


def ensure_code_directory_on_path() -> Path:
    """Allow new backend modules to reuse the legacy CodeDirectory modules."""
    code_directory_text = str(CODE_DIRECTORY)
    if code_directory_text not in sys.path:
        sys.path.insert(0, code_directory_text)
    return CODE_DIRECTORY
