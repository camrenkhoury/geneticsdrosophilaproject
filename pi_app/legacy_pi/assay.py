from __future__ import annotations

import importlib
from typing import Any

from shared.config.project_paths import ensure_code_directory_on_path, ensure_repo_root_on_path


def assay(*, preview_callback=None, stop_event=None) -> dict[str, Any]:
    ensure_repo_root_on_path()
    ensure_code_directory_on_path()
    try:
        operator_bridge = importlib.import_module("host_app.operator_bridge")
    except Exception as exc:
        raise RuntimeError(f"Could not import fin6 assay bridge: {type(exc).__name__}: {exc}") from exc
    return operator_bridge.run_assay_from_saved_settings(
        preview_callback=preview_callback,
        stop_event=stop_event,
    )
