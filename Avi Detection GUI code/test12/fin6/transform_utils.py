#!/usr/bin/env python3
"""
Deterministic image transforms shared by preview, background handling,
recording previews, and offline processing.

Transform order is:
1. optional horizontal / vertical flip
2. rotation with bound-preserving output canvas
3. optional crop rectangle in the rotated image space
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np


Rect = Tuple[int, int, int, int]


@dataclass
class TransformSettings:
    rotation_deg: float = 0.0
    flip_horizontal: bool = False
    flip_vertical: bool = False
    crop_xywh: Optional[List[int]] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TransformSettings":
        payload = dict(data or {})
        crop = payload.get("crop_xywh")
        if crop is not None:
            crop = [int(v) for v in crop]
        return cls(
            rotation_deg=float(payload.get("rotation_deg", payload.get("rotation", 0.0)) or 0.0),
            flip_horizontal=bool(payload.get("flip_horizontal", payload.get("flip_h", False))),
            flip_vertical=bool(payload.get("flip_vertical", payload.get("flip_v", False))),
            crop_xywh=crop,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if self.crop_xywh is not None:
            payload["crop_xywh"] = [int(v) for v in self.crop_xywh]
        return payload

    def normalized(self) -> "TransformSettings":
        crop = None if self.crop_xywh is None else [max(0, int(v)) for v in self.crop_xywh]
        return TransformSettings(
            rotation_deg=float(self.rotation_deg),
            flip_horizontal=bool(self.flip_horizontal),
            flip_vertical=bool(self.flip_vertical),
            crop_xywh=crop,
        )

    def signature(self) -> str:
        blob = json.dumps(self.normalized().to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def active(self) -> bool:
        crop_active = self.crop_xywh is not None and len(self.crop_xywh) == 4 and int(self.crop_xywh[2]) > 0 and int(self.crop_xywh[3]) > 0
        return abs(float(self.rotation_deg)) > 1e-6 or bool(self.flip_horizontal) or bool(self.flip_vertical) or crop_active


def clamp_crop(crop_xywh: Optional[Sequence[int]], shape_hw: Sequence[int]) -> Optional[List[int]]:
    if crop_xywh is None:
        return None
    if len(crop_xywh) != 4:
        raise ValueError("crop_xywh must contain four integers.")
    h = max(1, int(shape_hw[0]))
    w = max(1, int(shape_hw[1]))
    x, y, cw, ch = [int(v) for v in crop_xywh]
    x = max(0, min(w - 1, x))
    y = max(0, min(h - 1, y))
    cw = max(1, min(w - x, cw))
    ch = max(1, min(h - y, ch))
    return [int(x), int(y), int(cw), int(ch)]


def crop_from_points(start_xy: Sequence[int], end_xy: Sequence[int], min_size: int = 12) -> List[int]:
    x0 = int(min(start_xy[0], end_xy[0]))
    y0 = int(min(start_xy[1], end_xy[1]))
    x1 = int(max(start_xy[0], end_xy[0]))
    y1 = int(max(start_xy[1], end_xy[1]))
    return [x0, y0, max(int(min_size), x1 - x0), max(int(min_size), y1 - y0)]


def rotate_bound(image: np.ndarray, angle_deg: float) -> np.ndarray:
    angle = float(angle_deg)
    if abs(angle) < 1e-6:
        return image.copy()
    h, w = image.shape[:2]
    center = (float(w) / 2.0, float(h) / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos_v = abs(matrix[0, 0])
    sin_v = abs(matrix[0, 1])
    new_w = max(1, int(round((h * sin_v) + (w * cos_v))))
    new_h = max(1, int(round((h * cos_v) + (w * sin_v))))
    matrix[0, 2] += (new_w / 2.0) - center[0]
    matrix[1, 2] += (new_h / 2.0) - center[1]
    return cv2.warpAffine(image, matrix, (new_w, new_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def apply_image_transform(image: np.ndarray, settings: Optional[TransformSettings | Dict[str, Any]] = None) -> np.ndarray:
    if settings is None:
        return image.copy()
    if not isinstance(settings, TransformSettings):
        settings = TransformSettings.from_dict(dict(settings))
    settings = settings.normalized()

    out = image.copy()
    if settings.flip_horizontal:
        out = cv2.flip(out, 1)
    if settings.flip_vertical:
        out = cv2.flip(out, 0)
    out = rotate_bound(out, settings.rotation_deg)
    crop = clamp_crop(settings.crop_xywh, out.shape[:2])
    if crop is not None:
        x, y, w, h = crop
        out = out[y : y + h, x : x + w].copy()
    return out


def transformed_shape(shape_hw: Sequence[int], settings: Optional[TransformSettings | Dict[str, Any]] = None) -> Tuple[int, int]:
    h, w = [int(v) for v in shape_hw[:2]]
    if h <= 0 or w <= 0:
        raise ValueError("Image shape must be positive.")
    probe = np.zeros((h, w, 3), dtype=np.uint8)
    transformed = apply_image_transform(probe, settings=settings)
    th, tw = transformed.shape[:2]
    return int(th), int(tw)


def render_transform_preview(image_bgr: np.ndarray, settings: Optional[TransformSettings | Dict[str, Any]] = None, title: Optional[str] = None) -> np.ndarray:
    transformed = apply_image_transform(image_bgr, settings=settings)
    if title:
        out = transformed.copy()
        cv2.putText(out, title, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(out, title, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (24, 24, 24), 1, cv2.LINE_AA)
        return out
    return transformed


def describe_transform(settings: Optional[TransformSettings | Dict[str, Any]] = None) -> str:
    if settings is None:
        return "identity"
    if not isinstance(settings, TransformSettings):
        settings = TransformSettings.from_dict(dict(settings))
    settings = settings.normalized()
    parts: List[str] = []
    if abs(settings.rotation_deg) > 1e-6:
        parts.append(f"rot={settings.rotation_deg:.2f} deg")
    if settings.flip_horizontal:
        parts.append("flip_h")
    if settings.flip_vertical:
        parts.append("flip_v")
    if settings.crop_xywh is not None:
        x, y, w, h = settings.crop_xywh
        parts.append(f"crop=({x},{y},{w},{h})")
    return ", ".join(parts) if parts else "identity"


def merge_crop_from_regions(regions: Iterable[Rect], padding_px: int = 0) -> Optional[List[int]]:
    boxes = [tuple(int(v) for v in box) for box in regions]
    if not boxes:
        return None
    x0 = min(box[0] for box in boxes) - int(padding_px)
    y0 = min(box[1] for box in boxes) - int(padding_px)
    x1 = max(box[0] + box[2] for box in boxes) + int(padding_px)
    y1 = max(box[1] + box[3] for box in boxes) + int(padding_px)
    return [int(x0), int(y0), int(max(1, x1 - x0)), int(max(1, y1 - y0))]
