from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


DEFAULT_REMOTE_CONFIG_FILENAME = ".drosophila_remote_gui.json"


@dataclass(slots=True)
class RemoteConnectionSettings:
    base_url: str = "http://127.0.0.1:8000"
    api_key: str = ""
    poll_interval_s: float = 1.5
    request_timeout_s: float = 5.0
    config_path: Path | None = None


def load_remote_connection_settings(repo_root: Path) -> RemoteConnectionSettings:
    config_path = repo_root / DEFAULT_REMOTE_CONFIG_FILENAME
    payload: dict[str, object] = {}

    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

    base_url = str(payload.get("base_url", "")).strip() or "http://127.0.0.1:8000"
    api_key = str(payload.get("api_key", "")).strip()
    poll_interval_s = _coerce_positive_float(payload.get("poll_interval_s"), 1.5)
    request_timeout_s = _coerce_positive_float(payload.get("request_timeout_s"), 5.0)

    base_url = os.getenv("DROSOPHILA_REMOTE_URL", base_url).strip() or base_url
    api_key = os.getenv("DROSOPHILA_API_KEY", api_key).strip()
    poll_interval_s = _coerce_positive_float(os.getenv("DROSOPHILA_REMOTE_POLL_INTERVAL"), poll_interval_s)
    request_timeout_s = _coerce_positive_float(os.getenv("DROSOPHILA_REMOTE_TIMEOUT"), request_timeout_s)

    return RemoteConnectionSettings(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        poll_interval_s=poll_interval_s,
        request_timeout_s=request_timeout_s,
        config_path=config_path,
    )


def save_remote_connection_settings(settings: RemoteConnectionSettings) -> Path | None:
    if settings.config_path is None:
        return None

    payload = {
        "base_url": settings.base_url.rstrip("/"),
        "api_key": settings.api_key,
        "poll_interval_s": settings.poll_interval_s,
        "request_timeout_s": settings.request_timeout_s,
    }
    settings.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return settings.config_path


def _coerce_positive_float(value: object, default: float) -> float:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default
