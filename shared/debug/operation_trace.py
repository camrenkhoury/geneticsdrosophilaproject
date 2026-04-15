from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from shared.config.project_paths import FIN6_DIR

_TRACE_LOCK = threading.Lock()


def _normalize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    return value


def append_operation_trace(filename: str, subsystem: str, event: str, **fields: Any) -> Path:
    path = FIN6_DIR / filename
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "subsystem": str(subsystem),
        "event": str(event),
        "pid": int(os.getpid()),
        "host": socket.gethostname(),
        "thread": threading.current_thread().name,
    }
    for key, value in fields.items():
        payload[str(key)] = _normalize(value)

    with _TRACE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str))
            handle.write("\n")
    return path
