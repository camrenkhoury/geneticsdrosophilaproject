#!/usr/bin/env python3
"""
Shared helpers for JSON IO, safe directory handling, timestamped session names,
and OpenCV video writer fallback logic.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

import cv2
import numpy as np


def ensure_dir(path: str | Path) -> Path:
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj


def save_json(path: str | Path, data: Any) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path_obj, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
    return path_obj


def load_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def timestamp_slug() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def safe_video_writer(path: str | Path, fps: float, frame_size: tuple[int, int]) -> cv2.VideoWriter:
    writer, _actual_path = open_video_writer_with_path(path, fps=fps, frame_size=frame_size)
    return writer


def open_video_writer_with_path(path: str | Path, fps: float, frame_size: tuple[int, int]) -> tuple[cv2.VideoWriter, Path]:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path_obj), fourcc, float(fps), frame_size)
    if writer is not None and writer.isOpened():
        return writer, path_obj

    fourcc = cv2.VideoWriter_fourcc(*"XVID")
    fallback = path_obj.with_suffix(".avi")
    writer = cv2.VideoWriter(str(fallback), fourcc, float(fps), frame_size)
    if writer is None or not writer.isOpened():
        raise RuntimeError(f"Could not create a video writer for {path_obj}")
    return writer, fallback


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


def sha1_file(path: str | Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_file(src: str | Path, dst: str | Path) -> Path:
    src_path = Path(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dst_path)
    return dst_path


def newest_child_dir(root: str | Path, prefix: str = "") -> Optional[Path]:
    root_path = Path(root)
    if not root_path.exists():
        return None
    candidates = [path for path in root_path.iterdir() if path.is_dir() and (not prefix or path.name.startswith(prefix))]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]
