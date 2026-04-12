from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from shared.config.machine_paths import (
    CHANNEL_OUTPUT_DIRECTORY,
    DETECTION_RESULT_JSON,
    ensure_code_directory_on_path,
)


@dataclass(frozen=True, slots=True)
class BackendRuntimeConfig:
    detection_result_path: Path = DETECTION_RESULT_JSON
    channel_output_directory: Path = CHANNEL_OUTPUT_DIRECTORY
    recent_log_limit: int = 200
    api_key_header_name: str = "X-API-Key"
    api_key_env_var: str = "DROSOPHILA_API_KEY"
    expected_api_key: str | None = None


def build_backend_runtime_config() -> BackendRuntimeConfig:
    ensure_code_directory_on_path()
    api_key_env_var = "DROSOPHILA_API_KEY"
    return BackendRuntimeConfig(
        api_key_env_var=api_key_env_var,
        expected_api_key=os.getenv(api_key_env_var),
    )
