from __future__ import annotations

import os
import sys
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    return Path(raw_value).expanduser()


_CONFIG_DIR = Path(__file__).resolve().parent
SHARED_DIR = _CONFIG_DIR.parent
REPO_ROOT = SHARED_DIR.parent

CODE_DIRECTORY = REPO_ROOT / "CodeDirectory"
PI_BACKEND_DIR = REPO_ROOT / "pi_backend"
HOST_APP_DIR = REPO_ROOT / "host_app"
VISION_DIR = REPO_ROOT / "fin6"
FIN6_DIR = VISION_DIR
ASSETS_DIR = REPO_ROOT / "assets"
DEPLOYMENT_DIR = REPO_ROOT / "deployment"

CHANNEL_OUTPUT_DIR = _env_path("DROSOPHILA_CHANNEL_OUTPUT_DIR", FIN6_DIR / "outputs" / "channel")
ASSAY_OUTPUT_DIR = _env_path("DROSOPHILA_ASSAY_OUTPUT_DIR", FIN6_DIR / "outputs" / "assay")
MODEL_PATH = _env_path("DROSOPHILA_MODEL_PATH", REPO_ROOT / "best.pt")
TEMP_CLASS_IMAGE_DIR = _env_path("DROSOPHILA_TEMP_CLASS_IMAGE_DIR", CODE_DIRECTORY / "tempClassImage")

DETECTION_RESULT_PATH = CHANNEL_OUTPUT_DIR / "last_channel_result.json"
CHANNEL_ANNOTATED_IMAGE_PATH = CHANNEL_OUTPUT_DIR / "last_channel_annotated.png"
CHANNEL_MASK_IMAGE_PATH = CHANNEL_OUTPUT_DIR / "last_channel_mask.png"

REMOTE_GUI_SETTINGS_PATH = REPO_ROOT / ".drosophila_remote_gui.json"
REMOTE_GUI_SETTINGS_EXAMPLE_PATH = REPO_ROOT / ".drosophila_remote_gui.example.json"
PI_BACKEND_ENV_PATH = DEPLOYMENT_DIR / "pi" / "config" / "backend.env"
PI_BACKEND_ENV_EXAMPLE_PATH = DEPLOYMENT_DIR / "pi" / "config" / "backend.env.example"
WINDOWS_ICON_PATH = ASSETS_DIR / "drosophila.ico"
PNG_ICON_PATH = ASSETS_DIR / "drosophila.png"


def ensure_repo_root_on_path() -> Path:
    repo_root_text = str(REPO_ROOT)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
    return REPO_ROOT


def ensure_code_directory_on_path() -> Path:
    ensure_repo_root_on_path()
    code_directory_text = str(CODE_DIRECTORY)
    if code_directory_text not in sys.path:
        sys.path.insert(0, code_directory_text)
    return CODE_DIRECTORY
