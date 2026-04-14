
#!/usr/bin/env python3
"""
Assay calibration, detection, tracking, reporting orchestration.

The runtime detector consumes per-vial rectangular ROIs plus top/baseline
references. Calibration is intentionally separated from the detector so the GUI
can offer a richer editing workflow without disturbing the detection backend.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np
import pandas as pd

try:
    from .camera_sources import capture_background_image, open_assay_camera, normalize_assay_camera_backend
    from .shared_utils import ensure_dir, load_json, save_json, safe_video_writer, timestamp_slug
except ImportError:
    from camera_sources import capture_background_image, open_assay_camera, normalize_assay_camera_backend
    from shared_utils import ensure_dir, load_json, save_json, safe_video_writer, timestamp_slug

try:
    from scipy.optimize import linear_sum_assignment  # type: ignore
except Exception:  # pragma: no cover - optional
    linear_sum_assignment = None

try:
    from scipy.stats import linregress  # type: ignore
except Exception:  # pragma: no cover - optional
    linregress = None


Point = Tuple[int, int]
Rect = Tuple[int, int, int, int]


@dataclass
class VialCalibration:
    physical_index: int
    assay_index: Optional[int]
    enabled: bool
    roi_xywh: List[int]
    top_point_px: List[int]
    baseline_point_px: List[int]
    quad_points_px: Optional[List[List[int]]] = None
    tube_height_mm: Optional[float] = None
    tube_width_mm: Optional[float] = None
    label: Optional[str] = None
    group_id: Optional[str] = None

    @property
    def roi(self) -> Rect:
        return tuple(int(v) for v in self.roi_xywh)  # type: ignore[return-value]

    @property
    def top_y(self) -> int:
        return int(self.top_point_px[1])

    @property
    def baseline_y(self) -> int:
        return int(self.baseline_point_px[1])

    @property
    def center_x(self) -> float:
        return float(self.top_point_px[0] + self.baseline_point_px[0]) / 2.0

    @property
    def height_px(self) -> int:
        return max(1, int(self.baseline_y - self.top_y))

    @property
    def width_px(self) -> int:
        return max(1, int(self.roi_xywh[2]))


@dataclass
class AssayCalibration:
    background_path: Optional[str]
    image_shape_hw: List[int]
    vials: List[VialCalibration]
    schema_version: int = 3
    editor_mode: str = "rectangles"
    editor_meta: Dict[str, Any] = field(default_factory=dict)
    ignored_physical_indices: List[int] = field(default_factory=lambda: [1])

    def to_dict(self) -> Dict[str, Any]:
        normalized = normalize_assay_calibration(self)
        return {
            "schema_version": int(normalized.schema_version),
            "background_path": normalized.background_path,
            "image_shape_hw": [int(normalized.image_shape_hw[0]), int(normalized.image_shape_hw[1])],
            "editor_mode": str(normalized.editor_mode),
            "editor_meta": dict(normalized.editor_meta or {}),
            "ignored_physical_indices": [int(x) for x in normalized.ignored_physical_indices],
            "vials": [asdict(v) for v in normalized.vials],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssayCalibration":
        ignored = [int(x) for x in data.get("ignored_physical_indices", [1])]
        raw_vials: List[VialCalibration] = []
        for idx, item in enumerate(data.get("vials", []), start=1):
            payload = dict(item)
            payload.setdefault("physical_index", idx)
            payload.setdefault("enabled", int(payload["physical_index"]) not in set(ignored))
            payload.setdefault("assay_index", None)
            payload.setdefault("tube_height_mm", None)
            payload.setdefault("tube_width_mm", None)
            payload.setdefault("quad_points_px", None)
            payload.setdefault("label", None)
            payload.setdefault("group_id", None)
            raw_vials.append(VialCalibration(**payload))
        calibration = cls(
            background_path=data.get("background_path"),
            image_shape_hw=list(data["image_shape_hw"]),
            vials=raw_vials,
            schema_version=int(data.get("schema_version", 1)),
            editor_mode=str(data.get("editor_mode", "rectangles")),
            editor_meta=dict(data.get("editor_meta", {})),
            ignored_physical_indices=ignored,
        )
        return normalize_assay_calibration(calibration)

    @property
    def enabled_vials(self) -> List[VialCalibration]:
        return [v for v in self.vials if v.enabled]


@dataclass
class Detection:
    physical_vial_index: int
    assay_tube_index: int
    bbox_xywh: List[int]
    center_xy_px: List[float]
    area_px: int
    frame_index: int
    time_s: float
    x_from_left_px: float
    x_from_left_mm: Optional[float]
    y_from_base_px: float
    y_from_base_mm: Optional[float]
    distance_from_base_px: float
    distance_from_base_mm: Optional[float]
    relative_x: float
    relative_height: float
    threshold_used: float


@dataclass
class Track:
    internal_id: int
    display_id: int
    physical_vial_index: int
    assay_tube_index: int
    last_x: float
    last_y: float
    vx: float = 0.0
    vy: float = 0.0
    age_frames: int = 0
    missed_frames: int = 0
    active: bool = True
    history: List[Dict[str, Any]] = field(default_factory=list)
    retired_frame_index: Optional[int] = None
    retired_time_s: Optional[float] = None

    def predict(self, dt: float) -> Tuple[float, float]:
        return self.last_x + self.vx * dt, self.last_y + self.vy * dt

    def add_observation(self, det: Detection, dt: float) -> None:
        if dt > 1e-9:
            self.vx = (float(det.center_xy_px[0]) - self.last_x) / dt
            self.vy = (float(det.center_xy_px[1]) - self.last_y) / dt
        self.last_x = float(det.center_xy_px[0])
        self.last_y = float(det.center_xy_px[1])
        self.age_frames += 1
        self.missed_frames = 0
        self.history.append(
            {
                "frame_index": int(det.frame_index),
                "time_s": float(det.time_s),
                "x_px": float(det.center_xy_px[0]),
                "y_px": float(det.center_xy_px[1]),
                "bbox_x": int(det.bbox_xywh[0]),
                "bbox_y": int(det.bbox_xywh[1]),
                "bbox_w": int(det.bbox_xywh[2]),
                "bbox_h": int(det.bbox_xywh[3]),
                "x_from_left_px": float(det.x_from_left_px),
                "x_from_left_mm": None if det.x_from_left_mm is None else float(det.x_from_left_mm),
                "y_from_base_px": float(det.y_from_base_px),
                "y_from_base_mm": None if det.y_from_base_mm is None else float(det.y_from_base_mm),
                "distance_from_base_px": float(det.distance_from_base_px),
                "distance_from_base_mm": None if det.distance_from_base_mm is None else float(det.distance_from_base_mm),
                "relative_x": float(det.relative_x),
                "relative_height": float(det.relative_height),
                "detected": True,
            }
        )

    def add_missed_prediction(
        self,
        frame_index: int,
        time_s: float,
        pred_x: float,
        pred_y: float,
        vial: Optional[VialCalibration] = None,
    ) -> None:
        self.last_x = pred_x
        self.last_y = pred_y
        self.age_frames += 1
        self.missed_frames += 1
        pos = _position_in_vial(vial, pred_x, pred_y) if vial is not None else {}
        self.history.append(
            {
                "frame_index": int(frame_index),
                "time_s": float(time_s),
                "x_px": float(pred_x),
                "y_px": float(pred_y),
                "bbox_x": None,
                "bbox_y": None,
                "bbox_w": None,
                "bbox_h": None,
                "x_from_left_px": pos.get("x_from_left_px"),
                "x_from_left_mm": pos.get("x_from_left_mm"),
                "y_from_base_px": pos.get("y_from_base_px"),
                "y_from_base_mm": pos.get("y_from_base_mm"),
                "distance_from_base_px": pos.get("y_from_base_px"),
                "distance_from_base_mm": pos.get("y_from_base_mm"),
                "relative_x": pos.get("relative_x"),
                "relative_height": pos.get("relative_height"),
                "detected": False,
            }
        )


def save_assay_calibration(path: str | Path, calibration: AssayCalibration) -> Path:
    return save_json(path, normalize_assay_calibration(calibration).to_dict())


def load_assay_calibration(path: str | Path) -> AssayCalibration:
    return AssayCalibration.from_dict(load_json(path))


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _normalize_roi(roi_xywh: Sequence[int], image_shape_hw: Sequence[int]) -> List[int]:
    img_h = max(1, int(image_shape_hw[0]))
    img_w = max(1, int(image_shape_hw[1]))
    x = _clamp(int(roi_xywh[0]), 0, max(0, img_w - 2))
    y = _clamp(int(roi_xywh[1]), 0, max(0, img_h - 2))
    w = _clamp(int(roi_xywh[2]), 12, max(12, img_w - x))
    h = _clamp(int(roi_xywh[3]), 12, max(12, img_h - y))
    w = min(w, img_w - x)
    h = min(h, img_h - y)
    return [int(x), int(y), int(w), int(h)]


def _order_quad_points(points: Sequence[Sequence[int]]) -> List[List[int]]:
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    if pts.shape[0] != 4:
        raise ValueError("Expected exactly four corner points.")
    order_y = np.argsort(pts[:, 1], kind="mergesort")
    top = pts[order_y[:2]]
    bottom = pts[order_y[2:]]
    top = top[np.argsort(top[:, 0], kind="mergesort")]
    bottom = bottom[np.argsort(bottom[:, 0], kind="mergesort")]
    ordered = np.array([top[0], top[1], bottom[1], bottom[0]], dtype=np.float32)
    return [[int(round(x)), int(round(y))] for x, y in ordered]


def _normalize_quad_points(quad_points_px: Optional[Sequence[Sequence[int]]], image_shape_hw: Sequence[int]) -> Optional[List[List[int]]]:
    if not quad_points_px:
        return None
    ordered = _order_quad_points(list(quad_points_px)[:4])
    img_h = max(1, int(image_shape_hw[0]))
    img_w = max(1, int(image_shape_hw[1]))
    out: List[List[int]] = []
    for x, y in ordered:
        out.append([_clamp(int(x), 0, img_w - 1), _clamp(int(y), 0, img_h - 1)])
    return out


def _quad_bbox(quad_points_px: Sequence[Sequence[int]]) -> List[int]:
    xs = [int(pt[0]) for pt in quad_points_px]
    ys = [int(pt[1]) for pt in quad_points_px]
    x0 = min(xs)
    y0 = min(ys)
    x1 = max(xs)
    y1 = max(ys)
    return [int(x0), int(y0), max(12, int(x1 - x0 + 1)), max(12, int(y1 - y0 + 1))]


def _midpoint(a: Sequence[int], b: Sequence[int]) -> List[int]:
    return [int(round((float(a[0]) + float(b[0])) / 2.0)), int(round((float(a[1]) + float(b[1])) / 2.0))]


def _line_x_at_y(p0: Sequence[int], p1: Sequence[int], y: float) -> float:
    x0 = float(p0[0])
    y0 = float(p0[1])
    x1 = float(p1[0])
    y1 = float(p1[1])
    if abs(y1 - y0) < 1e-6:
        return (x0 + x1) / 2.0
    t = (float(y) - y0) / (y1 - y0)
    t = max(0.0, min(1.0, t))
    return x0 + t * (x1 - x0)


def _line_y_at_x(p0: Sequence[int], p1: Sequence[int], x: float) -> float:
    x0 = float(p0[0])
    y0 = float(p0[1])
    x1 = float(p1[0])
    y1 = float(p1[1])
    if abs(x1 - x0) < 1e-6:
        return (y0 + y1) / 2.0
    t = (float(x) - x0) / (x1 - x0)
    t = max(0.0, min(1.0, t))
    return y0 + t * (y1 - y0)


def _vial_quad_points(vial: VialCalibration) -> Optional[List[List[int]]]:
    quad_points = getattr(vial, "quad_points_px", None)
    if not quad_points or len(quad_points) != 4:
        return None
    return [[int(pt[0]), int(pt[1])] for pt in quad_points]


def _vial_polygon_points(vial: VialCalibration) -> np.ndarray:
    quad_points = _vial_quad_points(vial)
    if quad_points is not None:
        return np.asarray(quad_points, dtype=np.int32)
    x, y, w, h = vial.roi
    return np.asarray([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)


def _normalize_vial(vial: VialCalibration, image_shape_hw: Sequence[int], physical_index: int) -> VialCalibration:
    quad_points = _normalize_quad_points(getattr(vial, "quad_points_px", None), image_shape_hw)
    if quad_points is not None:
        x, y, w, h = _normalize_roi(_quad_bbox(quad_points), image_shape_hw)
        default_top = _midpoint(quad_points[0], quad_points[1])
        default_base = _midpoint(quad_points[3], quad_points[2])
        top_x = _clamp(int(default_top[0]), x, x + w - 1)
        top_y = _clamp(int(default_top[1]), y, y + h - 2)
        base_x = _clamp(int(default_base[0]), x, x + w - 1)
        base_y = _clamp(int(default_base[1]), top_y + 1, y + h - 1)
    else:
        x, y, w, h = _normalize_roi(vial.roi_xywh, image_shape_hw)
        cx = x + w // 2
        default_top_y = y + max(1, h // 6)
        default_base_y = y + max(2, int(round(h * 0.85)))
        top_x = _clamp(int((vial.top_point_px or [cx, default_top_y])[0]), x, x + w - 1)
        top_y = _clamp(int((vial.top_point_px or [cx, default_top_y])[1]), y, y + h - 2)
        base_x = _clamp(int((vial.baseline_point_px or [cx, default_base_y])[0]), x, x + w - 1)
        base_y = _clamp(int((vial.baseline_point_px or [cx, default_base_y])[1]), top_y + 1, y + h - 1)
    return VialCalibration(
        physical_index=int(physical_index),
        assay_index=None,
        enabled=bool(vial.enabled),
        roi_xywh=[int(x), int(y), int(w), int(h)],
        top_point_px=[int(top_x), int(top_y)],
        baseline_point_px=[int(base_x), int(base_y)],
        quad_points_px=None if quad_points is None else [[int(px), int(py)] for px, py in quad_points],
        tube_height_mm=None if vial.tube_height_mm is None else float(vial.tube_height_mm),
        tube_width_mm=None if vial.tube_width_mm is None else float(vial.tube_width_mm),
        label=vial.label,
        group_id=vial.group_id,
    )


def normalize_assay_calibration(calibration: AssayCalibration, sort_by_position: bool = False) -> AssayCalibration:
    image_shape_hw = [int(calibration.image_shape_hw[0]), int(calibration.image_shape_hw[1])]
    items = list(calibration.vials)
    if sort_by_position:
        items.sort(key=lambda v: (int(v.roi_xywh[0]), int(v.roi_xywh[1])))

    normalized_vials: List[VialCalibration] = []
    ignored: List[int] = []
    assay_counter = 1
    for physical_index, vial in enumerate(items, start=1):
        clean = _normalize_vial(vial, image_shape_hw=image_shape_hw, physical_index=physical_index)
        if clean.enabled:
            clean.assay_index = assay_counter
            assay_counter += 1
        else:
            clean.assay_index = None
            ignored.append(int(physical_index))
        if not clean.label:
            clean.label = f"Vial {physical_index}"
        normalized_vials.append(clean)

    return AssayCalibration(
        background_path=calibration.background_path,
        image_shape_hw=image_shape_hw,
        vials=normalized_vials,
        schema_version=max(4, int(calibration.schema_version or 4)),
        editor_mode=str(calibration.editor_mode or "rectangles"),
        editor_meta=dict(calibration.editor_meta or {}),
        ignored_physical_indices=ignored,
    )


def build_assay_calibration(
    background_bgr: np.ndarray,
    vials: Sequence[VialCalibration],
    background_path: Optional[str] = None,
    editor_mode: str = "rectangles",
    editor_meta: Optional[Dict[str, Any]] = None,
) -> AssayCalibration:
    return normalize_assay_calibration(
        AssayCalibration(
            background_path=None if background_path is None else str(Path(background_path).resolve()),
            image_shape_hw=[int(background_bgr.shape[0]), int(background_bgr.shape[1])],
            vials=list(vials),
            schema_version=4,
            editor_mode=str(editor_mode),
            editor_meta=dict(editor_meta or {}),
            ignored_physical_indices=[],
        )
    )


def select_vial_rois(image_bgr: np.ndarray, expected_count: int = 0, window_name: str = "Select vial ROIs") -> List[Rect]:
    display = image_bgr.copy()
    cv2.putText(
        display,
        "Draw ROI for each vial (left to right). ENTER/SPACE to accept, C to cancel.",
        (16, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    rois = cv2.selectROIs(window_name, display, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(window_name)
    if rois is None or len(rois) == 0:
        raise KeyboardInterrupt("Assay ROI selection cancelled.")
    rois_out = [tuple(int(v) for v in roi) for roi in rois]
    rois_out.sort(key=lambda r: r[0])

    if expected_count > 0 and len(rois_out) != expected_count:
        raise ValueError(
            f"Expected exactly {expected_count} ROIs, but got {len(rois_out)}. "
            "Please run calibration again and draw one ROI per vial."
    )
    return rois_out


def _rect_from_drag_points(start: Point, end: Point) -> Rect:
    x0 = min(int(start[0]), int(end[0]))
    y0 = min(int(start[1]), int(end[1]))
    x1 = max(int(start[0]), int(end[0]))
    y1 = max(int(start[1]), int(end[1]))
    return (int(x0), int(y0), max(12, int(x1 - x0)), max(12, int(y1 - y0)))


def _draw_guided_vial_screen(
    image_bgr: np.ndarray,
    accepted_shapes: Sequence[Dict[str, Any]],
    active_roi: Optional[Rect],
    active_quad_points: Sequence[Point],
    mode: str,
    vial_index: int,
    total_vials: int,
) -> np.ndarray:
    display = image_bgr.copy()
    tint = display.copy()

    for idx, shape in enumerate(accepted_shapes, start=1):
        quad_points = shape.get("quad_points")
        roi = shape.get("roi")
        if quad_points:
            pts = np.asarray(quad_points, dtype=np.int32)
            cv2.fillConvexPoly(tint, pts, (60, 150, 255))
            cv2.polylines(display, [pts.reshape((-1, 1, 2))], True, (60, 150, 255), 2, cv2.LINE_AA)
            x, y, w, h = _quad_bbox(quad_points)
        else:
            x, y, w, h = [int(v) for v in roi]
            cv2.rectangle(tint, (x, y), (x + w, y + h), (60, 150, 255), -1)
            cv2.rectangle(display, (x, y), (x + w, y + h), (60, 150, 255), 2)
        label = f"Vial {idx}"
        cv2.putText(display, label, (x + 4, max(18, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(display, label, (x + 4, max(18, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (40, 40, 40), 1, cv2.LINE_AA)

    if accepted_shapes:
        cv2.addWeighted(tint, 0.06, display, 0.94, 0, display)

    if active_roi is not None and mode == "rect":
        x, y, w, h = [int(v) for v in active_roi]
        cv2.rectangle(display, (x, y), (x + w, y + h), (0, 220, 255), 2)
        cv2.putText(display, f"Vial {vial_index}", (x + 4, max(18, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(display, f"Vial {vial_index}", (x + 4, max(18, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 140, 180), 1, cv2.LINE_AA)

    if mode == "points" and active_quad_points:
        pts = np.asarray(active_quad_points, dtype=np.int32)
        for pt_idx, (px, py) in enumerate(active_quad_points, start=1):
            cv2.circle(display, (int(px), int(py)), 5, (0, 220, 255), -1)
            cv2.putText(display, str(pt_idx), (int(px) + 6, int(py) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(display, str(pt_idx), (int(px) + 6, int(py) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (0, 140, 180), 1, cv2.LINE_AA)
        if len(active_quad_points) >= 2:
            cv2.polylines(display, [pts.reshape((-1, 1, 2))], False, (0, 220, 255), 2, cv2.LINE_AA)
        if len(active_quad_points) == 4:
            ordered = np.asarray(_order_quad_points(active_quad_points), dtype=np.int32)
            cv2.polylines(display, [ordered.reshape((-1, 1, 2))], True, (0, 220, 255), 2, cv2.LINE_AA)

    mode_label = "4-point bounds" if mode == "points" else "drag box"
    points_hint = "Click top-left, top-right, bottom-right, bottom-left." if mode == "points" else "Drag a tight rectangle around the vial walls."
    text_lines = [
        f"Set vial {vial_index} of {total_vials}. B=box  P=4 points  ENTER=accept  R=reset  ESC=cancel",
        f"Mode: {mode_label}. {points_hint}",
    ]
    for idx, line in enumerate(text_lines):
        y = 28 + idx * 28
        cv2.putText(display, line, (18, y), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(display, line, (18, y), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (20, 20, 20), 1, cv2.LINE_AA)
    return display


def select_single_vial_bounds(
    image_bgr: np.ndarray,
    vial_index: int,
    total_vials: int,
    accepted_shapes: Sequence[Dict[str, Any]],
    window_name: str = "Assay calibration",
) -> Dict[str, Any]:
    current_roi: Optional[Rect] = None
    current_quad_points: List[Point] = []
    drag_start: Optional[Point] = None
    dragging = False
    mode = "rect"

    def on_mouse(event, x, y, flags, param):
        nonlocal current_roi, current_quad_points, drag_start, dragging
        point = (int(x), int(y))
        if mode == "points":
            if event == cv2.EVENT_LBUTTONDOWN and len(current_quad_points) < 4:
                current_quad_points.append(point)
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            drag_start = point
            current_roi = None
            dragging = True
        elif event == cv2.EVENT_MOUSEMOVE and dragging and drag_start is not None:
            current_roi = _rect_from_drag_points(drag_start, point)
        elif event == cv2.EVENT_LBUTTONUP and drag_start is not None:
            current_roi = _rect_from_drag_points(drag_start, point)
            dragging = False

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, on_mouse)

    while True:
        cv2.imshow(window_name, _draw_guided_vial_screen(image_bgr, accepted_shapes, current_roi, current_quad_points, mode, vial_index, total_vials))
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10):
            if mode == "points" and len(current_quad_points) == 4:
                quad_points = _order_quad_points(current_quad_points)
                x, y, w, h = _quad_bbox(quad_points)
                cv2.destroyWindow(window_name)
                return {"roi": (int(x), int(y), int(w), int(h)), "quad_points": quad_points, "mode": "quad"}
            if mode == "rect" and current_roi is not None:
                x, y, w, h = current_roi
                if w >= 12 and h >= 12:
                    cv2.destroyWindow(window_name)
                    return {"roi": current_roi, "quad_points": None, "mode": "rect"}
        if key in (ord("r"), ord("R")):
            current_roi = None
            current_quad_points = []
            drag_start = None
            dragging = False
        if key in (ord("p"), ord("P")):
            mode = "points"
            current_roi = None
            current_quad_points = []
            drag_start = None
            dragging = False
        if key in (ord("b"), ord("B")):
            mode = "rect"
            current_roi = None
            current_quad_points = []
            drag_start = None
            dragging = False
        if key == 27:
            cv2.destroyWindow(window_name)
            raise KeyboardInterrupt("Calibration cancelled.")


def click_two_points(
    image_bgr: np.ndarray,
    prompt: str,
    window_name: str = "Calibration",
    circle_color: Tuple[int, int, int] = (0, 255, 0),
) -> Tuple[Point, Point]:
    points: List[Point] = []
    preview = image_bgr.copy()

    def on_mouse(event, x, y, flags, param):
        nonlocal preview, points
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 2:
            points.append((int(x), int(y)))
            cv2.circle(preview, (x, y), 4, circle_color, -1)
            if len(points) == 2:
                cv2.line(preview, points[0], points[1], circle_color, 1, cv2.LINE_AA)
            cv2.imshow(window_name, preview)

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, on_mouse)

    while True:
        display = preview.copy()
        cv2.putText(display, prompt, (20, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(display, prompt, (20, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(display, "ENTER=accept, R=reset, ESC=cancel", (20, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(display, "ENTER=accept, R=reset, ESC=cancel", (20, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.imshow(window_name, display)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10) and len(points) == 2:
            cv2.destroyWindow(window_name)
            return points[0], points[1]
        if key in (ord("r"), ord("R")):
            points = []
            preview = image_bgr.copy()
        if key == 27:
            cv2.destroyWindow(window_name)
            raise KeyboardInterrupt("Calibration cancelled.")


def calibrate_assay_interactive(
    background_path: str | Path,
    output_json: str | Path,
    total_vials: int = 0,
    ignored_physical_indices: Sequence[int] = (1,),
    tube_height_mm: Optional[float] = None,
    tube_width_mm: Optional[float] = None,
) -> AssayCalibration:
    bg = cv2.imread(str(background_path), cv2.IMREAD_COLOR)
    if bg is None:
        raise FileNotFoundError(f"Could not read background image: {background_path}")

    expected_count = int(total_vials)
    if expected_count > 0:
        guided_shapes: List[Dict[str, Any]] = []
        for vial_index in range(1, expected_count + 1):
            guided_shapes.append(
                select_single_vial_bounds(
                    bg,
                    vial_index=vial_index,
                    total_vials=expected_count,
                    accepted_shapes=guided_shapes,
                )
            )
    else:
        guided_shapes = [{"roi": roi, "quad_points": None, "mode": "rect"} for roi in select_vial_rois(bg, expected_count=0)]
    vials: List[VialCalibration] = []

    for i, shape in enumerate(guided_shapes, start=1):
        roi = shape["roi"]
        x, y, w, h = roi
        quad_points = shape.get("quad_points")
        if quad_points:
            ordered_quad = _order_quad_points(quad_points)
            top_point = _midpoint(ordered_quad[0], ordered_quad[1])
            base_point = _midpoint(ordered_quad[3], ordered_quad[2])
        else:
            ordered_quad = None
            top_point = [int(x + w // 2), int(y)]
            base_point = [int(x + w // 2), int(y + h - 1)]
        vials.append(
            VialCalibration(
                physical_index=i,
                assay_index=None,
                enabled=i not in set(int(v) for v in ignored_physical_indices),
                roi_xywh=[int(x), int(y), int(w), int(h)],
                top_point_px=top_point,
                baseline_point_px=base_point,
                quad_points_px=ordered_quad,
                tube_height_mm=None if tube_height_mm is None else float(tube_height_mm),
                tube_width_mm=None if tube_width_mm is None else float(tube_width_mm),
                label=f"Vial {i}",
            )
        )

    calibration = build_assay_calibration(
        background_bgr=bg,
        vials=vials,
        background_path=str(Path(background_path).resolve()),
        editor_mode="opencv_guided_bounds" if expected_count > 0 else "opencv_roi",
        editor_meta={
            "source": "guided_bounds" if expected_count > 0 else "cv2.selectROIs",
            "geometry": "mixed_bounds" if any(shape.get("quad_points") for shape in guided_shapes) else "rectangles",
        },
    )
    save_assay_calibration(output_json, calibration)
    return calibration


def validate_background_shape(background_bgr: np.ndarray, calibration: AssayCalibration) -> None:
    bg_shape = [int(background_bgr.shape[0]), int(background_bgr.shape[1])]
    if bg_shape != [int(calibration.image_shape_hw[0]), int(calibration.image_shape_hw[1])]:
        raise ValueError(
            "Background and calibration dimensions do not match. "
            f"Calibration expects HxW={calibration.image_shape_hw}, but the background is HxW={bg_shape}. "
            "Use a background captured at the calibration resolution, or reload and resave the calibration on the current background."
        )


def _vial_polygon_mask(
    vial: VialCalibration,
    offset_xy: Tuple[int, int],
    shape_hw: Tuple[int, int],
    inner_margin_px: int,
) -> Optional[np.ndarray]:
    quad_points = _vial_quad_points(vial)
    if quad_points is None:
        return None
    mask = np.zeros(shape_hw, dtype=np.uint8)
    ox, oy = offset_xy
    pts = []
    max_x = max(0, int(shape_hw[1]) - 1)
    max_y = max(0, int(shape_hw[0]) - 1)
    for px, py in quad_points:
        pts.append([_clamp(int(px - ox), 0, max_x), _clamp(int(py - oy), 0, max_y)])
    pts_arr = np.asarray(pts, dtype=np.int32)
    cv2.fillConvexPoly(mask, pts_arr, 255)
    shrink_px = max(0, int(round(float(inner_margin_px) * 0.35)))
    if shrink_px > 0:
        k = max(3, (shrink_px * 2) + 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.erode(mask, kernel, iterations=1)
    return mask


def _apply_vial_bounds_mask(
    mask: np.ndarray,
    vial: VialCalibration,
    offset_xy: Tuple[int, int],
    inner_margin_px: int,
) -> np.ndarray:
    poly_mask = _vial_polygon_mask(vial, offset_xy=offset_xy, shape_hw=mask.shape[:2], inner_margin_px=inner_margin_px)
    if poly_mask is not None:
        return cv2.bitwise_and(mask, poly_mask)
    clipped = mask.copy()
    if inner_margin_px > 0:
        clipped[:, :inner_margin_px] = 0
        clipped[:, -inner_margin_px:] = 0
    return clipped


def _position_in_vial(vial: VialCalibration, x_px: float, y_px: float) -> Dict[str, Optional[float]]:
    quad_points = _vial_quad_points(vial)
    if quad_points is not None:
        tl, tr, br, bl = quad_points
        left_x = _line_x_at_y(tl, bl, y_px)
        right_x = _line_x_at_y(tr, br, y_px)
        top_y = _line_y_at_x(tl, tr, x_px)
        bottom_y = _line_y_at_x(bl, br, x_px)
        if right_x < left_x:
            left_x, right_x = right_x, left_x
        if bottom_y < top_y:
            top_y, bottom_y = bottom_y, top_y
        width_px = max(1.0, float(right_x - left_x))
        height_px = max(1.0, float(bottom_y - top_y))
        x_from_left_px = float(x_px) - float(left_x)
        y_from_base_px = float(bottom_y) - float(y_px)
    else:
        roi_x, _, _, _ = vial.roi
        width_px = max(1.0, float(vial.width_px))
        height_px = max(1.0, float(vial.height_px))
        x_from_left_px = float(x_px) - float(roi_x)
        y_from_base_px = float(vial.baseline_y) - float(y_px)
    return {
        "x_from_left_px": float(x_from_left_px),
        "x_from_left_mm": None if vial.tube_width_mm is None else float(x_from_left_px * float(vial.tube_width_mm) / width_px),
        "y_from_base_px": float(y_from_base_px),
        "y_from_base_mm": None if vial.tube_height_mm is None else float(y_from_base_px * float(vial.tube_height_mm) / height_px),
        "relative_x": float(x_from_left_px / width_px),
        "relative_height": float(y_from_base_px / height_px),
    }


def _component_detections_from_mask(
    mask: np.ndarray,
    offset_xy: Tuple[int, int],
    vial: VialCalibration,
    frame_index: int,
    time_s: float,
    min_area: int,
    max_area: int,
    threshold_used: float,
) -> List[Detection]:
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    out: List[Detection] = []
    ox, oy = offset_xy

    for i in range(1, int(count)):
        x, y, w, h, area = [int(v) for v in stats[i].tolist()]
        if area < int(min_area):
            continue
        if area > int(max_area):
            # Try to salvage very large blobs by ignoring highly merged components.
            continue
        cx, cy = centroids[i]
        gx = float(ox + cx)
        gy = float(oy + cy)
        pos = _position_in_vial(vial, gx, gy)

        out.append(
            Detection(
                physical_vial_index=int(vial.physical_index),
                assay_tube_index=int(vial.assay_index or 0),
                bbox_xywh=[int(ox + x), int(oy + y), int(w), int(h)],
                center_xy_px=[gx, gy],
                area_px=int(area),
                frame_index=int(frame_index),
                time_s=float(time_s),
                x_from_left_px=float(pos["x_from_left_px"]),
                x_from_left_mm=None if pos["x_from_left_mm"] is None else float(pos["x_from_left_mm"]),
                y_from_base_px=float(pos["y_from_base_px"]),
                y_from_base_mm=None if pos["y_from_base_mm"] is None else float(pos["y_from_base_mm"]),
                distance_from_base_px=float(pos["y_from_base_px"]),
                distance_from_base_mm=None if pos["y_from_base_mm"] is None else float(pos["y_from_base_mm"]),
                relative_x=float(pos["relative_x"]),
                relative_height=float(pos["relative_height"]),
                threshold_used=float(threshold_used),
            )
        )
    return out


def _align_assay_frame_to_background(
    bg_gray: np.ndarray,
    frame_gray: np.ndarray,
    frame_color: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, Optional[np.ndarray], np.ndarray]:
    # Assay runs use a fixed overhead camera, so a lighter ECC pass is enough and
    # avoids the long stalls that the shared high-iteration aligner can cause.
    small_bg = cv2.resize(bg_gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    small_fr = cv2.resize(frame_gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)

    warp_small = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-4)

    try:
        cv2.findTransformECC(
            small_bg.astype(np.float32) / 255.0,
            small_fr.astype(np.float32) / 255.0,
            warp_small,
            cv2.MOTION_EUCLIDEAN,
            criteria,
            None,
            3,
        )
    except cv2.error:
        warp_small = np.eye(2, 3, dtype=np.float32)

    scale_x = float(bg_gray.shape[1]) / max(1.0, float(small_bg.shape[1]))
    scale_y = float(bg_gray.shape[0]) / max(1.0, float(small_bg.shape[0]))
    warp_full = warp_small.copy()
    warp_full[0, 2] *= scale_x
    warp_full[1, 2] *= scale_y

    aligned_gray = cv2.warpAffine(
        frame_gray,
        warp_full,
        (bg_gray.shape[1], bg_gray.shape[0]),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
    )

    aligned_color = None
    if frame_color is not None:
        aligned_color = cv2.warpAffine(
            frame_color,
            warp_full,
            (bg_gray.shape[1], bg_gray.shape[0]),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        )

    return aligned_gray, aligned_color, warp_full


def detect_assay_frame(
    background_bgr: np.ndarray,
    frame_bgr: np.ndarray,
    calibration: AssayCalibration,
    frame_index: int,
    time_s: float,
    min_area: int = 10,
    max_area: int = 250,
    min_threshold: float = 16.0,
    inner_margin_px: int = 8,
    no_align: bool = False,
) -> Tuple[List[Detection], np.ndarray, np.ndarray]:
    validate_background_shape(background_bgr, calibration)

    bg_gray = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2GRAY)
    frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

    if no_align:
        aligned_gray = frame_gray
        aligned_color = frame_bgr
    else:
        aligned_gray, aligned_color, _ = _align_assay_frame_to_background(bg_gray, frame_gray, frame_bgr)

    assert aligned_color is not None
    full_mask = np.zeros_like(bg_gray, dtype=np.uint8)
    detections: List[Detection] = []

    for vial in calibration.enabled_vials:
        x, y, w, h = vial.roi
        x0 = int(x + inner_margin_px)
        x1 = int(x + w - inner_margin_px)
        y0 = int(y)
        y1 = int(y + h)
        if x1 <= x0 + 4 or y1 <= y0 + 4:
            continue

        roi_bg = bg_gray[y0:y1, x0:x1]
        roi_fr = aligned_gray[y0:y1, x0:x1]

        score_dark = cv2.subtract(roi_bg, roi_fr)
        score_dark = cv2.GaussianBlur(score_dark, (5, 5), 0)

        # Blend in a black-hat channel to better emphasize tiny dark flies on a
        # bright assay background.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        bh_fr = cv2.morphologyEx(roi_fr, cv2.MORPH_BLACKHAT, kernel)
        bh_bg = cv2.morphologyEx(roi_bg, cv2.MORPH_BLACKHAT, kernel)
        score = cv2.max(score_dark, cv2.subtract(bh_fr, bh_bg))

        otsu_threshold, _ = cv2.threshold(score, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        threshold_used = float(max(min_threshold, otsu_threshold))
        _, mask = cv2.threshold(score, threshold_used, 255, cv2.THRESH_BINARY)

        # Keep detections inside the calibrated vial shape so slanted walls and
        # extra background outside the assay do not create blobs.
        mask = _apply_vial_bounds_mask(mask, vial=vial, offset_xy=(x0, y0), inner_margin_px=inner_margin_px)

        k2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
        k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k3)

        vial_dets = _component_detections_from_mask(
            mask=mask,
            offset_xy=(x0, y0),
            vial=vial,
            frame_index=frame_index,
            time_s=time_s,
            min_area=min_area,
            max_area=max_area,
            threshold_used=threshold_used,
        )
        if not vial_dets:
            relaxed_threshold = float(max(8.0, threshold_used * 0.82))
            if relaxed_threshold + 0.5 < threshold_used:
                _, relaxed_mask = cv2.threshold(score, relaxed_threshold, 255, cv2.THRESH_BINARY)
                relaxed_mask = _apply_vial_bounds_mask(relaxed_mask, vial=vial, offset_xy=(x0, y0), inner_margin_px=inner_margin_px)
                relaxed_mask = cv2.morphologyEx(relaxed_mask, cv2.MORPH_CLOSE, k3)
                relaxed_dets = _component_detections_from_mask(
                    mask=relaxed_mask,
                    offset_xy=(x0, y0),
                    vial=vial,
                    frame_index=frame_index,
                    time_s=time_s,
                    min_area=min_area,
                    max_area=max_area,
                    threshold_used=relaxed_threshold,
                )
                if relaxed_dets:
                    mask = relaxed_mask
                    vial_dets = relaxed_dets

        full_mask[y0:y1, x0:x1] = np.maximum(full_mask[y0:y1, x0:x1], mask)
        detections.extend(vial_dets)

    return detections, full_mask, aligned_color


def _greedy_match(cost_matrix: np.ndarray, max_cost: float) -> List[Tuple[int, int]]:
    matches: List[Tuple[int, int]] = []
    if cost_matrix.size == 0:
        return matches

    used_rows: set[int] = set()
    used_cols: set[int] = set()

    flat: List[Tuple[float, int, int]] = []
    rows, cols = cost_matrix.shape
    for r in range(rows):
        for c in range(cols):
            flat.append((float(cost_matrix[r, c]), r, c))
    flat.sort(key=lambda x: x[0])

    for cost, r, c in flat:
        if cost > max_cost:
            break
        if r in used_rows or c in used_cols:
            continue
        matches.append((r, c))
        used_rows.add(r)
        used_cols.add(c)
    return matches


def _match_tracks_to_detections(
    tracks: Sequence[Track],
    detections: Sequence[Detection],
    dt: float,
    x_search_px: float = 18.0,
    y_search_px: float = 48.0,
    max_cost: float = 1.35,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    if not tracks or not detections:
        return [], list(range(len(tracks))), list(range(len(detections)))

    cost = np.full((len(tracks), len(detections)), fill_value=1e6, dtype=np.float32)
    for ti, track in enumerate(tracks):
        px, py = track.predict(dt)
        missed_factor = min(6.0, float(max(0, track.missed_frames)))
        x_window = float(x_search_px) * (1.0 + 0.30 * missed_factor)
        y_window = float(y_search_px) * (1.0 + 0.40 * missed_factor)
        area_ref = None
        if track.history:
            last = track.history[-1]
            bw = last.get("bbox_w")
            bh = last.get("bbox_h")
            if bw is not None and bh is not None:
                area_ref = max(1.0, float(bw) * float(bh))
        for di, det in enumerate(detections):
            dx = abs(px - float(det.center_xy_px[0])) / max(1.0, x_window)
            dy = abs(py - float(det.center_xy_px[1])) / max(1.0, y_window)
            area_bias = 0.0 if area_ref is None else abs(float(det.area_px) - area_ref) / max(45.0, area_ref)
            cost[ti, di] = float(math.sqrt(dx * dx + dy * dy) + 0.08 * area_bias)

    if linear_sum_assignment is not None:
        row_ind, col_ind = linear_sum_assignment(cost)
        candidate_matches = [(int(r), int(c)) for r, c in zip(row_ind.tolist(), col_ind.tolist()) if cost[r, c] <= max_cost]
    else:
        candidate_matches = _greedy_match(cost, max_cost=max_cost)

    matched_track_ids = {r for r, _ in candidate_matches}
    matched_det_ids = {c for _, c in candidate_matches}
    unmatched_tracks = [i for i in range(len(tracks)) if i not in matched_track_ids]
    unmatched_dets = [i for i in range(len(detections)) if i not in matched_det_ids]
    return candidate_matches, unmatched_tracks, unmatched_dets


def _match_recently_lost_tracks_to_detections(
    tracks: Sequence[Track],
    detections: Sequence[Detection],
    time_s: float,
    x_search_px: float = 24.0,
    y_search_px: float = 60.0,
    max_cost: float = 1.8,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    if not tracks or not detections:
        return [], list(range(len(tracks))), list(range(len(detections)))

    cost = np.full((len(tracks), len(detections)), fill_value=1e6, dtype=np.float32)
    for ti, track in enumerate(tracks):
        last_time_s = float(track.history[-1]["time_s"]) if track.history else float(time_s)
        dt_track = max(1e-3, float(time_s - last_time_s))
        px, py = track.predict(dt_track)
        missed_factor = min(10.0, float(max(1, track.missed_frames)))
        x_window = float(x_search_px) * (1.0 + 0.40 * missed_factor)
        y_window = float(y_search_px) * (1.0 + 0.55 * missed_factor)
        area_ref = None
        if track.history:
            last = track.history[-1]
            bw = last.get("bbox_w")
            bh = last.get("bbox_h")
            if bw is not None and bh is not None:
                area_ref = max(1.0, float(bw) * float(bh))
        time_penalty = min(0.30, 0.04 * missed_factor)
        for di, det in enumerate(detections):
            dx = abs(px - float(det.center_xy_px[0])) / max(1.0, x_window)
            dy = abs(py - float(det.center_xy_px[1])) / max(1.0, y_window)
            area_bias = 0.0 if area_ref is None else abs(float(det.area_px) - area_ref) / max(45.0, area_ref)
            cost[ti, di] = float(math.sqrt(dx * dx + dy * dy) + 0.10 * area_bias + time_penalty)

    if linear_sum_assignment is not None:
        row_ind, col_ind = linear_sum_assignment(cost)
        candidate_matches = [(int(r), int(c)) for r, c in zip(row_ind.tolist(), col_ind.tolist()) if cost[r, c] <= max_cost]
    else:
        candidate_matches = _greedy_match(cost, max_cost=max_cost)

    matched_track_ids = {r for r, _ in candidate_matches}
    matched_det_ids = {c for _, c in candidate_matches}
    unmatched_tracks = [i for i in range(len(tracks)) if i not in matched_track_ids]
    unmatched_dets = [i for i in range(len(detections)) if i not in matched_det_ids]
    return candidate_matches, unmatched_tracks, unmatched_dets


class MultiVialTracker:
    def __init__(self, calibration: AssayCalibration, memory_frames: int = 8, max_flies_per_vial: int = 10) -> None:
        self.calibration = calibration
        self.memory_frames = int(memory_frames)
        self.reacquire_frames = max(12, int(self.memory_frames) * 3)
        self.max_flies_per_vial = max(1, int(max_flies_per_vial))
        self.active_tracks: Dict[int, List[Track]] = {v.physical_index: [] for v in calibration.enabled_vials}
        self.recently_lost_tracks: Dict[int, List[Track]] = {v.physical_index: [] for v in calibration.enabled_vials}
        self.completed_tracks: List[Track] = []
        self.next_internal_id = 1
        self.next_display_id: Dict[int, int] = {
            v.physical_index: 1 for v in calibration.enabled_vials
        }

    def _finalize_track(self, track: Track) -> None:
        track.active = False
        track.retired_frame_index = None
        track.retired_time_s = None
        self.completed_tracks.append(track)

    def _retire_track(self, track: Track, frame_index: int, time_s: float) -> None:
        track.active = False
        track.retired_frame_index = int(frame_index)
        track.retired_time_s = float(time_s)
        self.recently_lost_tracks.setdefault(int(track.physical_vial_index), []).append(track)

    def _revive_track(self, track: Track, det: Detection, time_s: float) -> None:
        last_time_s = float(track.history[-1]["time_s"]) if track.history else float(time_s)
        track.active = True
        track.retired_frame_index = None
        track.retired_time_s = None
        track.add_observation(det, dt=max(1e-3, float(time_s - last_time_s)))

    def _purge_stale_recent_tracks(self, vial_index: int, frame_index: int) -> None:
        recent_tracks = self.recently_lost_tracks.setdefault(vial_index, [])
        survivors: List[Track] = []
        for track in recent_tracks:
            retired_at = int(track.retired_frame_index) if track.retired_frame_index is not None else int(frame_index)
            if int(frame_index - retired_at) > self.reacquire_frames:
                self._finalize_track(track)
            else:
                survivors.append(track)
        self.recently_lost_tracks[vial_index] = survivors

    def update(self, frame_index: int, time_s: float, detections: Sequence[Detection], dt: float) -> None:
        by_vial: Dict[int, List[Detection]] = {}
        for det in detections:
            by_vial.setdefault(int(det.physical_vial_index), []).append(det)

        for vial in self.calibration.enabled_vials:
            vial_index = int(vial.physical_index)
            vial_tracks = self.active_tracks.setdefault(vial_index, [])
            self._purge_stale_recent_tracks(vial_index, frame_index)
            recent_tracks = self.recently_lost_tracks.setdefault(vial_index, [])
            vial_detections = by_vial.get(vial_index, [])
            vial_detections.sort(key=lambda d: float(d.center_xy_px[1]), reverse=True)

            matches, unmatched_tracks, unmatched_dets = _match_tracks_to_detections(vial_tracks, vial_detections, dt=dt)

            for ti, di in matches:
                vial_tracks[ti].add_observation(vial_detections[di], dt=max(dt, 1e-6))

            for ti in unmatched_tracks:
                track = vial_tracks[ti]
                pred_x, pred_y = track.predict(dt=max(dt, 1e-6))
                track.add_missed_prediction(frame_index=frame_index, time_s=time_s, pred_x=pred_x, pred_y=pred_y, vial=vial)

            available_det_ids = list(unmatched_dets)
            if recent_tracks and available_det_ids:
                available_detections = [vial_detections[di] for di in available_det_ids]
                lost_matches, _unmatched_recent, unmatched_recent_dets = _match_recently_lost_tracks_to_detections(
                    recent_tracks,
                    available_detections,
                    time_s=time_s,
                )
                if lost_matches:
                    matched_recent_ids = {ti for ti, _ in lost_matches}
                    revived_tracks: List[Track] = []
                    for ti, di in lost_matches:
                        track = recent_tracks[ti]
                        det = available_detections[di]
                        self._revive_track(track, det, time_s=time_s)
                        revived_tracks.append(track)
                    vial_tracks.extend(revived_tracks)
                    self.recently_lost_tracks[vial_index] = [
                        track for idx, track in enumerate(recent_tracks) if idx not in matched_recent_ids
                    ]
                    recent_tracks = self.recently_lost_tracks[vial_index]
                available_det_ids = [available_det_ids[idx] for idx in unmatched_recent_dets]

            # Create new tracks for unmatched detections, bottom-to-top order in the vial.
            new_det_ids = sorted(available_det_ids, key=lambda idx: float(vial_detections[idx].center_xy_px[1]), reverse=True)
            unique_slots_left = max(0, int(self.max_flies_per_vial) - (int(self.next_display_id[vial_index]) - 1))
            held_slots = len(vial_tracks) + len(recent_tracks)
            remaining_capacity = min(max(0, int(self.max_flies_per_vial) - held_slots), unique_slots_left)
            if remaining_capacity < len(new_det_ids):
                new_det_ids = new_det_ids[:remaining_capacity]
            for di in new_det_ids:
                det = vial_detections[di]
                display_id = int(self.next_display_id[vial_index])
                self.next_display_id[vial_index] += 1
                track = Track(
                    internal_id=int(self.next_internal_id),
                    display_id=display_id,
                    physical_vial_index=vial_index,
                    assay_tube_index=int(det.assay_tube_index),
                    last_x=float(det.center_xy_px[0]),
                    last_y=float(det.center_xy_px[1]),
                )
                self.next_internal_id += 1
                track.add_observation(det, dt=max(dt, 1e-6))
                vial_tracks.append(track)

            survivors: List[Track] = []
            for track in vial_tracks:
                if track.missed_frames > self.memory_frames:
                    self._retire_track(track, frame_index=frame_index, time_s=time_s)
                else:
                    survivors.append(track)
            self.active_tracks[vial_index] = survivors

    def finish(self) -> None:
        for vial_tracks in self.active_tracks.values():
            for track in vial_tracks:
                self._finalize_track(track)
        for key in list(self.active_tracks.keys()):
            self.active_tracks[key] = []
        for vial_tracks in self.recently_lost_tracks.values():
            for track in vial_tracks:
                self._finalize_track(track)
        for key in list(self.recently_lost_tracks.keys()):
            self.recently_lost_tracks[key] = []

    def all_tracks(self) -> List[Track]:
        out = list(self.completed_tracks)
        for tracks in self.active_tracks.values():
            out.extend(tracks)
        for tracks in self.recently_lost_tracks.values():
            out.extend(tracks)
        return out

    def active_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for vial_tracks in self.active_tracks.values():
            for track in vial_tracks:
                if not track.history:
                    continue
                last = track.history[-1]
                rows.append(
                    {
                        "label": f"fly ({track.assay_tube_index},{track.display_id})",
                        "physical_vial_index": int(track.physical_vial_index),
                        "assay_tube_index": int(track.assay_tube_index),
                        "display_id": int(track.display_id),
                        "frame_index": int(last["frame_index"]),
                        "time_s": float(last["time_s"]),
                        "x_px": float(last["x_px"]),
                        "y_px": float(last["y_px"]),
                        "x_from_left_px": last.get("x_from_left_px"),
                        "x_from_left_mm": last.get("x_from_left_mm"),
                        "y_from_base_px": last.get("y_from_base_px"),
                        "y_from_base_mm": last.get("y_from_base_mm"),
                        "distance_from_base_px": last.get("distance_from_base_px"),
                        "distance_from_base_mm": last.get("distance_from_base_mm"),
                        "relative_x": last.get("relative_x"),
                        "relative_height": last.get("relative_height"),
                        "detected": bool(last["detected"]),
                    }
                )
        rows.sort(key=lambda r: (r["assay_tube_index"], r["display_id"]))
        return rows


def assay_mask_to_bgr(mask: np.ndarray, frame_bgr: Optional[np.ndarray] = None) -> np.ndarray:
    if frame_bgr is None:
        return cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    out = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    out[mask > 0] = (0, 220, 255)
    return out


def render_assay_calibration_overlay(
    frame_bgr: np.ndarray,
    calibration: AssayCalibration,
    selected_physical_index: Optional[int] = None,
    header: Optional[str] = None,
    show_reference_lines: bool = True,
    show_vial_labels: bool = True,
    fill_alpha: float = 0.08,
    outline_width: int = 2,
    header_scale: float = 0.54,
) -> np.ndarray:
    out = frame_bgr.copy()
    fill = out.copy()
    for vial in calibration.vials:
        region_fill = (255, 140, 60) if vial.enabled else (120, 120, 120)
        pts = _vial_polygon_points(vial)
        cv2.fillConvexPoly(fill, pts, region_fill)
    if fill_alpha > 0:
        cv2.addWeighted(fill, float(fill_alpha), out, max(0.0, 1.0 - float(fill_alpha)), 0, out)

    for vial in calibration.vials:
        x, y, w, h = vial.roi
        outline = (42, 186, 255) if vial.enabled else (140, 140, 140)
        if selected_physical_index is not None and int(vial.physical_index) == int(selected_physical_index):
            outline = (0, 240, 255)
        pts = _vial_polygon_points(vial)
        cv2.polylines(out, [pts.reshape((-1, 1, 2))], True, outline, max(1, int(outline_width)), cv2.LINE_AA)
        if show_reference_lines:
            quad_points = _vial_quad_points(vial)
            if quad_points is not None:
                cv2.line(out, tuple(quad_points[0]), tuple(quad_points[1]), (0, 220, 255), 2, cv2.LINE_AA)
                cv2.line(out, tuple(quad_points[3]), tuple(quad_points[2]), (80, 225, 120), 2, cv2.LINE_AA)
            else:
                cv2.line(out, (x, vial.top_y), (x + w, vial.top_y), (0, 220, 255), 2, cv2.LINE_AA)
                cv2.line(out, (x, vial.baseline_y), (x + w, vial.baseline_y), (80, 225, 120), 2, cv2.LINE_AA)
            cv2.circle(out, (int(vial.center_x), vial.top_y), 4, (0, 220, 255), -1)
            cv2.circle(out, (int(vial.center_x), vial.baseline_y), 4, (80, 225, 120), -1)

        tag = f"P{vial.physical_index}"
        if vial.enabled and vial.assay_index is not None:
            tag += f"  T{vial.assay_index}"
        else:
            tag += "  ignored"
        if vial.label:
            tag = f"{tag}  {vial.label}"

        if show_vial_labels:
            label_y = max(18, y - 8)
            cv2.putText(out, tag, (x + 4, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(out, tag, (x + 4, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.42, outline, 1, cv2.LINE_AA)
        if show_reference_lines:
            cv2.putText(out, "top", (x + 6, vial.top_y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 220, 255), 1, cv2.LINE_AA)
            cv2.putText(out, "0", (x + 6, min(y + h - 6, vial.baseline_y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (80, 225, 120), 1, cv2.LINE_AA)

    if header:
        cv2.putText(out, header, (12, 22), cv2.FONT_HERSHEY_SIMPLEX, float(header_scale), (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(out, header, (12, 22), cv2.FONT_HERSHEY_SIMPLEX, float(header_scale), (18, 18, 18), 1, cv2.LINE_AA)
    return out


def annotate_assay_frame(
    frame_bgr: np.ndarray,
    calibration: AssayCalibration,
    detections: Sequence[Detection],
    tracker: MultiVialTracker,
    frame_index: int,
    time_s: float,
    show_positions: bool = False,
) -> np.ndarray:
    rows = tracker.active_rows()
    detected_rows = [row for row in rows if bool(row.get("detected"))]
    header = f"f={frame_index}  t={time_s:0.1f}s  n={len(detected_rows)}"
    out = render_assay_calibration_overlay(
        frame_bgr,
        calibration,
        header=header,
        show_reference_lines=False,
        show_vial_labels=False,
        fill_alpha=0.0,
        outline_width=1,
        header_scale=0.45,
    )
    counts_by_physical: Dict[int, int] = {}
    for row in detected_rows:
        counts_by_physical[int(row["physical_vial_index"])] = counts_by_physical.get(int(row["physical_vial_index"]), 0) + 1

    for vial in calibration.enabled_vials:
        x, y, _w, h = vial.roi
        count = int(counts_by_physical.get(int(vial.physical_index), 0))
        count_text = f"{count}/{tracker.max_flies_per_vial}"
        count_y = min(y + h - 8, y + 14)
        cv2.putText(out, count_text, (x + 3, count_y), cv2.FONT_HERSHEY_SIMPLEX, 0.26, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(out, count_text, (x + 3, count_y), cv2.FONT_HERSHEY_SIMPLEX, 0.26, (35, 35, 35), 1, cv2.LINE_AA)

    for row in detected_rows:
        label = f"{int(row['assay_tube_index'])}:{int(row['display_id'])}"
        if show_positions:
            x_pos_mm = row.get("x_from_left_mm")
            y_pos_mm = row.get("y_from_base_mm")
            if x_pos_mm is not None and y_pos_mm is not None:
                label = f"{label} x{float(x_pos_mm):.1f} y{float(y_pos_mm):.1f}"
            elif row.get("x_from_left_px") is not None and row.get("y_from_base_px") is not None:
                label = f"{label} x{float(row['x_from_left_px']):.0f} y{float(row['y_from_base_px']):.0f}"
        x = int(round(row["x_px"]))
        y = int(round(row["y_px"]))
        matched_det: Optional[Detection] = None
        for det in detections:
            if int(det.physical_vial_index) == int(row["physical_vial_index"]) and \
               abs(float(det.center_xy_px[0]) - float(row["x_px"])) < 1.5 and \
               abs(float(det.center_xy_px[1]) - float(row["y_px"])) < 1.5:
                matched_det = det
                break

        if matched_det is not None:
            bx, by, bw, bh = [int(v) for v in matched_det.bbox_xywh]
            cv2.rectangle(out, (bx, by), (bx + bw, by + bh), (150, 225, 255), 1)
            cv2.circle(out, (x, y), 2, (150, 225, 255), 1)
        else:
            bx, by, bw, bh = int(x - 4), int(y - 4), 8, 8
            cv2.rectangle(out, (bx, by), (bx + bw, by + bh), (135, 205, 255), 1)
            cv2.circle(out, (x, y), 2, (135, 205, 255), 1)

        txt = label
        ty = by - 6 if by > 16 else by + bh + 14
        cv2.putText(out, txt, (bx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.24, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(out, txt, (bx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.24, (40, 40, 40), 1, cv2.LINE_AA)
    return out


def preview_assay_frame(
    background_bgr: np.ndarray,
    frame_bgr: np.ndarray,
    calibration: AssayCalibration,
    frame_index: int = 0,
    time_s: float = 0.0,
    min_area: int = 10,
    max_area: int = 250,
    min_threshold: float = 16.0,
    inner_margin_px: int = 8,
    no_align: bool = False,
    max_flies_per_vial: int = 10,
    show_positions: bool = False,
) -> Tuple[Dict[str, np.ndarray], List[Dict[str, Any]], Dict[str, Any]]:
    detections, mask, aligned_bgr = detect_assay_frame(
        background_bgr=background_bgr,
        frame_bgr=frame_bgr,
        calibration=calibration,
        frame_index=frame_index,
        time_s=time_s,
        min_area=min_area,
        max_area=max_area,
        min_threshold=min_threshold,
        inner_margin_px=inner_margin_px,
        no_align=no_align,
    )
    tracker = MultiVialTracker(calibration=calibration, memory_frames=1, max_flies_per_vial=max_flies_per_vial)
    tracker.update(frame_index=frame_index, time_s=time_s, detections=detections, dt=max(1e-3, 1.0))
    rows = tracker.active_rows()
    detected_rows = [row for row in rows if bool(row.get("detected"))]
    preview_images = {
        "raw": frame_bgr.copy(),
        "aligned": aligned_bgr.copy(),
        "calibration": render_assay_calibration_overlay(
            aligned_bgr,
            calibration,
            header="Calibration overlay",
            show_reference_lines=False,
            show_vial_labels=False,
            fill_alpha=0.0,
            outline_width=1,
            header_scale=0.46,
        ),
        "mask": render_assay_calibration_overlay(
            assay_mask_to_bgr(mask, frame_bgr=aligned_bgr),
            calibration,
            header=f"Mask view   detections={len(detections)}",
            show_reference_lines=False,
            show_vial_labels=False,
            fill_alpha=0.0,
            outline_width=1,
            header_scale=0.46,
        ),
        "annotated": annotate_assay_frame(
            aligned_bgr,
            calibration,
            detections,
            tracker,
            frame_index=frame_index,
            time_s=time_s,
            show_positions=show_positions,
        ),
    }
    meta = {
        "frame_index": int(frame_index),
        "time_s": float(time_s),
        "detection_count": int(len(detections)),
        "active_track_count": int(len(detected_rows)),
    }
    return preview_images, detected_rows, meta


def tracks_to_dataframe(tracks: Sequence[Track]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for track in tracks:
        for hist in track.history:
            row = dict(hist)
            row.update(
                {
                    "internal_track_id": int(track.internal_id),
                    "display_id": int(track.display_id),
                    "physical_vial_index": int(track.physical_vial_index),
                    "assay_tube_index": int(track.assay_tube_index),
                    "label": f"fly ({track.assay_tube_index},{track.display_id})",
                }
            )
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=[
            "internal_track_id", "display_id", "physical_vial_index", "assay_tube_index", "label",
            "frame_index", "time_s", "x_px", "y_px", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
            "x_from_left_px", "x_from_left_mm", "y_from_base_px", "y_from_base_mm",
            "distance_from_base_px", "distance_from_base_mm", "relative_x", "relative_height", "detected",
        ])
    df = pd.DataFrame(rows)
    return df.sort_values(["assay_tube_index", "display_id", "frame_index"]).reset_index(drop=True)


def detections_to_dataframe(detections: Sequence[Detection]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for det in detections:
        rows.append(
            {
                "physical_vial_index": int(det.physical_vial_index),
                "assay_tube_index": int(det.assay_tube_index),
                "bbox_x": int(det.bbox_xywh[0]),
                "bbox_y": int(det.bbox_xywh[1]),
                "bbox_w": int(det.bbox_xywh[2]),
                "bbox_h": int(det.bbox_xywh[3]),
                "center_x_px": float(det.center_xy_px[0]),
                "center_y_px": float(det.center_xy_px[1]),
                "area_px": int(det.area_px),
                "frame_index": int(det.frame_index),
                "time_s": float(det.time_s),
                "x_from_left_px": float(det.x_from_left_px),
                "x_from_left_mm": None if det.x_from_left_mm is None else float(det.x_from_left_mm),
                "y_from_base_px": float(det.y_from_base_px),
                "y_from_base_mm": None if det.y_from_base_mm is None else float(det.y_from_base_mm),
                "distance_from_base_px": float(det.distance_from_base_px),
                "distance_from_base_mm": None if det.distance_from_base_mm is None else float(det.distance_from_base_mm),
                "relative_x": float(det.relative_x),
                "relative_height": float(det.relative_height),
                "threshold_used": float(det.threshold_used),
            }
        )
    if not rows:
        return pd.DataFrame(columns=[
            "physical_vial_index", "assay_tube_index", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
            "center_x_px", "center_y_px", "area_px", "frame_index", "time_s",
            "x_from_left_px", "x_from_left_mm", "y_from_base_px", "y_from_base_mm",
            "distance_from_base_px", "distance_from_base_mm", "relative_x", "relative_height", "threshold_used",
        ])
    return pd.DataFrame(rows).sort_values(["frame_index", "assay_tube_index"]).reset_index(drop=True)


def _series_best_linear_segment(
    times: np.ndarray,
    values: np.ndarray,
    min_window_s: float = 2.0,
    max_window_s: float = 5.0,
    min_points: int = 5,
) -> Dict[str, Any]:
    if len(times) < min_points:
        return {
            "best_slope": None,
            "best_intercept": None,
            "best_r2": None,
            "best_p_value": None,
            "start_time_s": None,
            "end_time_s": None,
        }

    best: Dict[str, Any] = {
        "score": -1.0,
        "best_slope": None,
        "best_intercept": None,
        "best_r2": None,
        "best_p_value": None,
        "start_time_s": None,
        "end_time_s": None,
    }

    n = len(times)
    for i in range(n):
        for j in range(i + min_points - 1, n):
            duration = float(times[j] - times[i])
            if duration < min_window_s:
                continue
            if duration > max_window_s:
                break

            x = times[i:j + 1]
            y = values[i:j + 1]
            if np.allclose(y, y[0]):
                slope = 0.0
                intercept = float(y[0])
                r2 = 1.0
                p_value = 1.0
            elif linregress is not None:
                res = linregress(x, y)
                slope = float(res.slope)
                intercept = float(res.intercept)
                r2 = float(res.rvalue ** 2)
                p_value = float(res.pvalue)
            else:
                slope, intercept = np.polyfit(x, y, 1)
                yhat = slope * x + intercept
                ss_res = float(np.sum((y - yhat) ** 2))
                ss_tot = float(np.sum((y - np.mean(y)) ** 2))
                r2 = 1.0 if ss_tot <= 1e-12 else float(1.0 - ss_res / ss_tot)
                p_value = None

            score = max(0.0, slope) * max(0.0, r2)
            if score > best["score"]:
                best = {
                    "score": score,
                    "best_slope": float(slope),
                    "best_intercept": float(intercept),
                    "best_r2": float(r2),
                    "best_p_value": None if p_value is None else float(p_value),
                    "start_time_s": float(x[0]),
                    "end_time_s": float(x[-1]),
                }

    best.pop("score", None)
    return best


def summarize_tracks(tracks_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if tracks_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    fly_rows: List[Dict[str, Any]] = []
    vial_rows: List[Dict[str, Any]] = []

    for (assay_tube_index, display_id), grp in tracks_df.groupby(["assay_tube_index", "display_id"], sort=True):
        g = grp.sort_values("frame_index").copy()

        g["dist_interp_px"] = g["distance_from_base_px"].interpolate(limit_direction="both")
        if "distance_from_base_mm" in g.columns and g["distance_from_base_mm"].notna().any():
            g["dist_interp_mm"] = g["distance_from_base_mm"].interpolate(limit_direction="both")
            dist_col = "dist_interp_mm"
            slope_unit = "mm_s"
        else:
            g["dist_interp_mm"] = np.nan
            dist_col = "dist_interp_px"
            slope_unit = "px_s"

        dt = g["time_s"].diff().replace(0, np.nan)
        g["velocity"] = g[dist_col].diff() / dt
        g["speed"] = g["velocity"].abs()

        best = _series_best_linear_segment(g["time_s"].to_numpy(dtype=float), g[dist_col].to_numpy(dtype=float))

        fly_rows.append(
            {
                "assay_tube_index": int(assay_tube_index),
                "display_id": int(display_id),
                "label": f"fly ({int(assay_tube_index)},{int(display_id)})",
                "start_time_s": float(g["time_s"].iloc[0]),
                "end_time_s": float(g["time_s"].iloc[-1]),
                "duration_s": float(g["time_s"].iloc[-1] - g["time_s"].iloc[0]),
                "n_rows": int(len(g)),
                "n_detected_rows": int(g["detected"].fillna(False).sum()),
                "start_distance_px": float(g["dist_interp_px"].iloc[0]),
                "end_distance_px": float(g["dist_interp_px"].iloc[-1]),
                "net_displacement_px": float(g["dist_interp_px"].iloc[-1] - g["dist_interp_px"].iloc[0]),
                "max_distance_px": float(g["dist_interp_px"].max()),
                "mean_distance_px": float(g["dist_interp_px"].mean()),
                "total_path_px": float(g["dist_interp_px"].diff().abs().fillna(0).sum()),
                "mean_speed_px_s": float(g["speed"].mean(skipna=True)),
                "max_speed_px_s": float(g["speed"].max(skipna=True)),
                "mean_velocity_px_s": float(g["velocity"].mean(skipna=True)),
                "max_velocity_px_s": float(g["velocity"].max(skipna=True)),
                "best_linear_slope_px_s": None if best["best_slope"] is None or slope_unit != "px_s" else float(best["best_slope"]),
                "best_linear_slope_mm_s": None if best["best_slope"] is None or slope_unit != "mm_s" else float(best["best_slope"]),
                "best_linear_r2": best["best_r2"],
                "best_linear_p_value": best["best_p_value"],
                "best_linear_start_s": best["start_time_s"],
                "best_linear_end_s": best["end_time_s"],
            }
        )

        if g["dist_interp_mm"].notna().any():
            fly_rows[-1].update(
                {
                    "start_distance_mm": float(g["dist_interp_mm"].iloc[0]),
                    "end_distance_mm": float(g["dist_interp_mm"].iloc[-1]),
                    "net_displacement_mm": float(g["dist_interp_mm"].iloc[-1] - g["dist_interp_mm"].iloc[0]),
                    "max_distance_mm": float(g["dist_interp_mm"].max()),
                    "mean_distance_mm": float(g["dist_interp_mm"].mean()),
                    "total_path_mm": float(g["dist_interp_mm"].diff().abs().fillna(0).sum()),
                    "mean_speed_mm_s": float((g["dist_interp_mm"].diff() / dt).abs().mean(skipna=True)),
                    "max_speed_mm_s": float((g["dist_interp_mm"].diff() / dt).abs().max(skipna=True)),
                    "mean_velocity_mm_s": float((g["dist_interp_mm"].diff() / dt).mean(skipna=True)),
                    "max_velocity_mm_s": float((g["dist_interp_mm"].diff() / dt).max(skipna=True)),
                }
            )

    fly_df = pd.DataFrame(fly_rows).sort_values(["assay_tube_index", "display_id"]).reset_index(drop=True)

    for assay_tube_index, grp in tracks_df.groupby("assay_tube_index", sort=True):
        g = grp.copy()
        if g["distance_from_base_mm"].notna().any():
            pivot = g.pivot_table(index="time_s", values="distance_from_base_mm", aggfunc="mean")
            dist_col = "distance_from_base_mm"
            unit = "mm"
        else:
            pivot = g.pivot_table(index="time_s", values="distance_from_base_px", aggfunc="mean")
            dist_col = "distance_from_base_px"
            unit = "px"
        pivot = pivot.sort_index().reset_index()
        best = _series_best_linear_segment(
            pivot["time_s"].to_numpy(dtype=float),
            pivot[dist_col].to_numpy(dtype=float),
        )
        slope = None if best["best_slope"] is None else float(best["best_slope"])
        if best.get("best_p_value") is not None and float(best["best_p_value"]) >= 0.05:
            slope = 0.0
        if slope is not None and slope < 0:
            slope = 0.0

        row = {
            "assay_tube_index": int(assay_tube_index),
            "n_tracks": int(g["display_id"].nunique()),
            "time_start_s": float(pivot["time_s"].min()),
            "time_end_s": float(pivot["time_s"].max()),
            "mean_curve_best_linear_r2": best["best_r2"],
            "mean_curve_best_linear_p_value": best["best_p_value"],
            "mean_curve_best_linear_start_s": best["start_time_s"],
            "mean_curve_best_linear_end_s": best["end_time_s"],
        }
        if unit == "mm":
            row["cohort_velocity_mm_s"] = slope
            row["mean_height_max_mm"] = float(pivot[dist_col].max())
            row["mean_height_final_mm"] = float(pivot[dist_col].iloc[-1])
        else:
            row["cohort_velocity_px_s"] = slope
            row["mean_height_max_px"] = float(pivot[dist_col].max())
            row["mean_height_final_px"] = float(pivot[dist_col].iloc[-1])
        vial_rows.append(row)

    vial_df = pd.DataFrame(vial_rows).sort_values("assay_tube_index").reset_index(drop=True)
    return fly_df, vial_df


def generate_graphs_and_pdf(
    output_dir: str | Path,
    tracks_df: pd.DataFrame,
    fly_summary_df: pd.DataFrame,
    vial_summary_df: pd.DataFrame,
    session_meta: Dict[str, Any],
) -> Dict[str, str]:
    out_dir = ensure_dir(output_dir)
    graphs_dir = ensure_dir(out_dir / "graphs")

    pdf_path = out_dir / "report.pdf"
    paths: Dict[str, str] = {"report_pdf": str(pdf_path)}

    with PdfPages(pdf_path) as pdf:
        # Cover page
        fig = plt.figure(figsize=(11.0, 8.5))
        fig.suptitle("Fruit fly assay report", fontsize=18)
        ax = fig.add_subplot(111)
        ax.axis("off")
        lines = [
            f"Session: {session_meta.get('session_name', '')}",
            f"Background: {session_meta.get('background_path', '')}",
            f"Calibration: {session_meta.get('calibration_path', '')}",
            f"Duration (s): {session_meta.get('seconds', '')}",
            f"Frames processed: {session_meta.get('frames_processed', '')}",
            f"Assay tubes tracked: {session_meta.get('tubes_tracked', '')}",
        ]
        ax.text(0.02, 0.95, "\n".join(lines), va="top", ha="left", fontsize=12)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        if not vial_summary_df.empty:
            fig = plt.figure(figsize=(11.0, 8.5))
            ax = fig.add_subplot(111)
            ax.axis("off")
            ax.set_title("Per-vial summary")
            table = ax.table(
                cellText=vial_summary_df.round(3).astype(str).values,
                colLabels=vial_summary_df.columns.tolist(),
                cellLoc="center",
                loc="center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(8)
            table.scale(1, 1.2)
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

        for assay_tube_index, grp in tracks_df.groupby("assay_tube_index", sort=True):
            fig = plt.figure(figsize=(11.0, 8.5))
            ax = fig.add_subplot(111)
            ax.set_title(f"Tube {int(assay_tube_index)}: fly distance from baseline vs time")
            unit_col = "distance_from_base_mm" if grp["distance_from_base_mm"].notna().any() else "distance_from_base_px"
            ylabel = "Distance from baseline (mm)" if unit_col.endswith("_mm") else "Distance from baseline (px)"
            for (display_id), g in grp.groupby("display_id", sort=True):
                g = g.sort_values("frame_index").copy()
                series = g[unit_col].interpolate(limit_direction="both")
                ax.plot(g["time_s"], series, label=f"fly ({int(assay_tube_index)},{int(display_id)})")
            ax.set_xlabel("Time (s)")
            ax.set_ylabel(ylabel)
            ax.legend(loc="best", fontsize=8)
            tube_path = graphs_dir / f"tube_{int(assay_tube_index)}_overlay.png"
            fig.savefig(tube_path, dpi=160, bbox_inches="tight")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

        for _, row in fly_summary_df.iterrows():
            assay_tube_index = int(row["assay_tube_index"])
            display_id = int(row["display_id"])
            label = str(row["label"])
            g = tracks_df[(tracks_df["assay_tube_index"] == assay_tube_index) & (tracks_df["display_id"] == display_id)].copy()
            if g.empty:
                continue
            g = g.sort_values("frame_index")
            unit_col = "distance_from_base_mm" if g["distance_from_base_mm"].notna().any() else "distance_from_base_px"
            ylabel = "Distance from baseline (mm)" if unit_col.endswith("_mm") else "Distance from baseline (px)"
            series = g[unit_col].interpolate(limit_direction="both")

            fig = plt.figure(figsize=(10.0, 4.8))
            ax = fig.add_subplot(111)
            ax.set_title(label)
            ax.plot(g["time_s"], series)
            ax.set_xlabel("Time (s)")
            ax.set_ylabel(ylabel)

            start_s = row.get("best_linear_start_s")
            end_s = row.get("best_linear_end_s")
            if pd.notna(start_s) and pd.notna(end_s):
                mask = (g["time_s"] >= float(start_s)) & (g["time_s"] <= float(end_s))
                ax.plot(g.loc[mask, "time_s"], series.loc[mask], linewidth=3)

            fly_path = graphs_dir / f"tube_{assay_tube_index}_fly_{display_id}.png"
            fig.savefig(fly_path, dpi=160, bbox_inches="tight")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

        if not fly_summary_df.empty:
            value_col = "max_distance_mm" if "max_distance_mm" in fly_summary_df.columns else "max_distance_px"
            ylabel = "Max height (mm)" if value_col.endswith("_mm") else "Max height (px)"
            fig = plt.figure(figsize=(11.0, 5.5))
            ax = fig.add_subplot(111)
            ax.set_title("Per-fly maximum height")
            labels = fly_summary_df["label"].tolist()
            ax.bar(range(len(fly_summary_df)), fly_summary_df[value_col].fillna(0.0))
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
            ax.set_ylabel(ylabel)
            summary_bar = graphs_dir / "per_fly_max_height.png"
            fig.savefig(summary_bar, dpi=160, bbox_inches="tight")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    return paths


def _sql_safe_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    safe = df.copy()
    for col in safe.columns:
        safe[col] = safe[col].map(
            lambda v: json.dumps(v) if isinstance(v, (list, tuple, dict)) else v
        )
    return safe


def write_outputs(
    output_dir: str | Path,
    session_name: str,
    detections_df: pd.DataFrame,
    tracks_df: pd.DataFrame,
    fly_summary_df: pd.DataFrame,
    vial_summary_df: pd.DataFrame,
    session_meta: Dict[str, Any],
) -> Dict[str, str]:
    out_dir = ensure_dir(output_dir)
    detections_csv = out_dir / "detections.csv"
    tracks_csv = out_dir / "tracks.csv"
    fly_summary_csv = out_dir / "per_fly_summary.csv"
    vial_summary_csv = out_dir / "per_vial_summary.csv"
    session_json = out_dir / "session.json"
    db_path = out_dir / "results.sqlite"

    detections_df.to_csv(detections_csv, index=False)
    tracks_df.to_csv(tracks_csv, index=False)
    fly_summary_df.to_csv(fly_summary_csv, index=False)
    vial_summary_df.to_csv(vial_summary_csv, index=False)
    save_json(session_json, session_meta)

    with sqlite3.connect(db_path) as conn:
        _sql_safe_dataframe(detections_df).to_sql("detections", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(tracks_df).to_sql("tracks", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(fly_summary_df).to_sql("per_fly_summary", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(vial_summary_df).to_sql("per_vial_summary", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(pd.DataFrame([session_meta])).to_sql("session", conn, if_exists="replace", index=False)

    graph_paths = generate_graphs_and_pdf(
        output_dir=out_dir,
        tracks_df=tracks_df,
        fly_summary_df=fly_summary_df,
        vial_summary_df=vial_summary_df,
        session_meta=session_meta,
    )

    return {
        "detections_csv": str(detections_csv),
        "tracks_csv": str(tracks_csv),
        "per_fly_summary_csv": str(fly_summary_csv),
        "per_vial_summary_csv": str(vial_summary_csv),
        "session_json": str(session_json),
        "sqlite_db": str(db_path),
        **graph_paths,
    }


def run_assay_session(
    background_path: str | Path,
    calibration_path: str | Path,
    output_dir: str | Path,
    seconds: float = 30.0,
    fps: float = 5.0,
    camera_width: int = 1536,
    camera_height: int = 864,
    camera_backend: str = "opencv",
    camera_device: str | int = "auto:assay",
    camera_preferred_hint: str = "",
    camera_index: int = 0,
    min_area: int = 10,
    max_area: int = 250,
    min_threshold: float = 16.0,
    inner_margin_px: int = 8,
    max_flies_per_vial: int = 10,
    snapshot_interval_s: float = 1.0,
    no_align: bool = False,
    show_positions: bool = False,
    save_raw_video: bool = True,
    save_annotated_video: bool = True,
    preview_callback: Optional[Callable[[Dict[str, np.ndarray], List[Dict[str, Any]], Dict[str, Any]], None]] = None,
    stop_event = None,
) -> Dict[str, Any]:
    calibration = load_assay_calibration(calibration_path)
    background_bgr = cv2.imread(str(background_path), cv2.IMREAD_COLOR)
    if background_bgr is None:
        raise FileNotFoundError(f"Could not read background image: {background_path}")

    validate_background_shape(background_bgr, calibration)

    session_name = f"assay_{timestamp_slug()}"
    out_dir = ensure_dir(Path(output_dir) / session_name)
    snapshots_dir = ensure_dir(out_dir / "snapshots")

    tracker = MultiVialTracker(calibration=calibration, memory_frames=12, max_flies_per_vial=max_flies_per_vial)
    all_frame_detections: List[Detection] = []

    assay_camera_backend = normalize_assay_camera_backend(camera_backend)
    raw_writer = None
    annot_writer = None

    camera_index_in_use: Optional[int] = None
    with open_assay_camera(
        camera_backend=assay_camera_backend,
        width=int(camera_width),
        height=int(camera_height),
        fps=float(fps),
        camera_index=int(camera_index),
        camera_device=camera_device,
        preferred_hint=camera_preferred_hint,
        role="assay",
    ) as camera:
        camera_index_in_use = getattr(camera, "camera_index_in_use", None)
        first_frame = camera.read()
        h, w = first_frame.shape[:2]
        if [h, w] != calibration.image_shape_hw:
            raise ValueError(
                "Camera frame shape does not match calibration/background shape. "
                f"Camera produced {[h, w]}, calibration expects {calibration.image_shape_hw}."
            )

        if save_raw_video:
            raw_writer = safe_video_writer(out_dir / "raw_video.mp4", fps=float(fps), frame_size=(w, h))
        if save_annotated_video:
            annot_writer = safe_video_writer(out_dir / "annotated_video.mp4", fps=float(fps), frame_size=(w, h))

        start_time = time.monotonic()
        last_t = 0.0
        frame_index = 0
        next_snapshot_t = 0.0
        frame_interval_s = 1.0 / max(1.0, float(fps))

        while True:
            if stop_event is not None and bool(stop_event.is_set()):
                break

            if frame_index == 0:
                frame_bgr = first_frame
            else:
                frame_bgr = camera.read()

            t_now = time.monotonic() - start_time
            if t_now > float(seconds):
                break

            detections, mask, aligned_bgr = detect_assay_frame(
                background_bgr=background_bgr,
                frame_bgr=frame_bgr,
                calibration=calibration,
                frame_index=frame_index,
                time_s=t_now,
                min_area=min_area,
                max_area=max_area,
                min_threshold=min_threshold,
                inner_margin_px=inner_margin_px,
                no_align=no_align,
            )
            if stop_event is not None and bool(stop_event.is_set()):
                break
            all_frame_detections.extend(detections)

            dt = max(1e-3, float(t_now - last_t)) if frame_index > 0 else max(1e-3, 1.0 / max(1.0, float(fps)))
            tracker.update(frame_index=frame_index, time_s=t_now, detections=detections, dt=dt)
            annotated = annotate_assay_frame(
                aligned_bgr,
                calibration,
                detections,
                tracker,
                frame_index=frame_index,
                time_s=t_now,
                show_positions=show_positions,
            )
            active_rows = tracker.active_rows()
            detected_rows = [row for row in active_rows if bool(row.get("detected"))]
            preview_images = {
                "raw": frame_bgr.copy(),
                "aligned": aligned_bgr.copy(),
                "calibration": render_assay_calibration_overlay(
                    aligned_bgr,
                    calibration,
                    header="Calibration overlay",
                    show_reference_lines=False,
                    show_vial_labels=False,
                    fill_alpha=0.0,
                    outline_width=1,
                    header_scale=0.46,
                ),
                "mask": render_assay_calibration_overlay(
                    assay_mask_to_bgr(mask, frame_bgr=aligned_bgr),
                    calibration,
                    header=f"Mask view   detections={len(detections)}",
                    show_reference_lines=False,
                    show_vial_labels=False,
                    fill_alpha=0.0,
                    outline_width=1,
                    header_scale=0.46,
                ),
                "annotated": annotated,
            }

            if raw_writer is not None:
                raw_writer.write(frame_bgr)
            if annot_writer is not None:
                annot_writer.write(annotated)

            if t_now + 1e-9 >= next_snapshot_t:
                snap_path = snapshots_dir / f"snapshot_{int(round(next_snapshot_t)):02d}s.png"
                cv2.imwrite(str(snap_path), annotated)
                next_snapshot_t += float(snapshot_interval_s)

            if stop_event is not None and bool(stop_event.is_set()):
                break

            if preview_callback is not None:
                preview_callback(
                    preview_images,
                    detected_rows,
                    {
                        "frame_index": int(frame_index),
                        "time_s": float(t_now),
                        "session_name": session_name,
                        "output_dir": str(out_dir),
                        "detection_count": int(len(detections)),
                        "active_track_count": int(len(detected_rows)),
                    },
                )

            frame_index += 1
            last_t = t_now
            target_time = start_time + (float(frame_index) * frame_interval_s)
            while True:
                remaining = float(target_time - time.monotonic())
                if remaining <= 0.0:
                    break
                if stop_event is not None and bool(stop_event.is_set()):
                    break
                time.sleep(min(0.02, remaining))

        tracker.finish()

    if raw_writer is not None:
        raw_writer.release()
    if annot_writer is not None:
        annot_writer.release()

    detections_df = detections_to_dataframe(all_frame_detections)
    tracks_df = tracks_to_dataframe(tracker.completed_tracks)
    fly_summary_df, vial_summary_df = summarize_tracks(tracks_df)

    session_meta = {
        "session_name": session_name,
        "background_path": str(Path(background_path).resolve()),
        "calibration_path": str(Path(calibration_path).resolve()),
        "output_dir": str(out_dir.resolve()),
        "seconds": float(seconds),
        "fps": float(fps),
        "camera_width": int(camera_width),
        "camera_height": int(camera_height),
        "camera_backend": assay_camera_backend,
        "camera_device_requested": None if assay_camera_backend != "opencv" else str(camera_device),
        "camera_preferred_hint": str(camera_preferred_hint or ""),
        "camera_index_requested": int(camera_index),
        "camera_index_in_use": None if camera_index_in_use is None else int(camera_index_in_use),
        "frames_processed": int(frame_index),
        "tubes_tracked": int(len(calibration.enabled_vials)),
        "physical_vials_present": [int(v.physical_index) for v in calibration.vials],
        "ignored_physical_indices": [int(v) for v in calibration.ignored_physical_indices],
        "enabled_assay_tube_indices": [int(v.assay_index) for v in calibration.enabled_vials if v.assay_index is not None],
        "min_area": int(min_area),
        "max_area": int(max_area),
        "min_threshold": float(min_threshold),
        "inner_margin_px": int(inner_margin_px),
        "max_flies_per_vial": int(max_flies_per_vial),
        "no_align": bool(no_align),
        "show_positions": bool(show_positions),
    }

    output_paths = write_outputs(
        output_dir=out_dir,
        session_name=session_name,
        detections_df=detections_df,
        tracks_df=tracks_df,
        fly_summary_df=fly_summary_df,
        vial_summary_df=vial_summary_df,
        session_meta=session_meta,
    )

    session_meta.update(output_paths)
    save_json(Path(out_dir) / "session.json", session_meta)
    return session_meta


def capture_assay_background(
    output_path: str | Path,
    width: int = 1536,
    height: int = 864,
    fps: float = 10.0,
    frame_count: int = 25,
    camera_backend: str = "opencv",
    camera_device: str | int = "auto:assay",
    camera_preferred_hint: str = "",
    camera_index: int = 0,
) -> str:
    with open_assay_camera(
        camera_backend=camera_backend,
        width=int(width),
        height=int(height),
        fps=float(fps),
        camera_index=int(camera_index),
        camera_device=camera_device,
        preferred_hint=camera_preferred_hint,
        role="assay",
    ) as camera:
        bg = capture_background_image(camera, frame_count=frame_count, frame_sleep_s=0.03)
    ok = cv2.imwrite(str(output_path), bg)
    if not ok:
        raise IOError(f"Could not save background image to {output_path}")
    return str(Path(output_path).resolve())


def capture_assay_frame(
    width: int = 1536,
    height: int = 864,
    fps: float = 10.0,
    camera_backend: str = "opencv",
    camera_device: str | int = "auto:assay",
    camera_preferred_hint: str = "",
    camera_index: int = 0,
) -> np.ndarray:
    with open_assay_camera(
        camera_backend=camera_backend,
        width=int(width),
        height=int(height),
        fps=float(fps),
        camera_index=int(camera_index),
        camera_device=camera_device,
        preferred_hint=camera_preferred_hint,
        role="assay",
    ) as camera:
        return camera.read()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assay capture, calibration, tracking, and reporting.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bg = sub.add_parser("background", help="Capture a static assay background image with the selected assay camera.")
    p_bg.add_argument("-o", "--output", required=True, help="Output background image path")
    p_bg.add_argument("--width", type=int, default=1536)
    p_bg.add_argument("--height", type=int, default=864)
    p_bg.add_argument("--fps", type=float, default=10.0)
    p_bg.add_argument("--frames", type=int, default=25)
    p_bg.add_argument("--camera-backend", choices=["opencv", "pihq"], default="opencv")
    p_bg.add_argument("--camera-device", default="auto:assay")
    p_bg.add_argument("--camera-index", type=int, default=0)

    p_cal = sub.add_parser("calibrate", help="Calibrate vial ROIs, top, and baseline from a background image.")
    p_cal.add_argument("-b", "--background", required=True, help="Background image path")
    p_cal.add_argument("-o", "--output", required=True, help="Output calibration JSON path")
    p_cal.add_argument("--total-vials", type=int, default=0, help="Optional expected vial count. Leave at 0 to accept any count.")
    p_cal.add_argument("--ignore-leftmost", action="store_true", help="Start with physical vial 1 ignored")
    p_cal.add_argument("--tube-height-mm", type=float, default=None, help="Optional physical vial height to convert vertical px to mm")
    p_cal.add_argument("--tube-width-mm", type=float, default=None, help="Optional physical vial width to convert lateral px to mm")

    p_run = sub.add_parser("run", help="Run a 30-second assay with per-fly tracking and reporting.")
    p_run.add_argument("-b", "--background", required=True, help="Background image path")
    p_run.add_argument("-c", "--calibration", required=True, help="Calibration JSON path")
    p_run.add_argument("-o", "--output-dir", required=True, help="Session output root directory")
    p_run.add_argument("--seconds", type=float, default=30.0)
    p_run.add_argument("--fps", type=float, default=10.0)
    p_run.add_argument("--width", type=int, default=1536)
    p_run.add_argument("--height", type=int, default=864)
    p_run.add_argument("--camera-backend", choices=["opencv", "pihq"], default="opencv")
    p_run.add_argument("--camera-device", default="auto:assay")
    p_run.add_argument("--camera-index", type=int, default=0)
    p_run.add_argument("--min-area", type=int, default=10)
    p_run.add_argument("--max-area", type=int, default=250)
    p_run.add_argument("--min-threshold", type=float, default=16.0)
    p_run.add_argument("--inner-margin-px", type=int, default=8)
    p_run.add_argument("--max-flies-per-vial", type=int, default=10)
    p_run.add_argument("--snapshot-interval-s", type=float, default=1.0)
    p_run.add_argument("--no-align", action="store_true")
    p_run.add_argument("--no-raw-video", action="store_true")
    p_run.add_argument("--no-annotated-video", action="store_true")

    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.command == "background":
        out = capture_assay_background(
            output_path=args.output,
            width=args.width,
            height=args.height,
            fps=args.fps,
            frame_count=args.frames,
            camera_backend=args.camera_backend,
            camera_device=args.camera_device,
            camera_index=args.camera_index,
        )
        print(out)
        return

    if args.command == "calibrate":
        ignored = [1] if args.ignore_leftmost else []
        calibration = calibrate_assay_interactive(
            background_path=args.background,
            output_json=args.output,
            total_vials=args.total_vials,
            ignored_physical_indices=ignored,
            tube_height_mm=args.tube_height_mm,
            tube_width_mm=args.tube_width_mm,
        )
        print(save_json(args.output, calibration.to_dict()))
        return

    if args.command == "run":
        result = run_assay_session(
            background_path=args.background,
            calibration_path=args.calibration,
            output_dir=args.output_dir,
            seconds=args.seconds,
            fps=args.fps,
            camera_width=args.width,
            camera_height=args.height,
            camera_backend=args.camera_backend,
            camera_device=args.camera_device,
            camera_index=args.camera_index,
            min_area=args.min_area,
            max_area=args.max_area,
            min_threshold=args.min_threshold,
            inner_margin_px=args.inner_margin_px,
            max_flies_per_vial=args.max_flies_per_vial,
            snapshot_interval_s=args.snapshot_interval_s,
            no_align=args.no_align,
            save_raw_video=not args.no_raw_video,
            save_annotated_video=not args.no_annotated_video,
        )
        print(pd.Series(result).to_json(indent=2))
        return

    parser.error("Unknown command.")


if __name__ == "__main__":
    main()
