
#!/usr/bin/env python3
"""
Small shared helpers for session naming, JSON IO, image display, and video writing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import cv2
import numpy as np


def ensure_dir(path: str | Path) -> Path:
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj


def save_json(path: str | Path, data: Any) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path_obj, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path_obj


def load_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def timestamp_slug() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def safe_video_writer(path: str | Path, fps: float, frame_size: tuple[int, int]) -> cv2.VideoWriter:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    # Try mp4 first, then AVI.
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path_obj), fourcc, float(fps), frame_size)
    if writer is None or not writer.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        fallback = path_obj.with_suffix(".avi")
        writer = cv2.VideoWriter(str(fallback), fourcc, float(fps), frame_size)
        if writer is None or not writer.isOpened():
            raise RuntimeError(f"Could not create a video writer for {path_obj}")
        return writer
    return writer


def annotate_header(image: np.ndarray, text: str) -> np.ndarray:
    out = image.copy()
    cv2.putText(out, text, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(out, text, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 0, 0), 1, cv2.LINE_AA)
    return out


def resize_for_screen(image: np.ndarray, max_width: int = 1400, max_height: int = 900) -> tuple[np.ndarray, float]:
    h, w = image.shape[:2]
    scale = min(max_width / max(1, w), max_height / max(1, h), 1.0)
    if scale >= 0.999:
        return image.copy(), 1.0
    out = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return out, scale
