#!/usr/bin/env python3
"""
Tkinter dashboard integrating:
- Brio channel mode (background, calibrate, detect, live on/off)
- assay mode with an in-GUI calibration editor, richer preview modes,
  and a cleaner operator-focused layout.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk  # type: ignore

try:
    import config as project_config
except Exception:
    project_config = None

try:
    from .assay_tracking import (
        AssayCalibration,
        VialCalibration,
        build_assay_calibration,
        calibrate_assay_interactive,
        capture_assay_background as capture_assay_background_file,
        capture_assay_frame as capture_assay_frame_once,
        load_assay_calibration,
        preview_assay_frame,
        render_assay_calibration_overlay,
        run_assay_session,
    )
    from .brio_channel_cli import (
        calibrate_channel,
        capture_brio_background as capture_brio_background_file,
    )
    from .camera_sources import BrioCamera, BrioConfig
    from .fly_x_detector import process_fly_detection
    from .shared_utils import load_json, save_json
except ImportError:
    from assay_tracking import (
        AssayCalibration,
        VialCalibration,
        build_assay_calibration,
        calibrate_assay_interactive,
        capture_assay_background as capture_assay_background_file,
        capture_assay_frame as capture_assay_frame_once,
        load_assay_calibration,
        preview_assay_frame,
        render_assay_calibration_overlay,
        run_assay_session,
    )
    from brio_channel_cli import (
        calibrate_channel,
        capture_brio_background as capture_brio_background_file,
    )
    from camera_sources import BrioCamera, BrioConfig
    from fly_x_detector import process_fly_detection
    from shared_utils import load_json, save_json


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _read_bgr(path: str | Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    return image


def _looks_like_windows_path(raw_text: str) -> bool:
    return len(raw_text) >= 3 and raw_text[1] == ":" and raw_text[0].isalpha() and raw_text[2] in {"\\", "/"}


@dataclass
class EditorRegion:
    x: int
    y: int
    w: int
    h: int
    top_y: int
    baseline_y: int
    quad_points_px: Optional[List[List[int]]] = None
    enabled: bool = True
    label: str = ""
    group_id: Optional[str] = None

    @property
    def right(self) -> int:
        return int(self.x + self.w)

    @property
    def bottom(self) -> int:
        return int(self.y + self.h)

    @property
    def center_x(self) -> int:
        return int(self.x + self.w // 2)

    @classmethod
    def from_vial(cls, vial: VialCalibration) -> "EditorRegion":
        x, y, w, h = [int(v) for v in vial.roi_xywh]
        return cls(
            x=int(x),
            y=int(y),
            w=int(w),
            h=int(h),
            top_y=int(vial.top_y),
            baseline_y=int(vial.baseline_y),
            quad_points_px=None if getattr(vial, "quad_points_px", None) is None else [[int(px), int(py)] for px, py in vial.quad_points_px],
            enabled=bool(vial.enabled),
            label=str(vial.label or f"Vial {vial.physical_index}"),
            group_id=vial.group_id,
        )

    def to_vial(
        self,
        physical_index: int,
        tube_height_mm: Optional[float],
        tube_width_mm: Optional[float],
    ) -> VialCalibration:
        return VialCalibration(
            physical_index=int(physical_index),
            assay_index=None,
            enabled=bool(self.enabled),
            roi_xywh=[int(self.x), int(self.y), int(self.w), int(self.h)],
            top_point_px=[int(self.center_x), int(self.top_y)],
            baseline_point_px=[int(self.center_x), int(self.baseline_y)],
            quad_points_px=None if self.quad_points_px is None else [[int(px), int(py)] for px, py in self.quad_points_px],
            tube_height_mm=None if tube_height_mm is None else float(tube_height_mm),
            tube_width_mm=None if tube_width_mm is None else float(tube_width_mm),
            label=str(self.label or f"Vial {physical_index}"),
            group_id=self.group_id,
        )


class ImageCanvas(tk.Canvas):
    def __init__(self, master, background: str = "#0f172a", **kwargs):
        super().__init__(master, background=background, highlightthickness=0, **kwargs)
        self._source_bgr = None
        self._photo = None
        self._display_box = (0, 0, 1, 1)
        self._empty_text = "No image loaded"
        self.bind("<Configure>", lambda _event: self.render())

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self.render()

    def set_image_bgr(self, image_bgr) -> None:
        self._source_bgr = None if image_bgr is None else image_bgr.copy()
        self.render()

    def get_image_bgr(self):
        return None if self._source_bgr is None else self._source_bgr.copy()

    def image_shape(self) -> Optional[Tuple[int, int]]:
        if self._source_bgr is None:
            return None
        h, w = self._source_bgr.shape[:2]
        return int(h), int(w)

    def get_scale(self) -> float:
        h_w = self.image_shape()
        if h_w is None:
            return 1.0
        h, w = h_w
        cw = max(1, self.winfo_width())
        ch = max(1, self.winfo_height())
        return min(cw / max(1, w), ch / max(1, h))

    def image_to_canvas(self, x: int, y: int) -> Tuple[int, int]:
        ox, oy, _, _ = self._display_box
        scale = self.get_scale()
        return int(round(ox + x * scale)), int(round(oy + y * scale))

    def canvas_to_image(self, x: int, y: int) -> Optional[Tuple[int, int]]:
        if self._source_bgr is None:
            return None
        ox, oy, dw, dh = self._display_box
        if x < ox or y < oy or x > ox + dw or y > oy + dh:
            return None
        scale = self.get_scale()
        img_h, img_w = self._source_bgr.shape[:2]
        ix = int(round((x - ox) / max(scale, 1e-6)))
        iy = int(round((y - oy) / max(scale, 1e-6)))
        ix = max(0, min(img_w - 1, ix))
        iy = max(0, min(img_h - 1, iy))
        return ix, iy

    def draw_overlay(self) -> None:
        return

    def render(self) -> None:
        self.delete("all")
        if self._source_bgr is None:
            self.create_text(
                max(80, self.winfo_width() // 2),
                max(60, self.winfo_height() // 2),
                text=self._empty_text,
                fill="#b9c4d6",
                font=("DejaVu Sans", 12, "bold"),
            )
            return

        h, w = self._source_bgr.shape[:2]
        cw = max(1, self.winfo_width())
        ch = max(1, self.winfo_height())
        scale = min(cw / max(1, w), ch / max(1, h))
        dw = max(1, int(round(w * scale)))
        dh = max(1, int(round(h * scale)))
        ox = max(0, (cw - dw) // 2)
        oy = max(0, (ch - dh) // 2)
        self._display_box = (ox, oy, dw, dh)

        rgb = cv2.cvtColor(self._source_bgr, cv2.COLOR_BGR2RGB)
        if dw != w or dh != h:
            rgb = cv2.resize(rgb, (dw, dh), interpolation=cv2.INTER_AREA)
        image = Image.fromarray(rgb)
        self._photo = ImageTk.PhotoImage(image=image)
        self.create_image(ox, oy, image=self._photo, anchor="nw")
        self.draw_overlay()


class CalibrationCanvas(ImageCanvas):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.regions: List[EditorRegion] = []
        self.selected_index: Optional[int] = None
        self.show_regions = True
        self.editor_enabled = True
        self.tool = "select"
        self.split_count = 4
        self.undo_stack: List[Tuple[List[EditorRegion], Optional[int]]] = []
        self.redo_stack: List[Tuple[List[EditorRegion], Optional[int]]] = []
        self.on_change: Optional[Callable[[], None]] = None
        self.on_select: Optional[Callable[[Optional[int]], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None
        self._drag: Optional[Dict[str, Any]] = None
        self._group_counter = 1

        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_callbacks(
        self,
        on_change: Optional[Callable[[], None]] = None,
        on_select: Optional[Callable[[Optional[int]], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.on_change = on_change
        self.on_select = on_select
        self.on_status = on_status

    def _status(self, text: str) -> None:
        if self.on_status is not None:
            self.on_status(text)

    def _emit_change(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _emit_select(self) -> None:
        if self.on_select is not None:
            self.on_select(self.selected_index)

    def snapshot_state(self) -> Tuple[List[EditorRegion], Optional[int]]:
        return copy.deepcopy(self.regions), self.selected_index

    def load_state(self, state: Tuple[List[EditorRegion], Optional[int]], clear_history: bool = False) -> None:
        regions, selected = state
        self.regions = copy.deepcopy(regions)
        self.selected_index = selected if selected is not None and 0 <= selected < len(self.regions) else None
        if clear_history:
            self.undo_stack.clear()
            self.redo_stack.clear()
        self.render()
        self._emit_change()
        self._emit_select()

    def set_regions(self, regions: Sequence[EditorRegion], clear_history: bool = False) -> None:
        self.regions = copy.deepcopy(list(regions))
        self.selected_index = 0 if self.regions else None
        if clear_history:
            self.undo_stack.clear()
            self.redo_stack.clear()
        self.render()
        self._emit_change()
        self._emit_select()

    def get_regions(self) -> List[EditorRegion]:
        return copy.deepcopy(self.regions)

    def set_selected_index(self, index: Optional[int]) -> None:
        previous = self.selected_index
        if index is None or not self.regions:
            self.selected_index = None
        elif 0 <= int(index) < len(self.regions):
            self.selected_index = int(index)
        if self.selected_index == previous:
            return
        self.render()
        self._emit_select()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        if tool == "draw":
            self._status("Draw a rectangle on the viewer.")
        elif tool == "split":
            self._status(f"Draw a large assay area to split into {self.split_count} even vials.")
        else:
            self._status("Select a vial to move, resize, reorder, or edit references.")

    def set_split_count(self, count: int) -> None:
        self.split_count = max(2, int(count))
        if self.tool == "split":
            self._status(f"Draw a large assay area to split into {self.split_count} even vials.")

    def set_show_regions(self, enabled: bool) -> None:
        self.show_regions = bool(enabled)
        self.render()

    def set_editor_enabled(self, enabled: bool) -> None:
        self.editor_enabled = bool(enabled)
        self.configure(cursor="crosshair" if enabled and self.tool in {"draw", "split"} else "arrow")

    def _push_undo(self) -> None:
        self.undo_stack.append(self.snapshot_state())
        if len(self.undo_stack) > 80:
            self.undo_stack = self.undo_stack[-80:]
        self.redo_stack.clear()

    def undo(self) -> None:
        if not self.undo_stack:
            self._status("Nothing to undo.")
            return
        self.redo_stack.append(self.snapshot_state())
        self.load_state(self.undo_stack.pop())
        self._status("Undo.")

    def redo(self) -> None:
        if not self.redo_stack:
            self._status("Nothing to redo.")
            return
        self.undo_stack.append(self.snapshot_state())
        self.load_state(self.redo_stack.pop())
        self._status("Redo.")

    def clear_regions(self) -> None:
        if not self.regions:
            return
        self._push_undo()
        self.regions = []
        self.selected_index = None
        self.render()
        self._emit_change()
        self._emit_select()
        self._status("Cleared all assay regions.")

    def delete_selected(self) -> None:
        if self.selected_index is None:
            self._status("Select a region to delete.")
            return
        self._push_undo()
        del self.regions[self.selected_index]
        if self.selected_index >= len(self.regions):
            self.selected_index = len(self.regions) - 1 if self.regions else None
        self.render()
        self._emit_change()
        self._emit_select()
        self._status("Deleted selected region.")

    def duplicate_selected(self) -> None:
        if self.selected_index is None or self._source_bgr is None:
            self._status("Select a region to duplicate.")
            return
        self._push_undo()
        source = copy.deepcopy(self.regions[self.selected_index])
        img_h, img_w = self._source_bgr.shape[:2]
        source.group_id = None
        source.label = f"{source.label or 'Vial'} copy"
        source.x = min(max(0, img_w - source.w), source.x + source.w + 10)
        source.top_y = min(max(source.y, source.top_y), source.y + source.h - 2)
        source.baseline_y = min(max(source.top_y + 1, source.baseline_y), source.y + source.h - 1)
        self.regions.insert(self.selected_index + 1, source)
        self.selected_index += 1
        self.render()
        self._emit_change()
        self._emit_select()
        self._status("Duplicated selected region.")

    def toggle_selected_enabled(self) -> None:
        if self.selected_index is None:
            self._status("Select a region to mark active or ignored.")
            return
        self._push_undo()
        self.regions[self.selected_index].enabled = not self.regions[self.selected_index].enabled
        self.render()
        self._emit_change()
        self._emit_select()
        self._status("Updated selected region status.")

    def move_selected(self, delta: int) -> None:
        if self.selected_index is None:
            return
        target = self.selected_index + int(delta)
        if target < 0 or target >= len(self.regions):
            return
        self._push_undo()
        self.regions[self.selected_index], self.regions[target] = self.regions[target], self.regions[self.selected_index]
        self.selected_index = target
        self.render()
        self._emit_change()
        self._emit_select()
        self._status("Reordered assay regions.")

    def sort_left_to_right(self) -> None:
        if len(self.regions) < 2:
            return
        self._push_undo()
        selected = self.regions[self.selected_index] if self.selected_index is not None else None
        self.regions.sort(key=lambda r: (r.x, r.y))
        self.selected_index = self.regions.index(selected) if selected in self.regions else None
        self.render()
        self._emit_change()
        self._emit_select()
        self._status("Sorted regions left to right.")

    def apply_reference_style_from_selected(self) -> None:
        if self.selected_index is None or not self.regions:
            self._status("Select a region first.")
            return
        ref = self.regions[self.selected_index]
        if ref.h <= 1:
            return
        self._push_undo()
        top_ratio = (ref.top_y - ref.y) / max(1.0, float(ref.h))
        base_ratio = (ref.baseline_y - ref.y) / max(1.0, float(ref.h))
        for region in self.regions:
            region.top_y = int(round(region.y + top_ratio * region.h))
            region.baseline_y = int(round(region.y + base_ratio * region.h))
            region.top_y = max(region.y, min(region.bottom - 2, region.top_y))
            region.baseline_y = max(region.top_y + 1, min(region.bottom - 1, region.baseline_y))
        self.render()
        self._emit_change()
        self._status("Applied top and baseline style to all vials.")

    def cancel_current_action(self) -> None:
        if not self._drag:
            return
        before = self._drag.get("before_state")
        self._drag = None
        if before is not None:
            self.load_state(before)
        self._status("Cancelled current edit.")

    def draw_overlay(self) -> None:
        if not self.show_regions:
            if self._drag and self._drag.get("kind") in {"draw", "split"}:
                self._draw_draft_rect()
            return

        for idx, region in enumerate(self.regions):
            selected = idx == self.selected_index
            self._draw_region(region, idx, selected)

        for left_idx, right_idx in self._group_dividers():
            left = self.regions[left_idx]
            right = self.regions[right_idx]
            x = left.right
            y0 = max(left.y, right.y)
            y1 = min(left.bottom, right.bottom)
            cx, cy0 = self.image_to_canvas(x, y0)
            _, cy1 = self.image_to_canvas(x, y1)
            self.create_line(cx, cy0, cx, cy1, fill="#ffb454", width=2, dash=(4, 3))

        if self._drag and self._drag.get("kind") in {"draw", "split"}:
            self._draw_draft_rect()

    def _draw_region(self, region: EditorRegion, idx: int, selected: bool) -> None:
        x0, y0 = self.image_to_canvas(region.x, region.y)
        x1, y1 = self.image_to_canvas(region.right, region.bottom)
        outline = "#25a7ff" if region.enabled else "#8d99a8"
        fill = "#63aefb" if region.enabled else "#7f8a97"
        if selected:
            outline = "#ffd166"
        if region.quad_points_px:
            flat_points = []
            canvas_points = []
            for px, py in region.quad_points_px:
                cx, cy = self.image_to_canvas(int(px), int(py))
                flat_points.extend([cx, cy])
                canvas_points.append((cx, cy))
            self.create_polygon(*flat_points, outline=outline, width=2, fill=fill, stipple="gray25")
            if len(canvas_points) == 4:
                self.create_line(*canvas_points[0], *canvas_points[1], fill="#ffcd38", width=2)
                self.create_line(*canvas_points[3], *canvas_points[2], fill="#72e06a", width=2)
        else:
            self.create_rectangle(x0, y0, x1, y1, outline=outline, width=2, fill=fill, stipple="gray25")
            tx0, ty = self.image_to_canvas(region.x, region.top_y)
            tx1, _ = self.image_to_canvas(region.right, region.top_y)
            bx0, by = self.image_to_canvas(region.x, region.baseline_y)
            bx1, _ = self.image_to_canvas(region.right, region.baseline_y)
            self.create_line(tx0, ty, tx1, ty, fill="#ffcd38", width=2)
            self.create_line(bx0, by, bx1, by, fill="#72e06a", width=2)

        state = "active" if region.enabled else "ignored"
        label = region.label or f"Vial {idx + 1}"
        self.create_text(
            x0 + 8,
            max(14, y0 - 10),
            anchor="sw",
            text=f"{idx + 1}. {label} [{state}]",
            fill="#f8fafc",
            font=("DejaVu Sans", 10, "bold"),
        )

        if selected:
            for cx, cy in self._handle_points(region).values():
                px, py = self.image_to_canvas(cx, cy)
                self.create_rectangle(px - 4, py - 4, px + 4, py + 4, fill="#ffffff", outline="#0f172a", width=1)

    def _draw_draft_rect(self) -> None:
        if not self._drag:
            return
        start = self._drag.get("start")
        current = self._drag.get("current")
        if start is None or current is None:
            return
        x0, y0 = self.image_to_canvas(min(start[0], current[0]), min(start[1], current[1]))
        x1, y1 = self.image_to_canvas(max(start[0], current[0]), max(start[1], current[1]))
        fill = "#66c7ff" if self._drag.get("kind") == "draw" else "#ffcf6b"
        self.create_rectangle(x0, y0, x1, y1, outline=fill, width=2, dash=(5, 3))

    def _handle_points(self, region: EditorRegion) -> Dict[str, Tuple[int, int]]:
        cx = region.x + region.w // 2
        cy = region.y + region.h // 2
        return {
            "nw": (region.x, region.y),
            "n": (cx, region.y),
            "ne": (region.right, region.y),
            "e": (region.right, cy),
            "se": (region.right, region.bottom),
            "s": (cx, region.bottom),
            "sw": (region.x, region.bottom),
            "w": (region.x, cy),
        }

    def _group_dividers(self) -> List[Tuple[int, int]]:
        groups: Dict[str, List[Tuple[int, EditorRegion]]] = {}
        for idx, region in enumerate(self.regions):
            if region.group_id:
                groups.setdefault(region.group_id, []).append((idx, region))
        pairs: List[Tuple[int, int]] = []
        for items in groups.values():
            items.sort(key=lambda item: item[1].x)
            for (left_idx, left), (right_idx, right) in zip(items, items[1:]):
                if abs(left.right - right.x) <= 6:
                    pairs.append((left_idx, right_idx))
        return pairs

    def _rect_from_points(self, start: Tuple[int, int], end: Tuple[int, int]) -> Tuple[int, int, int, int]:
        x0 = min(start[0], end[0])
        y0 = min(start[1], end[1])
        x1 = max(start[0], end[0])
        y1 = max(start[1], end[1])
        return x0, y0, max(12, x1 - x0), max(12, y1 - y0)

    def _new_region(self, x: int, y: int, w: int, h: int, enabled: bool = True, group_id: Optional[str] = None) -> EditorRegion:
        top_y = int(y)
        baseline_y = int(y + h - 1)
        return EditorRegion(
            x=int(x),
            y=int(y),
            w=int(w),
            h=int(h),
            top_y=int(top_y),
            baseline_y=int(baseline_y),
            enabled=enabled,
            label="",
            group_id=group_id,
        )

    def _hit_test(self, x: int, y: int) -> Tuple[Optional[str], Any]:
        tolerance = max(4, int(round(8 / max(1e-6, self.get_scale()))))

        for left_idx, right_idx in self._group_dividers():
            left = self.regions[left_idx]
            right = self.regions[right_idx]
            if abs(x - left.right) <= tolerance and max(left.y, right.y) <= y <= min(left.bottom, right.bottom):
                return "divider", (left_idx, right_idx)

        if self.selected_index is not None and 0 <= self.selected_index < len(self.regions):
            region = self.regions[self.selected_index]
            if region.x <= x <= region.right:
                if abs(y - region.top_y) <= tolerance:
                    return "top", self.selected_index
                if abs(y - region.baseline_y) <= tolerance:
                    return "baseline", self.selected_index

            for handle_name, (hx, hy) in self._handle_points(region).items():
                if abs(x - hx) <= tolerance and abs(y - hy) <= tolerance:
                    return "resize", handle_name

            if region.x <= x <= region.right and region.y <= y <= region.bottom:
                return "move", self.selected_index

        for idx in range(len(self.regions) - 1, -1, -1):
            region = self.regions[idx]
            if region.x <= x <= region.right and region.y <= y <= region.bottom:
                return "select", idx

        return None, None

    def _on_press(self, event) -> None:
        if not self.editor_enabled or self._source_bgr is None:
            return
        point = self.canvas_to_image(event.x, event.y)
        if point is None:
            return

        if self.tool in {"draw", "split"}:
            self._drag = {
                "kind": self.tool,
                "start": point,
                "current": point,
            }
            self.render()
            return

        hit_kind, payload = self._hit_test(*point)
        if hit_kind is None:
            self.selected_index = None
            self.render()
            self._emit_select()
            self._status("No region selected.")
            return

        before_state = self.snapshot_state()

        if hit_kind == "select":
            self.selected_index = int(payload)
            self.render()
            self._emit_select()
            self._status("Selected region.")
            return

        self._push_undo()
        self._drag = {
            "kind": hit_kind,
            "payload": payload,
            "start": point,
            "before_state": before_state,
            "regions": copy.deepcopy(self.regions),
            "selected": self.selected_index,
        }
        if isinstance(payload, int):
            self.selected_index = int(payload)
        self.render()
        self._emit_select()

    def _on_motion(self, event) -> None:
        if not self._drag or self._source_bgr is None:
            return
        point = self.canvas_to_image(event.x, event.y)
        if point is None:
            return

        kind = self._drag.get("kind")
        if kind in {"draw", "split"}:
            self._drag["current"] = point
            self.render()
            return

        original_regions: List[EditorRegion] = copy.deepcopy(self._drag["regions"])
        self.regions = original_regions
        start_x, start_y = self._drag["start"]
        cur_x, cur_y = point
        dx = cur_x - start_x
        dy = cur_y - start_y
        img_h, img_w = self._source_bgr.shape[:2]

        if kind == "move" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            old_x, old_y = region.x, region.y
            region.x = max(0, min(img_w - region.w, region.x + dx))
            region.y = max(0, min(img_h - region.h, region.y + dy))
            move_dx = region.x - old_x
            move_dy = region.y - old_y
            region.top_y = max(region.y, min(region.bottom - 2, region.top_y + move_dy))
            region.baseline_y = max(region.top_y + 1, min(region.bottom - 1, region.baseline_y + move_dy))
            if region.quad_points_px:
                moved_points: List[List[int]] = []
                for px, py in region.quad_points_px:
                    moved_points.append([
                        max(0, min(img_w - 1, int(px) + move_dx)),
                        max(0, min(img_h - 1, int(py) + move_dy)),
                    ])
                region.quad_points_px = moved_points

        elif kind == "resize" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            handle = str(self._drag["payload"])
            left, top, right, bottom = region.x, region.y, region.right, region.bottom
            if "w" in handle:
                left = max(0, min(right - 12, cur_x))
            if "e" in handle:
                right = min(img_w, max(left + 12, cur_x))
            if "n" in handle:
                top = max(0, min(bottom - 12, cur_y))
            if "s" in handle:
                bottom = min(img_h, max(top + 12, cur_y))
            region.x = int(left)
            region.y = int(top)
            region.w = int(right - left)
            region.h = int(bottom - top)
            region.top_y = max(region.y, min(region.bottom - 2, region.top_y))
            region.baseline_y = max(region.top_y + 1, min(region.bottom - 1, region.baseline_y))
            region.quad_points_px = None

        elif kind == "top" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            region.top_y = max(region.y, min(region.baseline_y - 1, cur_y))
            region.quad_points_px = None

        elif kind == "baseline" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            region.baseline_y = max(region.top_y + 1, min(region.bottom - 1, cur_y))
            region.quad_points_px = None

        elif kind == "divider":
            left_idx, right_idx = self._drag["payload"]
            left = self.regions[left_idx]
            right = self.regions[right_idx]
            right_edge = right.x + right.w
            boundary = max(left.x + 12, min(right.right - 12, cur_x))
            left.w = int(boundary - left.x)
            right.x = int(boundary)
            right.w = int(max(12, right_edge - boundary))
            left.top_y = max(left.y, min(left.bottom - 2, left.top_y))
            left.baseline_y = max(left.top_y + 1, min(left.bottom - 1, left.baseline_y))
            right.top_y = max(right.y, min(right.bottom - 2, right.top_y))
            right.baseline_y = max(right.top_y + 1, min(right.bottom - 1, right.baseline_y))
            left.quad_points_px = None
            right.quad_points_px = None

        self.render()

    def _on_release(self, event) -> None:
        if not self._drag or self._source_bgr is None:
            return
        point = self.canvas_to_image(event.x, event.y)
        if point is None:
            self._drag = None
            return

        kind = self._drag.get("kind")
        if kind == "draw":
            x, y, w, h = self._rect_from_points(self._drag["start"], point)
            if w >= 12 and h >= 12:
                self._push_undo()
                self.regions.append(self._new_region(x, y, w, h))
                self.selected_index = len(self.regions) - 1
                self.render()
                self._emit_change()
                self._emit_select()
                self._status("Added a new assay region.")
        elif kind == "split":
            x, y, w, h = self._rect_from_points(self._drag["start"], point)
            if w >= 20 and h >= 12:
                self._push_undo()
                group_id = f"group-{self._group_counter}"
                self._group_counter += 1
                lane_width = w / float(self.split_count)
                new_regions = []
                last_right = x
                for idx in range(self.split_count):
                    if idx == self.split_count - 1:
                        next_right = x + w
                    else:
                        next_right = int(round(x + lane_width * (idx + 1)))
                    lane_x = int(last_right)
                    lane_w = max(12, int(next_right - lane_x))
                    new_regions.append(self._new_region(lane_x, y, lane_w, h, group_id=group_id))
                    last_right = next_right
                self.regions.extend(new_regions)
                self.selected_index = len(self.regions) - len(new_regions)
                self.render()
                self._emit_change()
                self._emit_select()
                self._status(f"Split area into {self.split_count} even vials.")
        else:
            self.render()
            self._emit_change()
            self._emit_select()

        self._drag = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fruit Fly Tracking Dashboard")
        self.geometry("1680x1040")
        self.minsize(1380, 860)

        self.project_root = Path(__file__).resolve().parent
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.settings_path = self.project_root / ".fly_tracking_gui_settings.json"
        self._settings_after_id = None
        self._suppress_region_tree_select = False

        self.channel_stop_event: Optional[threading.Event] = None
        self.channel_thread: Optional[threading.Thread] = None
        self.assay_stop_event: Optional[threading.Event] = None
        self.assay_thread: Optional[threading.Thread] = None
        self.assay_live_window: Optional[tk.Toplevel] = None
        self.assay_live_canvas: Optional[ImageCanvas] = None
        self._assay_stop_requested = False
        self._assay_live_presented_with_frame = False
        self._assay_preview_lock = threading.Lock()
        self._pending_assay_preview: Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]] = None
        self._last_assay_tree_update_s = 0.0
        self._last_assay_display_update_s = 0.0
        self._assay_poll_interval_ms = 33

        self.assay_background_image = None
        self.assay_background_path_loaded: Optional[str] = None
        self.assay_last_preview_images: Dict[str, Any] = {}
        self.assay_last_preview_rows: List[Dict[str, Any]] = []
        self.assay_last_meta: Dict[str, Any] = {}
        self.assay_saved_state: Optional[Tuple[List[EditorRegion], Optional[int]]] = None
        self.assay_calibration_dirty = False
        self.assay_guided_target_count = 0

        self._build_vars()
        self._load_settings()
        self._configure_styles()
        self._build_ui()
        self._bind_shortcuts()
        self._watch_settings_vars()
        self._bootstrap_from_disk()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if os.environ.get("DROSOPHILA_FIN6_RAISE") == "1":
            self.after(120, self._raise_window_on_startup)
        self.after(self._assay_poll_interval_ms, self._poll_ui_queue)

    def _raise_window_on_startup(self) -> None:
        try:
            self.deiconify()
            self.update_idletasks()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.winfo_exists() and self.attributes("-topmost", False))
        except tk.TclError:
            return

    def _build_vars(self) -> None:
        root = self.project_root
        self.app_status_var = tk.StringVar(value="Idle")
        self.channel_status_var = tk.StringVar(value="Channel idle")
        self.assay_status_var = tk.StringVar(value="Assay idle")
        self.footer_status_var = tk.StringVar(value="Ready")
        self.assay_preview_info_var = tk.StringVar(value="Calibration editor ready.")
        self.assay_editor_hint_var = tk.StringVar(value="Load a background to begin.")
        self.assay_region_summary_var = tk.StringVar(value="No assay regions defined.")

        self.channel_background_var = tk.StringVar(value=str(root / "backgrounds" / "channel_bg.png"))
        self.channel_calibration_var = tk.StringVar(value=str(root / "calibrations" / "channel_calibration.json"))
        self.channel_output_var = tk.StringVar(value=str(root / "outputs" / "channel"))
        self.channel_device_var = tk.StringVar(value="auto:channel")
        self.channel_width_var = tk.IntVar(value=1920)
        self.channel_height_var = tk.IntVar(value=1080)
        self.channel_fps_var = tk.IntVar(value=30)
        default_channel_mm = float(getattr(project_config, "CHANNEL_LENGTH", 149.0))
        self.channel_mm_var = tk.DoubleVar(value=default_channel_mm)
        self.channel_score_var = tk.IntVar(value=20)
        self.channel_band_var = tk.IntVar(value=35)

        self.assay_background_var = tk.StringVar(value=str(root / "backgrounds" / "assay_bg.png"))
        self.assay_calibration_var = tk.StringVar(value=str(root / "calibrations" / "assay_calibration.json"))
        self.assay_output_var = tk.StringVar(value=str(root / "outputs" / "assay"))
        self.assay_width_var = tk.IntVar(value=1536)
        self.assay_height_var = tk.IntVar(value=864)
        self.assay_fps_var = tk.DoubleVar(value=5.0)
        self.assay_camera_backend_var = tk.StringVar(value="opencv")
        self.assay_camera_device_var = tk.StringVar(value="auto:assay")
        self.assay_camera_index_var = tk.IntVar(value=0)
        self.assay_seconds_var = tk.DoubleVar(value=30.0)
        self.assay_min_area_var = tk.IntVar(value=10)
        self.assay_max_area_var = tk.IntVar(value=250)
        self.assay_threshold_var = tk.DoubleVar(value=16.0)
        self.assay_margin_var = tk.IntVar(value=8)
        self.assay_max_flies_var = tk.IntVar(value=10)
        self.assay_fullscreen_live_var = tk.BooleanVar(value=True)
        self.assay_show_xy_overlay_var = tk.BooleanVar(value=False)
        self.assay_expected_vials_var = tk.IntVar(value=4)
        self.assay_vial_width_mm_var = tk.StringVar(value="24.8")
        self.assay_height_mm_var = tk.StringVar(value="94.84")
        self.assay_snapshot_var = tk.DoubleVar(value=1.0)
        self.assay_view_var = tk.StringVar(value="calibration")
        self.assay_freeze_var = tk.BooleanVar(value=False)
        self.editor_tool_var = tk.StringVar(value="select")
        self.editor_split_count_var = tk.IntVar(value=4)
        self.assay_no_align_var = tk.BooleanVar(value=False)

    def _default_project_paths(self) -> Dict[str, Path]:
        root = self.project_root
        return {
            "channel_background_var": root / "backgrounds" / "channel_bg.png",
            "channel_calibration_var": root / "calibrations" / "channel_calibration.json",
            "channel_output_var": root / "outputs" / "channel",
            "assay_background_var": root / "backgrounds" / "assay_bg.png",
            "assay_calibration_var": root / "calibrations" / "assay_calibration.json",
            "assay_output_var": root / "outputs" / "assay",
        }

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            data = load_json(self.settings_path)
        except Exception:
            return
        for key, value in data.items():
            var = getattr(self, key, None)
            if isinstance(var, (tk.StringVar, tk.IntVar, tk.DoubleVar, tk.BooleanVar)):
                try:
                    var.set(value)
                except Exception:
                    continue
        self._normalize_project_paths()

    def _normalize_project_paths(self) -> None:
        for var_name, default_path in self._default_project_paths().items():
            var = getattr(self, var_name, None)
            if not isinstance(var, tk.StringVar):
                continue
            raw = str(var.get() or "").strip()
            if not raw:
                var.set(str(default_path))
                continue
            if _looks_like_windows_path(raw):
                var.set(str(default_path))
                continue
            current = Path(raw).expanduser()
            if not current.is_absolute():
                var.set(str((self.project_root / current).resolve()))
                continue
            if var_name in {"channel_background_var", "assay_background_var"}:
                try:
                    current.relative_to(self.project_root)
                except ValueError:
                    var.set(str(default_path))
                    continue
            same_name = current.name == default_path.name
            if same_name and not current.exists():
                var.set(str(default_path))

        channel_device = str(self.channel_device_var.get() or "").strip()
        if not channel_device or channel_device == "/dev/video8":
            self.channel_device_var.set("auto:channel")

        assay_device = str(self.assay_camera_device_var.get() or "").strip()
        if not assay_device or assay_device == "/dev/video10":
            self.assay_camera_device_var.set("auto:assay")

    def _watch_settings_vars(self) -> None:
        watch_vars = [
            "channel_background_var",
            "channel_calibration_var",
            "channel_output_var",
            "channel_device_var",
            "channel_width_var",
            "channel_height_var",
            "channel_fps_var",
            "channel_mm_var",
            "channel_score_var",
            "channel_band_var",
            "assay_background_var",
            "assay_calibration_var",
            "assay_output_var",
            "assay_width_var",
            "assay_height_var",
            "assay_fps_var",
            "assay_camera_backend_var",
            "assay_camera_device_var",
            "assay_camera_index_var",
            "assay_seconds_var",
            "assay_min_area_var",
            "assay_max_area_var",
            "assay_threshold_var",
            "assay_margin_var",
            "assay_max_flies_var",
            "assay_fullscreen_live_var",
            "assay_show_xy_overlay_var",
            "assay_expected_vials_var",
            "assay_vial_width_mm_var",
            "assay_height_mm_var",
            "assay_snapshot_var",
            "assay_view_var",
            "assay_freeze_var",
            "editor_split_count_var",
            "assay_no_align_var",
        ]
        for name in watch_vars:
            var = getattr(self, name)
            var.trace_add("write", lambda *_args: self._schedule_settings_save())
        self.assay_view_var.trace_add("write", lambda *_args: self._refresh_assay_display())
        self.assay_freeze_var.trace_add("write", lambda *_args: self._refresh_assay_display())
        self.assay_expected_vials_var.trace_add("write", lambda *_args: self._mark_assay_dirty_if_regions())
        self.assay_vial_width_mm_var.trace_add("write", lambda *_args: self._mark_assay_dirty_if_regions())
        self.assay_height_mm_var.trace_add("write", lambda *_args: self._mark_assay_dirty_if_regions())
        self.editor_split_count_var.trace_add("write", lambda *_args: self.assay_canvas.set_split_count(self.editor_split_count_var.get()))

    def _schedule_settings_save(self) -> None:
        if self._settings_after_id is not None:
            self.after_cancel(self._settings_after_id)
        self._settings_after_id = self.after(400, self._save_settings)

    def _save_settings(self) -> None:
        self._settings_after_id = None
        data = {
            name: getattr(self, name).get()
            for name in [
                "channel_background_var",
                "channel_calibration_var",
                "channel_output_var",
                "channel_device_var",
                "channel_width_var",
                "channel_height_var",
                "channel_fps_var",
                "channel_mm_var",
                "channel_score_var",
                "channel_band_var",
                "assay_background_var",
                "assay_calibration_var",
                "assay_output_var",
                "assay_width_var",
                "assay_height_var",
                "assay_fps_var",
                "assay_camera_backend_var",
                "assay_camera_device_var",
                "assay_camera_index_var",
                "assay_seconds_var",
                "assay_min_area_var",
                "assay_max_area_var",
                "assay_threshold_var",
                "assay_margin_var",
                "assay_max_flies_var",
                "assay_fullscreen_live_var",
                "assay_show_xy_overlay_var",
                "assay_expected_vials_var",
                "assay_vial_width_mm_var",
                "assay_height_mm_var",
                "assay_snapshot_var",
                "assay_view_var",
                "assay_freeze_var",
                "editor_split_count_var",
                "assay_no_align_var",
            ]
        }
        try:
            save_json(self.settings_path, data)
        except Exception:
            pass

    def _configure_styles(self) -> None:
        self.configure(bg="#eef3f9")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        base_font = ("DejaVu Sans", 10)
        style.configure(".", font=base_font)
        style.configure("TFrame", background="#eef3f9")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#0f172a", font=("DejaVu Sans", 12, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#5f6b7a")
        style.configure("Section.TLabel", background="#ffffff", foreground="#1e293b", font=("DejaVu Sans", 10, "bold"))
        style.configure("Data.TLabel", background="#ffffff", foreground="#1f2937")
        style.configure("TEntry", fieldbackground="#f8fafc")
        style.configure("TSpinbox", fieldbackground="#f8fafc")
        style.configure("Primary.TButton", padding=(12, 8), font=("DejaVu Sans", 10, "bold"))
        style.map("Primary.TButton", background=[("!disabled", "#1d4ed8"), ("active", "#1e40af"), ("disabled", "#bfd2ff")], foreground=[("!disabled", "#ffffff"), ("disabled", "#eef2ff")])
        style.configure("Danger.TButton", padding=(12, 8), font=("DejaVu Sans", 10, "bold"))
        style.map("Danger.TButton", background=[("!disabled", "#dc2626"), ("active", "#b91c1c"), ("disabled", "#fecaca")], foreground=[("!disabled", "#ffffff"), ("disabled", "#fff5f5")])
        style.configure("Secondary.TButton", padding=(10, 8))
        style.configure("SidebarPrimary.TButton", padding=(8, 5), font=("DejaVu Sans", 9, "bold"))
        style.map("SidebarPrimary.TButton", background=[("!disabled", "#1d4ed8"), ("active", "#1e40af"), ("disabled", "#bfd2ff")], foreground=[("!disabled", "#ffffff"), ("disabled", "#eef2ff")])
        style.configure("SidebarDanger.TButton", padding=(8, 5), font=("DejaVu Sans", 9, "bold"))
        style.map("SidebarDanger.TButton", background=[("!disabled", "#dc2626"), ("active", "#b91c1c"), ("disabled", "#fecaca")], foreground=[("!disabled", "#ffffff"), ("disabled", "#fff5f5")])
        style.configure("SidebarSecondary.TButton", padding=(8, 5), font=("DejaVu Sans", 9))
        style.configure("Treeview", rowheight=28, background="#ffffff", fieldbackground="#ffffff", foreground="#0f172a")
        style.configure("Treeview.Heading", font=("DejaVu Sans", 10, "bold"))
        style.configure("TNotebook", background="#eef3f9", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), font=("DejaVu Sans", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("!selected", "#dbe5f2")])
        style.configure("Tool.TRadiobutton", background="#ffffff", foreground="#334155")

    def _make_card(
        self,
        parent,
        title: str,
        subtitle: Optional[str] = None,
        *,
        padding: Tuple[int, int] = (14, 14),
        subtitle_wraplength: int = 420,
        subtitle_pad_y: Tuple[int, int] = (2, 10),
    ) -> Tuple[ttk.Frame, ttk.Frame]:
        card = ttk.Frame(parent, style="Card.TFrame", padding=padding)
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(card, text=subtitle, style="Muted.TLabel", wraplength=subtitle_wraplength, justify="left").pack(anchor="w", pady=subtitle_pad_y)
        body = ttk.Frame(card, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        return card, body

    def _bind_scrollable_sidebar(self, canvas: tk.Canvas) -> None:
        def on_mousewheel(event):
            delta = 0
            if getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            elif getattr(event, "delta", 0):
                delta = -int(event.delta / 120) if event.delta else 0
            if delta != 0:
                canvas.yview_scroll(delta, "units")

        def bind_events(_event):
            canvas.bind_all("<MouseWheel>", on_mousewheel)
            canvas.bind_all("<Button-4>", on_mousewheel)
            canvas.bind_all("<Button-5>", on_mousewheel)

        def unbind_events(_event):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", bind_events)
        canvas.bind("<Leave>", unbind_events)

    def _browse_path(self, variable, mode: str, title: str, filetypes=None) -> None:
        current = Path(variable.get()).expanduser()
        initial_dir = str(current.parent if current.parent.exists() else self.project_root)
        initial_name = current.name
        selected = ""
        if mode == "open":
            selected = filedialog.askopenfilename(title=title, initialdir=initial_dir, initialfile=initial_name, filetypes=filetypes)
        elif mode == "save":
            selected = filedialog.asksaveasfilename(title=title, initialdir=initial_dir, initialfile=initial_name, defaultextension=".json", filetypes=filetypes)
        elif mode == "dir":
            selected = filedialog.askdirectory(title=title, initialdir=initial_dir)
        if selected:
            if variable is self.channel_background_var:
                try:
                    self._load_channel_background_image(selected)
                except Exception as exc:
                    self.log(f"[error] {exc}")
                    messagebox.showerror("Channel background error", str(exc))
            elif variable is self.assay_background_var:
                try:
                    self._load_assay_background_image(selected)
                except Exception as exc:
                    self.log(f"[error] {exc}")
                    messagebox.showerror("Assay background error", str(exc))
            else:
                variable.set(selected)

    def _path_row(self, parent, label: str, variable, mode: str, filetypes=None) -> None:
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, style="Section.TLabel", width=12).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(
            row,
            text="Browse",
            style="Secondary.TButton",
            command=lambda: self._browse_path(variable, mode, title=f"Select {label.lower()}", filetypes=filetypes),
        ).pack(side="left")

    def _metric_entry(self, parent, row: int, col: int, label: str, variable, width: int = 8) -> None:
        ttk.Label(parent, text=label, style="Data.TLabel").grid(row=row, column=col * 2, sticky="w", padx=(0, 6), pady=5)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=col * 2 + 1, sticky="ew", padx=(0, 14), pady=5)

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg="#0b1220", height=72)
        header.pack(fill="x")
        tk.Label(header, text="Fruit Fly Tracking Dashboard", bg="#0b1220", fg="#f8fafc", font=("DejaVu Sans", 18, "bold")).pack(side="left", padx=18, pady=18)
        tk.Label(header, textvariable=self.app_status_var, bg="#1e3a5f", fg="#dbeafe", font=("DejaVu Sans", 10, "bold"), padx=12, pady=6).pack(side="right", padx=18)
        tk.Label(header, textvariable=self.assay_status_var, bg="#10243f", fg="#bfdbfe", font=("DejaVu Sans", 10), padx=12, pady=6).pack(side="right", padx=(0, 10))
        tk.Label(header, textvariable=self.channel_status_var, bg="#10243f", fg="#bfdbfe", font=("DejaVu Sans", 10), padx=12, pady=6).pack(side="right", padx=(0, 10))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=14)
        channel_tab = ttk.Frame(self.notebook)
        assay_tab = ttk.Frame(self.notebook)
        self.notebook.add(channel_tab, text="Channel / Brio")
        self.notebook.add(assay_tab, text="Assay / IMX477")

        self._build_channel_tab(channel_tab)
        self._build_assay_tab(assay_tab)

        footer = tk.Frame(self, bg="#dce6f3", height=34)
        footer.pack(fill="x", side="bottom")
        tk.Label(footer, textvariable=self.footer_status_var, bg="#dce6f3", fg="#243447", anchor="w", padx=12).pack(fill="x")

    def _build_channel_tab(self, parent) -> None:
        content = ttk.Frame(parent, padding=(14, 14))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=5)
        content.rowconfigure(1, weight=1)

        controls_card, controls = self._make_card(content, "Channel controls", "Background capture, calibration, and live detection for the Brio camera.")
        controls_card.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 12))

        self._path_row(controls, "Background", self.channel_background_var, "open", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        self._path_row(controls, "Calibration", self.channel_calibration_var, "save", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        self._path_row(controls, "Output dir", self.channel_output_var, "dir")

        device_row = ttk.Frame(controls, style="Card.TFrame")
        device_row.pack(fill="x", pady=(8, 6))
        ttk.Label(device_row, text="Device", style="Section.TLabel", width=12).pack(side="left")
        ttk.Entry(device_row, textvariable=self.channel_device_var, width=18).pack(side="left", padx=(8, 12))

        metrics = ttk.Frame(controls, style="Card.TFrame")
        metrics.pack(fill="x", pady=(8, 6))
        for col in range(4):
            metrics.columnconfigure(col * 2 + 1, weight=1)
        self._metric_entry(metrics, 0, 0, "Width", self.channel_width_var)
        self._metric_entry(metrics, 0, 1, "Height", self.channel_height_var)
        self._metric_entry(metrics, 1, 0, "FPS", self.channel_fps_var)
        self._metric_entry(metrics, 1, 1, "Channel mm", self.channel_mm_var)
        self._metric_entry(metrics, 2, 0, "Score", self.channel_score_var)
        self._metric_entry(metrics, 2, 1, "Band width", self.channel_band_var)

        action_bar = ttk.Frame(controls, style="Card.TFrame")
        action_bar.pack(fill="x", pady=(12, 0))
        self.channel_capture_btn = ttk.Button(action_bar, text="Capture background", style="Secondary.TButton", command=self.capture_channel_background)
        self.channel_capture_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.channel_calibrate_btn = ttk.Button(action_bar, text="Calibrate", style="Secondary.TButton", command=self.calibrate_channel)
        self.channel_calibrate_btn.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.channel_detect_btn = ttk.Button(action_bar, text="Detect once", style="Primary.TButton", command=self.detect_channel_once)
        self.channel_detect_btn.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.channel_start_btn = ttk.Button(action_bar, text="Start live", style="Primary.TButton", command=self.start_channel_live)
        self.channel_start_btn.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.channel_stop_btn = ttk.Button(action_bar, text="Stop live", style="Danger.TButton", command=self.stop_channel_live)
        self.channel_stop_btn.grid(row=1, column=2, sticky="ew")
        for col in range(3):
            action_bar.columnconfigure(col, weight=1)

        preview_card, preview_body = self._make_card(content, "Channel preview", "Annotated detection output updates here.")
        preview_card.grid(row=0, column=1, sticky="nsew")
        preview_body.rowconfigure(0, weight=1)
        preview_body.columnconfigure(0, weight=1)
        self.channel_canvas = ImageCanvas(preview_body)
        self.channel_canvas.grid(row=0, column=0, sticky="nsew")
        self.channel_canvas.set_empty_text("Capture a background or run detection to populate the preview.")

        table_card, table_body = self._make_card(content, "Channel detections", "Latest detections along the calibrated channel.")
        table_card.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        table_body.columnconfigure(0, weight=1)
        table_body.rowconfigure(0, weight=1)
        self.channel_tree = ttk.Treeview(table_body, columns=("index", "mm", "px"), show="headings", height=7)
        for col, title, width in [("index", "Index", 80), ("mm", "Position mm", 140), ("px", "Position px", 140)]:
            self.channel_tree.heading(col, text=title)
            self.channel_tree.column(col, width=width, anchor="center")
        self.channel_tree.grid(row=0, column=0, sticky="nsew")

    def _build_assay_tab(self, parent) -> None:
        content = ttk.Frame(parent, padding=(14, 14))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=5)
        content.columnconfigure(1, weight=3)
        content.rowconfigure(0, weight=6)
        content.rowconfigure(1, weight=3)

        viewer_card, viewer_body = self._make_card(content, "Assay viewer", "Calibration editor, live previews, and debugging overlays stay in the main dashboard.")
        viewer_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        viewer_body.columnconfigure(0, weight=1)
        viewer_body.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(viewer_body, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(toolbar, text="View", style="Section.TLabel").pack(side="left")
        for value, text in [("calibration", "Calibration"), ("annotated", "Annotated"), ("raw", "Raw"), ("mask", "Mask")]:
            ttk.Radiobutton(toolbar, text=text, value=value, variable=self.assay_view_var, style="Tool.TRadiobutton").pack(side="left", padx=(10, 0))
        ttk.Checkbutton(toolbar, text="Freeze preview", variable=self.assay_freeze_var).pack(side="left", padx=(16, 0))
        ttk.Button(toolbar, text="Snapshot", style="Secondary.TButton", command=self.save_assay_snapshot).pack(side="right")

        self.assay_canvas = CalibrationCanvas(viewer_body)
        self.assay_canvas.grid(row=1, column=0, sticky="nsew")
        self.assay_canvas.set_empty_text("Load or capture an assay background to begin calibration.")
        self.assay_canvas.set_callbacks(
            on_change=self._on_assay_regions_changed,
            on_select=self._on_assay_region_selected,
            on_status=self._set_assay_editor_hint,
        )

        info_row = ttk.Frame(viewer_body, style="Card.TFrame")
        info_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(info_row, textvariable=self.assay_preview_info_var, style="Muted.TLabel").pack(side="left")

        sidebar_shell = ttk.Frame(content)
        sidebar_shell.grid(row=0, column=1, sticky="nsew")
        sidebar_shell.columnconfigure(0, weight=1)
        sidebar_shell.rowconfigure(0, weight=1)

        sidebar_canvas = tk.Canvas(sidebar_shell, background="#eef3f9", highlightthickness=0, borderwidth=0)
        sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        sidebar_scrollbar = ttk.Scrollbar(sidebar_shell, orient="vertical", command=sidebar_canvas.yview)
        sidebar_scrollbar.grid(row=0, column=1, sticky="ns")
        sidebar_canvas.configure(yscrollcommand=sidebar_scrollbar.set)

        sidebar = ttk.Frame(sidebar_canvas)
        sidebar_window = sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")
        sidebar.columnconfigure(0, weight=1)
        sidebar.bind("<Configure>", lambda _e: sidebar_canvas.configure(scrollregion=sidebar_canvas.bbox("all")))
        sidebar_canvas.bind(
            "<Configure>",
            lambda event: sidebar_canvas.itemconfigure(sidebar_window, width=max(1, event.width)),
        )
        self._bind_scrollable_sidebar(sidebar_canvas)

        camera_card, camera_body = self._make_card(
            sidebar,
            "Assay camera",
            "Pick the assay source and fallback device here.",
            padding=(10, 10),
            subtitle_wraplength=320,
            subtitle_pad_y=(2, 6),
        )
        camera_card.grid(row=0, column=0, sticky="ew")
        source_row = ttk.Frame(camera_body, style="Card.TFrame")
        source_row.pack(fill="x", pady=(0, 6))
        ttk.Label(source_row, text="Source", style="Section.TLabel", width=10).pack(side="left")
        ttk.Radiobutton(source_row, text="USB webcam", value="opencv", variable=self.assay_camera_backend_var, style="Tool.TRadiobutton").pack(side="left", padx=(8, 8))
        ttk.Radiobutton(source_row, text="Pi HQ", value="pihq", variable=self.assay_camera_backend_var, style="Tool.TRadiobutton").pack(side="left")

        device_row = ttk.Frame(camera_body, style="Card.TFrame")
        device_row.pack(fill="x", pady=(0, 6))
        ttk.Label(device_row, text="USB device", style="Section.TLabel", width=10).pack(side="left")
        ttk.Entry(device_row, textvariable=self.assay_camera_device_var, width=18).pack(side="left", padx=(8, 10))

        index_row = ttk.Frame(camera_body, style="Card.TFrame")
        index_row.pack(fill="x")
        ttk.Label(index_row, text="Camera idx", style="Section.TLabel", width=10).pack(side="left")
        ttk.Entry(index_row, textvariable=self.assay_camera_index_var, width=8).pack(side="left", padx=(8, 10))
        ttk.Label(index_row, text="Pi HQ uses index; USB uses device path.", style="Muted.TLabel", wraplength=210, justify="left").pack(side="left")

        files_card, files = self._make_card(
            sidebar,
            "Files",
            "Background, calibration, and output paths.",
            padding=(10, 10),
            subtitle_wraplength=320,
            subtitle_pad_y=(2, 6),
        )
        files_card.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._path_row(files, "Background", self.assay_background_var, "open", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        self._path_row(files, "Calibration", self.assay_calibration_var, "save", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        self._path_row(files, "Output root", self.assay_output_var, "dir")

        actions_card, actions = self._make_card(
            sidebar,
            "Primary actions",
            "Background, calibration, preview, and run.",
            padding=(10, 10),
            subtitle_wraplength=320,
            subtitle_pad_y=(2, 6),
        )
        actions_card.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        for col in range(2):
            actions.columnconfigure(col, weight=1)
        self.assay_capture_bg_btn = ttk.Button(actions, text="Capture bg", style="SidebarSecondary.TButton", command=self.capture_assay_background)
        self.assay_capture_bg_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        self.assay_calibrate_btn = ttk.Button(actions, text="Calibrate", style="SidebarPrimary.TButton", command=self.calibrate_assay_external)
        self.assay_calibrate_btn.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self.assay_load_cal_btn = ttk.Button(actions, text="Load cal", style="SidebarSecondary.TButton", command=self.load_assay_calibration_into_editor)
        self.assay_load_cal_btn.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        self.assay_save_cal_btn = ttk.Button(actions, text="Save cal", style="SidebarSecondary.TButton", command=self.save_assay_calibration_from_editor)
        self.assay_save_cal_btn.grid(row=1, column=1, sticky="ew", pady=(0, 4))
        self.assay_test_btn = ttk.Button(actions, text="Test frame", style="SidebarSecondary.TButton", command=self.test_assay_on_frame)
        self.assay_test_btn.grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        self.assay_mask_btn = ttk.Button(actions, text="Mask", style="SidebarSecondary.TButton", command=lambda: self.test_assay_on_frame(force_view="mask"))
        self.assay_mask_btn.grid(row=2, column=1, sticky="ew", pady=(0, 4))
        self.assay_start_btn = ttk.Button(actions, text="Start run", style="SidebarPrimary.TButton", command=self.start_assay)
        self.assay_start_btn.grid(row=3, column=0, sticky="ew", padx=(0, 6))
        self.assay_stop_btn = ttk.Button(actions, text="Stop", style="SidebarDanger.TButton", command=self.stop_assay)
        self.assay_stop_btn.grid(row=3, column=1, sticky="ew")

        tools_card, tools = self._make_card(
            sidebar,
            "Calibration tools",
            "Guide four spaced vial bounds, then fine-tune references if needed.",
            padding=(10, 10),
            subtitle_wraplength=320,
            subtitle_pad_y=(2, 6),
        )
        tools_card.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        guide_row = ttk.Frame(tools, style="Card.TFrame")
        guide_row.pack(fill="x", pady=(0, 6))
        self.assay_guide_btn = ttk.Button(guide_row, text="Guide vials", style="SidebarPrimary.TButton", command=self.start_guided_assay_calibration)
        self.assay_guide_btn.pack(side="left")
        ttk.Label(guide_row, text="Use this when the four vials have gaps between them.", style="Muted.TLabel", wraplength=180, justify="left").pack(side="left", padx=(8, 0))
        tool_row = ttk.Frame(tools, style="Card.TFrame")
        tool_row.pack(fill="x")
        for value, text in [("select", "Select"), ("draw", "Draw bound"), ("split", "Split evenly")]:
            ttk.Radiobutton(tool_row, text=text, value=value, variable=self.editor_tool_var, style="Tool.TRadiobutton", command=self._on_editor_tool_changed).pack(side="left", padx=(0, 10))
        split_row = ttk.Frame(tools, style="Card.TFrame")
        split_row.pack(fill="x", pady=(6, 6))
        ttk.Label(split_row, text="Split count", style="Data.TLabel").pack(side="left")
        ttk.Spinbox(split_row, textvariable=self.editor_split_count_var, from_=2, to=12, width=6).pack(side="left", padx=(8, 0))

        button_grid = ttk.Frame(tools, style="Card.TFrame")
        button_grid.pack(fill="x", pady=(2, 0))
        for col in range(2):
            button_grid.columnconfigure(col, weight=1)
        ttk.Button(button_grid, text="Duplicate", style="SidebarSecondary.TButton", command=self.assay_canvas.duplicate_selected).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        ttk.Button(button_grid, text="Toggle state", style="SidebarSecondary.TButton", command=self.assay_canvas.toggle_selected_enabled).grid(row=0, column=1, sticky="ew", pady=(0, 4))
        ttk.Button(button_grid, text="Delete", style="SidebarSecondary.TButton", command=self.assay_canvas.delete_selected).grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        ttk.Button(button_grid, text="Copy refs", style="SidebarSecondary.TButton", command=self.assay_canvas.apply_reference_style_from_selected).grid(row=1, column=1, sticky="ew", pady=(0, 4))
        ttk.Button(button_grid, text="Move up", style="SidebarSecondary.TButton", command=lambda: self.assay_canvas.move_selected(-1)).grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        ttk.Button(button_grid, text="Move down", style="SidebarSecondary.TButton", command=lambda: self.assay_canvas.move_selected(1)).grid(row=2, column=1, sticky="ew", pady=(0, 4))
        ttk.Button(button_grid, text="Sort L->R", style="SidebarSecondary.TButton", command=self.assay_canvas.sort_left_to_right).grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))
        ttk.Button(button_grid, text="Reset", style="SidebarSecondary.TButton", command=self.reset_assay_editor).grid(row=3, column=1, sticky="ew", pady=(0, 4))
        ttk.Button(button_grid, text="Undo", style="SidebarSecondary.TButton", command=self.assay_canvas.undo).grid(row=4, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(button_grid, text="Redo", style="SidebarSecondary.TButton", command=self.assay_canvas.redo).grid(row=4, column=1, sticky="ew")

        ttk.Label(
            tools,
            text="Tip: drag the yellow and green lines to edit top and baseline after placing a region.",
            style="Muted.TLabel",
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(8, 3))
        ttk.Label(tools, textvariable=self.assay_editor_hint_var, style="Muted.TLabel", wraplength=320, justify="left").pack(anchor="w")

        settings_card, settings = self._make_card(
            sidebar,
            "Run settings",
            "Resolution, timing, and detection thresholds.",
            padding=(10, 10),
            subtitle_wraplength=320,
            subtitle_pad_y=(2, 6),
        )
        settings_card.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        for col in range(4):
            settings.columnconfigure(col * 2 + 1, weight=1)
        self._metric_entry(settings, 0, 0, "Width", self.assay_width_var)
        self._metric_entry(settings, 0, 1, "Height", self.assay_height_var)
        self._metric_entry(settings, 1, 0, "FPS", self.assay_fps_var)
        self._metric_entry(settings, 1, 1, "Seconds", self.assay_seconds_var)
        self._metric_entry(settings, 2, 0, "Min area", self.assay_min_area_var)
        self._metric_entry(settings, 2, 1, "Max area", self.assay_max_area_var)
        self._metric_entry(settings, 3, 0, "Threshold", self.assay_threshold_var)
        self._metric_entry(settings, 3, 1, "Margin", self.assay_margin_var)
        self._metric_entry(settings, 4, 0, "Snapshot s", self.assay_snapshot_var)
        self._metric_entry(settings, 4, 1, "Max flies", self.assay_max_flies_var)
        self._metric_entry(settings, 5, 0, "Vials", self.assay_expected_vials_var)
        self._metric_entry(settings, 5, 1, "Vial W mm", self.assay_vial_width_mm_var)
        self._metric_entry(settings, 6, 0, "Vial H mm", self.assay_height_mm_var)
        ttk.Checkbutton(settings, text="Full-screen live tracker", variable=self.assay_fullscreen_live_var).grid(row=6, column=2, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(settings, text="Show x/y on live overlay", variable=self.assay_show_xy_overlay_var).grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(settings, text="Skip frame alignment", variable=self.assay_no_align_var).grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 0))

        regions_card, regions = self._make_card(
            sidebar,
            "Assay regions",
            "Region order controls physical numbering.",
            padding=(10, 10),
            subtitle_wraplength=320,
            subtitle_pad_y=(2, 6),
        )
        regions_card.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        sidebar.rowconfigure(5, weight=1)
        regions.columnconfigure(0, weight=1)
        regions.rowconfigure(1, weight=1)
        ttk.Label(regions, textvariable=self.assay_region_summary_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.assay_region_tree = ttk.Treeview(regions, columns=("physical", "tube", "state", "label", "roi", "refs"), show="headings", height=6)
        for col, title, width in [
            ("physical", "P", 42),
            ("tube", "T", 42),
            ("state", "State", 72),
            ("label", "Label", 120),
            ("roi", "ROI", 122),
            ("refs", "Top/Base", 92),
        ]:
            self.assay_region_tree.heading(col, text=title)
            self.assay_region_tree.column(col, width=width, anchor="center")
        self.assay_region_tree.grid(row=1, column=0, sticky="nsew")
        self.assay_region_tree.bind("<<TreeviewSelect>>", self._on_region_tree_select)

        lower = ttk.Panedwindow(content, orient="horizontal")
        lower.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        self.assay_lower_panel = lower

        flies_card, flies = self._make_card(lower, "Active flies", "Live assay tracks with calibrated x/y inside each vial.")
        log_card, log_body = self._make_card(lower, "Assay log", "Timestamped capture, calibration, and run events.")
        lower.add(flies_card, weight=3)
        lower.add(log_card, weight=2)

        flies.columnconfigure(0, weight=1)
        flies.rowconfigure(0, weight=1)
        self.assay_tree = ttk.Treeview(flies, columns=("label", "tube", "status", "x", "y", "center"), show="headings", height=10)
        for col, title, width in [
            ("label", "Fly", 130),
            ("tube", "Tube", 60),
            ("status", "Status", 70),
            ("x", "X pos", 95),
            ("y", "Y pos", 95),
            ("center", "Center px", 120),
        ]:
            self.assay_tree.heading(col, text=title)
            self.assay_tree.column(col, width=width, anchor="center")
        self.assay_tree.grid(row=0, column=0, sticky="nsew")

        log_body.columnconfigure(0, weight=1)
        log_body.rowconfigure(0, weight=1)
        self.assay_log = tk.Text(log_body, height=10, wrap="word", background="#0f172a", foreground="#dce8f7", insertbackground="#dce8f7", relief="flat", padx=10, pady=10)
        self.assay_log.grid(row=0, column=0, sticky="nsew")

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-s>", lambda _e: self.save_assay_calibration_from_editor())
        self.bind("<Control-o>", lambda _e: self.load_assay_calibration_into_editor())
        self.bind("<Control-z>", lambda _e: self.assay_canvas.undo())
        self.bind("<Control-y>", lambda _e: self.assay_canvas.redo())
        self.bind("<Delete>", lambda _e: self.assay_canvas.delete_selected())
        self.bind("<BackSpace>", lambda _e: self.assay_canvas.delete_selected())
        self.bind("<Escape>", lambda _e: self.assay_canvas.cancel_current_action())
        self.bind("<Control-n>", lambda _e: self._set_editor_tool("draw"))
        self.bind("<Control-l>", lambda _e: self._set_editor_tool("split"))
        self.bind("<space>", lambda _e: self._toggle_freeze())

    def _bootstrap_from_disk(self) -> None:
        if Path(self.channel_background_var.get()).exists():
            try:
                self._load_channel_background_image(self.channel_background_var.get(), silent=True)
            except Exception:
                pass
        if Path(self.assay_background_var.get()).exists():
            try:
                self._load_assay_background_image(self.assay_background_var.get(), silent=True)
            except Exception:
                pass
        if Path(self.assay_calibration_var.get()).exists():
            try:
                self.load_assay_calibration_into_editor(silent=True)
            except Exception:
                pass
        self._on_editor_tool_changed()
        self._update_channel_ui_state()
        self._update_assay_ui_state()
        self._update_global_status()

    def log(self, text: str) -> None:
        self.assay_log.insert("end", f"[{_ts()}] {text.rstrip()}\n")
        self.assay_log.see("end")

    def _set_footer(self, text: str) -> None:
        self.footer_status_var.set(text)

    def _set_assay_editor_hint(self, text: str) -> None:
        self.assay_editor_hint_var.set(text)
        self._set_footer(text)

    def _mark_assay_dirty_if_regions(self) -> None:
        if self.assay_canvas.get_regions():
            self.assay_calibration_dirty = True
            self._update_assay_ui_state()

    def _expected_vial_count(self) -> int:
        return max(1, int(self.assay_expected_vials_var.get()))

    def _set_assay_calibration_layout(self, expanded: bool) -> None:
        panel = getattr(self, "assay_lower_panel", None)
        if panel is None:
            return
        try:
            mapped = bool(panel.winfo_ismapped())
        except tk.TclError:
            return
        if expanded and mapped:
            panel.grid_remove()
        elif not expanded and not mapped:
            panel.grid()

    def _guided_assay_hint(self, next_index: int, total: int) -> str:
        return f"Guided calibration: set vial {next_index} of {total} with a tight box or 4 corner points."

    def start_guided_assay_calibration(self) -> None:
        self.assay_guided_target_count = 0
        self.assay_freeze_var.set(False)
        self.assay_view_var.set("calibration")
        self._set_assay_calibration_layout(True)
        self._set_assay_editor_hint(
            f"Opening guided calibration window for {self._expected_vial_count()} vials. Use B for box mode or P for 4-point mode, then ENTER to accept or R to reset."
        )
        self.calibrate_assay_external()

    def _maybe_complete_guided_assay_calibration(self) -> bool:
        target = int(self.assay_guided_target_count)
        if target <= 0:
            return False
        regions = self.assay_canvas.get_regions()
        count = len(regions)
        if count < target:
            self._set_assay_editor_hint(self._guided_assay_hint(count + 1, target))
            return False
        if count > target:
            self.assay_guided_target_count = 0
            self._set_assay_editor_hint(f"Guided calibration expected {target} vials, but {count} were drawn. Delete extras or reset and try again.")
            return False

        self.assay_guided_target_count = 0
        regions.sort(key=lambda region: (region.x, region.y))
        for idx, region in enumerate(regions, start=1):
            region.label = f"Vial {idx}"
        self.assay_canvas.set_regions(regions)
        self._set_editor_tool("select")
        self._set_assay_editor_hint("Guided calibration complete. Drag any vial or top/base line to fine-tune.")
        self.log("Guided assay calibration captured all vial bounds.")
        return True

    def _on_assay_regions_changed(self) -> None:
        if self._maybe_complete_guided_assay_calibration():
            return
        self.assay_calibration_dirty = True
        self._update_assay_region_tree()
        if self.assay_view_var.get() == "calibration" and not self._assay_is_running():
            self._refresh_assay_display()
        self._update_assay_ui_state()
        self._update_global_status()

    def _on_assay_region_selected(self, index: Optional[int]) -> None:
        self._update_assay_region_tree(selected_override=index)
        self._update_assay_ui_state()

    def _on_region_tree_select(self, _event) -> None:
        if self._suppress_region_tree_select:
            return
        selected = self.assay_region_tree.selection()
        if not selected:
            if self.assay_canvas.selected_index is not None:
                self.assay_canvas.set_selected_index(None)
            return
        selected_index = int(selected[0])
        if self.assay_canvas.selected_index != selected_index:
            self.assay_canvas.set_selected_index(selected_index)

    def _update_assay_region_tree(self, selected_override: Optional[int] = None) -> None:
        regions = self.assay_canvas.get_regions()
        self._suppress_region_tree_select = True
        try:
            for item in self.assay_region_tree.get_children():
                self.assay_region_tree.delete(item)
            assay_counter = 1
            active_count = 0
            for idx, region in enumerate(regions, start=1):
                if region.enabled:
                    tube_text = str(assay_counter)
                    assay_counter += 1
                    active_count += 1
                else:
                    tube_text = "-"
                state = "Active" if region.enabled else "Ignored"
                label = region.label or f"Vial {idx}"
                roi = f"{region.x},{region.y},{region.w}x{region.h}"
                refs = f"{region.top_y}/{region.baseline_y}"
                self.assay_region_tree.insert("", "end", iid=str(idx - 1), values=(idx, tube_text, state, label, roi, refs))
            selected = self.assay_canvas.selected_index if selected_override is None else selected_override
            current_selection = self.assay_region_tree.selection()
            target_selection = () if selected is None else ((str(selected),) if str(selected) in self.assay_region_tree.get_children() else ())
            if current_selection != target_selection:
                if target_selection:
                    self.assay_region_tree.selection_set(target_selection[0])
                    self.assay_region_tree.focus(target_selection[0])
                else:
                    self.assay_region_tree.selection_remove(*current_selection)
            self.assay_region_summary_var.set(f"{active_count} active vials, {len(regions) - active_count} ignored, {len(regions)} total.")
        finally:
            self._suppress_region_tree_select = False

    def _assay_is_running(self) -> bool:
        return self.assay_thread is not None and self.assay_thread.is_alive()

    def _channel_is_running(self) -> bool:
        return self.channel_thread is not None and self.channel_thread.is_alive()

    def _update_global_status(self) -> None:
        if self._assay_is_running():
            self.app_status_var.set("Assay running")
        elif self._channel_is_running():
            self.app_status_var.set("Channel live")
        elif self.assay_canvas.get_regions():
            state = "Calibration ready"
            if self.assay_calibration_dirty:
                state += " (unsaved)"
            self.app_status_var.set(state)
        else:
            self.app_status_var.set("Idle")

    def _update_channel_ui_state(self) -> None:
        running = self._channel_is_running()
        self.channel_capture_btn.configure(state="disabled" if running else "normal")
        self.channel_calibrate_btn.configure(state="disabled" if running else "normal")
        self.channel_detect_btn.configure(state="disabled" if running else "normal")
        self.channel_start_btn.configure(state="disabled" if running else "normal")
        self.channel_stop_btn.configure(state="normal" if running else "disabled")

    def _update_assay_ui_state(self) -> None:
        running = self._assay_is_running()
        has_regions = bool(self.assay_canvas.get_regions())
        has_background = self.assay_background_image is not None
        has_calibration_file = Path(self.assay_calibration_var.get()).exists()
        selected = self.assay_canvas.selected_index is not None

        self.assay_capture_bg_btn.configure(state="disabled" if running else "normal")
        self.assay_calibrate_btn.configure(state="normal" if (has_background and not running) else "disabled")
        self.assay_load_cal_btn.configure(state="disabled" if running else "normal")
        self.assay_guide_btn.configure(state="normal" if (has_background and not running) else "disabled")
        self.assay_save_cal_btn.configure(state="normal" if (has_background and has_regions and not running) else "disabled")
        self.assay_test_btn.configure(state="normal" if (has_background and has_regions and not running) else "disabled")
        self.assay_mask_btn.configure(state="normal" if (has_background and has_regions and not running) else "disabled")
        self.assay_start_btn.configure(state="normal" if (has_background and (has_calibration_file or has_regions) and not running) else "disabled")
        self.assay_stop_btn.configure(state="normal" if running else "disabled")
        self.assay_canvas.set_editor_enabled(not running and self.assay_view_var.get() == "calibration")
        self.assay_canvas.set_show_regions(self.assay_view_var.get() == "calibration" and not running)
        if not running or not self.assay_fullscreen_live_var.get():
            self._close_assay_live_window()

        self.channel_status_var.set("Channel live" if self._channel_is_running() else self.channel_status_var.get())
        if running:
            self.assay_status_var.set("Stopping assay" if self._assay_stop_requested else "Assay running")
        elif has_regions:
            state = "Calibration ready"
            if self.assay_calibration_dirty:
                state += " (unsaved)"
            self.assay_status_var.set(state)
        elif has_background:
            self.assay_status_var.set("Background loaded")
        else:
            self.assay_status_var.set("Assay idle")

        self._update_global_status()
        self._set_footer(self.assay_editor_hint_var.get() if self.assay_view_var.get() == "calibration" else self.assay_preview_info_var.get())

    def _on_editor_tool_changed(self) -> None:
        self._set_editor_tool(self.editor_tool_var.get())

    def _set_editor_tool(self, tool: str) -> None:
        self.editor_tool_var.set(tool)
        self.assay_canvas.set_tool(tool)
        self.assay_canvas.set_split_count(self.editor_split_count_var.get())
        self.assay_canvas.set_editor_enabled(not self._assay_is_running() and self.assay_view_var.get() == "calibration")

    def _toggle_freeze(self) -> None:
        self.assay_freeze_var.set(not self.assay_freeze_var.get())

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]

                if kind == "channel_preview":
                    _, image_bgr, result = item
                    self.channel_canvas.set_image_bgr(image_bgr)
                    self._update_channel_tree(result)
                    self.channel_status_var.set(f"Channel detections: {result.get('count', 0)}")

                elif kind == "channel_background_saved":
                    _, path = item
                    try:
                        self._load_channel_background_image(path)
                    except Exception as exc:
                        self.log(f"[error] {exc}")
                        messagebox.showerror("Channel background error", str(exc))

                elif kind == "channel_status":
                    _, text = item
                    self.channel_status_var.set(text)
                    self._set_footer(text)

                elif kind == "assay_background_saved":
                    _, path = item
                    try:
                        self._load_assay_background_image(path)
                        self.log(f"Saved assay background: {path}")
                    except Exception as exc:
                        self.log(f"[error] {exc}")
                        messagebox.showerror("Assay background error", str(exc))

                elif kind == "assay_preview":
                    _, preview_images, rows, meta = item
                    with self._assay_preview_lock:
                        self._pending_assay_preview = (preview_images, rows, meta)

                elif kind == "assay_done":
                    _, result = item
                    self._assay_stop_requested = False
                    self.assay_status_var.set("Assay finished")
                    self._close_assay_live_window()
                    self.log("Assay finished.")
                    self.log(json.dumps(result, indent=2))
                    self._update_assay_ui_state()
                    messagebox.showinfo("Assay complete", f"Finished.\n\nReport PDF:\n{result.get('report_pdf', '')}")

                elif kind == "assay_stopped":
                    _, result = item
                    self._assay_stop_requested = False
                    self.assay_status_var.set("Assay stopped")
                    self._close_assay_live_window()
                    self.log("Assay stopped.")
                    self.log(json.dumps(result, indent=2))
                    self._update_assay_ui_state()

                elif kind == "assay_error":
                    _, text = item
                    self._assay_stop_requested = False
                    self.assay_status_var.set("Assay error")
                    self._close_assay_live_window()
                    self.log(f"[error] {text}")
                    self._update_assay_ui_state()
                    messagebox.showerror("Assay error", text)

        except queue.Empty:
            pass

        self._consume_pending_assay_preview()

        self._update_channel_ui_state()
        self._update_assay_ui_state()
        self.after(self._assay_poll_interval_ms, self._poll_ui_queue)

    def _consume_pending_assay_preview(self) -> None:
        with self._assay_preview_lock:
            pending = self._pending_assay_preview
            self._pending_assay_preview = None
        if pending is None:
            return
        if self._assay_stop_requested:
            return

        preview_images, rows, meta = pending
        self.assay_last_preview_images = preview_images
        self.assay_last_preview_rows = rows
        self.assay_last_meta = meta
        self.assay_preview_info_var.set(
            f"view={self.assay_view_var.get()}   frame={meta.get('frame_index', 0)}   detections={meta.get('detection_count', 0)}   active={meta.get('active_track_count', 0)}"
        )

        now = time.monotonic()
        refresh_tree = (not self._assay_is_running()) or ((now - self._last_assay_tree_update_s) >= 0.25)
        if refresh_tree:
            self._update_assay_tree(rows)
            self._last_assay_tree_update_s = now

        self._update_assay_live_window(preview_images.get("annotated"))

        if self.assay_freeze_var.get():
            return
        display_interval_s = 0.22 if (self._assay_is_running() and self.assay_fullscreen_live_var.get()) else 0.08
        if (not self._assay_is_running()) or ((now - self._last_assay_display_update_s) >= display_interval_s):
            self._refresh_assay_display()
            self._last_assay_display_update_s = now

    def _update_channel_tree(self, result: Dict[str, Any]) -> None:
        for item in self.channel_tree.get_children():
            self.channel_tree.delete(item)
        for det in result.get("detections", []):
            self.channel_tree.insert("", "end", values=(
                det.get("index"),
                f"{det.get('x_along_channel_mm', 0):.2f}",
                f"{det.get('x_along_channel_px', 0):.2f}",
            ))

    def _load_channel_background_image(self, path: str | Path, silent: bool = False) -> None:
        path_obj = Path(path)
        image = _read_bgr(path_obj)
        self.channel_background_var.set(str(path_obj.resolve()))
        self.channel_canvas.set_image_bgr(image)
        self._update_channel_tree({"detections": []})
        self.channel_status_var.set("Channel background loaded")
        self._set_footer(f"Loaded channel background: {path_obj.resolve()}")
        if not silent:
            self.log(f"Loaded channel background: {path_obj.resolve()}")

    def _update_assay_tree(self, rows: List[Dict[str, Any]]) -> None:
        for item in self.assay_tree.get_children():
            self.assay_tree.delete(item)
        for row in rows:
            x_pos = row.get("x_from_left_mm")
            if x_pos is not None:
                x_text = f"{x_pos:.2f} mm"
            elif row.get("x_from_left_px") is not None:
                x_text = f"{row['x_from_left_px']:.1f} px"
            else:
                x_text = "-"
            y_pos = row.get("y_from_base_mm")
            if y_pos is not None:
                y_text = f"{y_pos:.2f} mm"
            elif row.get("y_from_base_px") is not None:
                y_text = f"{row['y_from_base_px']:.1f} px"
            else:
                y_text = "-"
            self.assay_tree.insert("", "end", values=(
                row.get("label"),
                row.get("assay_tube_index"),
                "detected" if row.get("detected") else "held",
                x_text,
                y_text,
                f"({row.get('x_px', 0):.0f}, {row.get('y_px', 0):.0f})",
            ))

    def _current_tube_height_mm(self) -> Optional[float]:
        text = self.assay_height_mm_var.get().strip()
        return float(text) if text else None

    def _current_tube_width_mm(self) -> Optional[float]:
        text = self.assay_vial_width_mm_var.get().strip()
        return float(text) if text else None

    def _first_non_none_image(self, *images):
        for image in images:
            if image is not None:
                return image
        return None

    def _assay_live_window_exists(self) -> bool:
        if self.assay_live_window is None:
            return False
        try:
            return bool(self.assay_live_window.winfo_exists())
        except tk.TclError:
            return False

    def _close_assay_live_window(self, _event=None) -> None:
        window = self.assay_live_window
        self.assay_live_window = None
        self.assay_live_canvas = None
        self._assay_live_presented_with_frame = False
        if window is None:
            return
        try:
            if window.winfo_exists():
                window.destroy()
        except tk.TclError:
            pass

    def _disable_assay_live_fullscreen(self, _event=None) -> None:
        self.assay_fullscreen_live_var.set(False)
        self._close_assay_live_window()

    def _present_assay_live_window(self, window: tk.Toplevel) -> None:
        try:
            window.deiconify()
        except tk.TclError:
            return
        try:
            screen_w = max(1, int(window.winfo_screenwidth()))
            screen_h = max(1, int(window.winfo_screenheight()))
            try:
                window.overrideredirect(True)
            except tk.TclError:
                pass
            window.geometry(f"{screen_w}x{screen_h}+0+0")
        except tk.TclError:
            pass
        try:
            window.attributes("-fullscreen", True)
        except tk.TclError:
            try:
                window.attributes("-zoomed", True)
            except tk.TclError:
                try:
                    window.state("zoomed")
                except tk.TclError:
                    pass
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            window.update_idletasks()
            window.lift()
            window.focus_force()
        except tk.TclError:
            return
        try:
            window.after(250, lambda: window.winfo_exists() and window.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _ensure_assay_live_window(self) -> None:
        if not self.assay_fullscreen_live_var.get():
            self._close_assay_live_window()
            return
        if self._assay_live_window_exists():
            self._present_assay_live_window(self.assay_live_window)
            return

        window = tk.Toplevel(self)
        window.title("Assay Live Tracker")
        window.configure(bg="#000000")
        window.bind("<Escape>", self._disable_assay_live_fullscreen)
        window.protocol("WM_DELETE_WINDOW", self._disable_assay_live_fullscreen)

        canvas = ImageCanvas(window, background="#000000")
        canvas.pack(fill="both", expand=True)
        canvas.set_empty_text("Waiting for assay tracker frames...")
        try:
            canvas.focus_set()
        except tk.TclError:
            pass

        self.assay_live_window = window
        self.assay_live_canvas = canvas
        self._present_assay_live_window(window)

    def _update_assay_live_window(self, image_bgr) -> None:
        if not self._assay_is_running() or not self.assay_fullscreen_live_var.get():
            self._close_assay_live_window()
            return
        if image_bgr is None:
            return
        self._ensure_assay_live_window()
        if self.assay_live_canvas is not None:
            self.assay_live_canvas.set_image_bgr(image_bgr)
        if self.assay_live_window is not None and not self._assay_live_presented_with_frame:
            self._present_assay_live_window(self.assay_live_window)
            self._assay_live_presented_with_frame = True

    def _build_current_assay_calibration(self) -> AssayCalibration:
        if self.assay_background_image is None:
            raise ValueError("Load or capture an assay background before saving or testing calibration.")
        regions = self.assay_canvas.get_regions()
        if not regions:
            raise ValueError("Define at least one assay region before saving or testing calibration.")
        expected_vials = self._expected_vial_count()
        if expected_vials > 0 and len(regions) != expected_vials:
            raise ValueError(f"Expected exactly {expected_vials} vial bounds, but found {len(regions)}. Use Guided vials or redraw the missing bounds.")
        vials = [
            region.to_vial(
                physical_index=idx,
                tube_height_mm=self._current_tube_height_mm(),
                tube_width_mm=self._current_tube_width_mm(),
            )
            for idx, region in enumerate(regions, start=1)
        ]
        return build_assay_calibration(
            background_bgr=self.assay_background_image,
            vials=vials,
            background_path=self.assay_background_var.get(),
            editor_mode="gui_editor",
            editor_meta={
                "expected_vials": int(expected_vials),
                "split_count": int(self.editor_split_count_var.get()),
                "view_mode": str(self.assay_view_var.get()),
                "group_ids_present": bool(any(region.group_id for region in regions)),
            },
        )

    def _render_calibration_snapshot(self, base_image=None):
        try:
            calibration_path = Path(self.assay_calibration_var.get())
            if calibration_path.exists() and not self.assay_calibration_dirty:
                calibration = load_assay_calibration(calibration_path)
            else:
                calibration = self._build_current_assay_calibration()
        except Exception:
            return base_image
        image = base_image
        if image is None:
            image = self.assay_last_preview_images.get("aligned")
        if image is None:
            image = self.assay_background_image
        if image is None:
            return None
        return render_assay_calibration_overlay(image, calibration)

    def _refresh_assay_display(self) -> None:
        view = self.assay_view_var.get()
        running = self._assay_is_running()
        self._set_assay_calibration_layout(view == "calibration" and not running)
        image = None
        if view == "annotated":
            image = self._first_non_none_image(self.assay_last_preview_images.get("annotated"), self._render_calibration_snapshot())
            self.assay_canvas.set_show_regions(False)
            self.assay_canvas.set_editor_enabled(False)
        elif view == "raw":
            image = self._first_non_none_image(self.assay_last_preview_images.get("raw"), self.assay_background_image)
            self.assay_canvas.set_show_regions(False)
            self.assay_canvas.set_editor_enabled(False)
        elif view == "mask":
            image = self._first_non_none_image(self.assay_last_preview_images.get("mask"), self._render_calibration_snapshot())
            self.assay_canvas.set_show_regions(False)
            self.assay_canvas.set_editor_enabled(False)
        else:
            if running:
                image = self._first_non_none_image(self.assay_last_preview_images.get("calibration"), self._render_calibration_snapshot())
                self.assay_canvas.set_show_regions(False)
                self.assay_canvas.set_editor_enabled(False)
            else:
                image = self._first_non_none_image(self.assay_last_preview_images.get("aligned"), self.assay_background_image)
                self.assay_canvas.set_show_regions(True)
                self.assay_canvas.set_editor_enabled(True)

        self.assay_canvas.set_image_bgr(image)

    def _load_assay_background_image(self, path: str | Path, silent: bool = False) -> None:
        path_obj = Path(path)
        image = _read_bgr(path_obj)
        self.assay_guided_target_count = 0
        old_shape = None if self.assay_background_image is None else self.assay_background_image.shape[:2]
        if old_shape is not None and old_shape != image.shape[:2] and self.assay_canvas.get_regions():
            keep = messagebox.askyesno(
                "Background size changed",
                "The new background dimensions do not match the current editor calibration.\n\nClear the existing assay regions and continue?",
            )
            if not keep:
                return
            self.assay_canvas.clear_regions()
        self.assay_background_image = image
        self.assay_background_path_loaded = str(path_obj.resolve())
        self.assay_background_var.set(str(path_obj.resolve()))
        self.assay_last_preview_images = {}
        self.assay_last_meta = {}
        self.assay_canvas.set_image_bgr(image)
        self.assay_canvas.set_empty_text("No assay background loaded.")
        self.assay_preview_info_var.set(f"Background loaded: {image.shape[1]}x{image.shape[0]}")
        self.assay_status_var.set("Background loaded")
        if not silent:
            self.log(f"Loaded assay background: {path_obj.resolve()}")
        self._refresh_assay_display()
        self._update_assay_ui_state()

    def load_assay_calibration_into_editor(self, silent: bool = False) -> bool:
        try:
            cal_path = Path(self.assay_calibration_var.get())
            if not cal_path.exists():
                raise FileNotFoundError(f"Calibration file not found: {cal_path}")
            calibration = load_assay_calibration(cal_path)

            bg_candidates = []
            if calibration.background_path:
                bg_candidates.append(Path(calibration.background_path))
            current_bg = Path(self.assay_background_var.get())
            if current_bg not in bg_candidates:
                bg_candidates.append(current_bg)

            loaded_bg = False
            for bg_path in bg_candidates:
                if bg_path and bg_path.exists():
                    self._load_assay_background_image(bg_path, silent=True)
                    loaded_bg = True
                    break
            if not loaded_bg and self.assay_background_image is None:
                raise FileNotFoundError("Could not locate a usable background image for this calibration.")

            if self.assay_background_image is not None:
                shape = [self.assay_background_image.shape[0], self.assay_background_image.shape[1]]
                if shape != calibration.image_shape_hw:
                    raise ValueError(
                        f"Calibration expects HxW={calibration.image_shape_hw}, but the loaded background is HxW={shape}."
                    )

            regions = [EditorRegion.from_vial(vial) for vial in calibration.vials]
            if calibration.vials:
                self.assay_expected_vials_var.set(len(calibration.vials))
                width_mm = next((vial.tube_width_mm for vial in calibration.vials if vial.tube_width_mm is not None), None)
                height_mm = next((vial.tube_height_mm for vial in calibration.vials if vial.tube_height_mm is not None), None)
                if width_mm is not None:
                    self.assay_vial_width_mm_var.set(f"{width_mm:g}")
                if height_mm is not None:
                    self.assay_height_mm_var.set(f"{height_mm:g}")
            self.assay_canvas.set_regions(regions, clear_history=True)
            self.assay_saved_state = self.assay_canvas.snapshot_state()
            self.assay_calibration_dirty = False
            self.assay_status_var.set("Calibration loaded")
            self.assay_view_var.set("calibration")
            self._refresh_assay_display()
            if not silent:
                self.log(f"Loaded assay calibration: {cal_path.resolve()}")
            return True
        except Exception as exc:
            if silent:
                raise
            self.log(f"[error] {exc}")
            messagebox.showerror("Load calibration error", str(exc))
            return False

    def save_assay_calibration_from_editor(self) -> bool:
        try:
            calibration = self._build_current_assay_calibration()
            path = Path(self.assay_calibration_var.get())
            if path.exists():
                overwrite = messagebox.askyesno("Overwrite calibration", f"Overwrite existing calibration file?\n\n{path}")
                if not overwrite:
                    return False
            save_json(path, calibration.to_dict())
            self.assay_saved_state = self.assay_canvas.snapshot_state()
            self.assay_calibration_dirty = False
            self.log(f"Saved assay calibration: {path.resolve()}")
            self.assay_status_var.set("Calibration saved")
            self._refresh_assay_display()
            self._update_assay_ui_state()
            return True
        except Exception as exc:
            self.log(f"[error] {exc}")
            messagebox.showerror("Save calibration error", str(exc))
            return False

    def reset_assay_editor(self) -> None:
        self.assay_guided_target_count = 0
        if self.assay_saved_state is not None:
            self.assay_canvas.load_state(self.assay_saved_state, clear_history=True)
            self.assay_calibration_dirty = False
            self.log("Reset assay editor to the last loaded or saved calibration.")
            self._refresh_assay_display()
        elif self.assay_canvas.get_regions():
            if messagebox.askyesno("Clear regions", "Clear all assay regions from the editor?"):
                self.assay_canvas.clear_regions()
                self.assay_calibration_dirty = True

    def save_assay_snapshot(self) -> None:
        view = self.assay_view_var.get()
        if view == "calibration":
            image = self._render_calibration_snapshot()
        elif view == "annotated":
            image = self.assay_last_preview_images.get("annotated")
        elif view == "mask":
            image = self.assay_last_preview_images.get("mask")
        else:
            image = self._first_non_none_image(self.assay_last_preview_images.get("raw"), self.assay_background_image)
        if image is None:
            messagebox.showinfo("No snapshot", "There is no image to snapshot yet.")
            return
        out_dir = Path(self.assay_output_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"preview_{view}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        cv2.imwrite(str(path), image)
        self.log(f"Saved preview snapshot: {path.resolve()}")

    def capture_channel_background(self) -> None:
        params = {
            "output_path": Path(self.channel_background_var.get()),
            "device": self.channel_device_var.get(),
            "width": self.channel_width_var.get(),
            "height": self.channel_height_var.get(),
            "fps": self.channel_fps_var.get(),
        }

        def worker():
            try:
                params["output_path"].parent.mkdir(parents=True, exist_ok=True)
                path = capture_brio_background_file(frame_count=15, **params)
                self.ui_queue.put(("channel_background_saved", path))
            except Exception as exc:
                self.ui_queue.put(("channel_status", f"Channel background capture failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def calibrate_channel(self) -> None:
        try:
            Path(self.channel_calibration_var.get()).parent.mkdir(parents=True, exist_ok=True)
            path = calibrate_channel(
                background_path=self.channel_background_var.get(),
                calibration_path=self.channel_calibration_var.get(),
                channel_mm=self.channel_mm_var.get(),
            )
            self.channel_status_var.set(f"Saved channel calibration: {path}")
            self._set_footer(f"Saved channel calibration: {path}")
        except Exception as exc:
            messagebox.showerror("Channel calibration error", str(exc))

    def detect_channel_once(self) -> None:
        params = {
            "device": self.channel_device_var.get(),
            "width": self.channel_width_var.get(),
            "height": self.channel_height_var.get(),
            "fps": self.channel_fps_var.get(),
            "background": self.channel_background_var.get(),
            "calibration": self.channel_calibration_var.get(),
            "score_thresh": self.channel_score_var.get(),
            "band_half_width": self.channel_band_var.get(),
            "output_dir": Path(self.channel_output_var.get()),
        }

        def worker():
            try:
                with BrioCamera(BrioConfig(device=params["device"], width=params["width"], height=params["height"], fps=params["fps"], role="channel")) as camera:
                    frame = camera.read()
                result, annotated, mask = process_fly_detection(
                    background=params["background"],
                    frame=frame,
                    calibration_path=params["calibration"],
                    score_thresh=params["score_thresh"],
                    band_half_width=params["band_half_width"],
                )
                params["output_dir"].mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(params["output_dir"] / "last_channel_annotated.png"), annotated)
                cv2.imwrite(str(params["output_dir"] / "last_channel_mask.png"), mask)
                with open(params["output_dir"] / "last_channel_result.json", "w", encoding="utf-8") as fh:
                    json.dump(result, fh, indent=2)
                self.ui_queue.put(("channel_preview", annotated, result))
            except Exception as exc:
                self.ui_queue.put(("channel_status", f"Channel detect error: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def start_channel_live(self) -> None:
        if self._channel_is_running():
            self.channel_status_var.set("Channel live is already running.")
            return
        params = {
            "device": self.channel_device_var.get(),
            "width": self.channel_width_var.get(),
            "height": self.channel_height_var.get(),
            "fps": self.channel_fps_var.get(),
            "background": self.channel_background_var.get(),
            "calibration": self.channel_calibration_var.get(),
            "score_thresh": self.channel_score_var.get(),
            "band_half_width": self.channel_band_var.get(),
        }
        self.channel_stop_event = threading.Event()

        def worker():
            try:
                with BrioCamera(BrioConfig(device=params["device"], width=params["width"], height=params["height"], fps=params["fps"], role="channel")) as camera:
                    while not self.channel_stop_event.is_set():
                        frame = camera.read()
                        result, annotated, _mask = process_fly_detection(
                            background=params["background"],
                            frame=frame,
                            calibration_path=params["calibration"],
                            score_thresh=params["score_thresh"],
                            band_half_width=params["band_half_width"],
                        )
                        self.ui_queue.put(("channel_preview", annotated, result))
            except Exception as exc:
                self.ui_queue.put(("channel_status", f"Channel live error: {exc}"))

        self.channel_thread = threading.Thread(target=worker, daemon=True)
        self.channel_thread.start()
        self.channel_status_var.set("Channel live started.")

    def stop_channel_live(self) -> None:
        if self.channel_stop_event is not None:
            self.channel_stop_event.set()
        self.channel_status_var.set("Channel live stopped.")

    def capture_assay_background(self) -> None:
        target_path = self._default_project_paths()["assay_background_var"]
        self.assay_background_var.set(str(target_path))
        params = {
            "output_path": target_path,
            "width": self.assay_width_var.get(),
            "height": self.assay_height_var.get(),
            "fps": self.assay_fps_var.get(),
            "camera_backend": self.assay_camera_backend_var.get(),
            "camera_device": self.assay_camera_device_var.get(),
            "camera_index": self.assay_camera_index_var.get(),
        }

        def worker():
            try:
                params["output_path"].parent.mkdir(parents=True, exist_ok=True)
                path = capture_assay_background_file(frame_count=25, **params)
                self.ui_queue.put(("assay_background_saved", path))
            except Exception as exc:
                self.ui_queue.put(("assay_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def calibrate_assay_external(self) -> None:
        try:
            background_path = Path(self.assay_background_var.get())
            if self.assay_background_image is None and not background_path.exists():
                raise ValueError("Load or capture an assay background before calibrating.")
            if self.assay_background_image is not None:
                background_path.parent.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(background_path), self.assay_background_image):
                    raise IOError(f"Could not save assay background to {background_path}")

            calibration_path = Path(self.assay_calibration_var.get())
            calibration_path.parent.mkdir(parents=True, exist_ok=True)

            self.assay_freeze_var.set(False)
            self.assay_view_var.set("calibration")
            self._set_assay_calibration_layout(True)
            self.log("Opening assay calibration window. Use B for box mode or P for 4-point mode, ENTER to accept, and R to reset.")
            calibrate_assay_interactive(
                background_path=background_path,
                output_json=calibration_path,
                total_vials=self._expected_vial_count(),
                ignored_physical_indices=[],
                tube_height_mm=self._current_tube_height_mm(),
                tube_width_mm=self._current_tube_width_mm(),
            )
            self.load_assay_calibration_into_editor(silent=True)
            self.assay_status_var.set("Calibration loaded")
            self.assay_view_var.set("calibration")
            self.log(f"Saved assay calibration: {calibration_path.resolve()}")
            self._refresh_assay_display()
            self._update_assay_ui_state()
        except KeyboardInterrupt:
            self.log("Assay calibration cancelled.")
        except Exception as exc:
            self.log(f"[error] {exc}")
            messagebox.showerror("Assay calibration error", str(exc))

    def test_assay_on_frame(self, force_view: Optional[str] = None) -> None:
        try:
            if self.assay_background_image is None:
                raise ValueError("Load or capture an assay background before previewing calibration.")
            calibration_path = Path(self.assay_calibration_var.get())
            if calibration_path.exists() and not self.assay_calibration_dirty:
                calibration = load_assay_calibration(calibration_path)
            else:
                calibration = self._build_current_assay_calibration()

            params = {
                "background_bgr": self.assay_background_image.copy(),
                "calibration": calibration,
                "width": self.assay_width_var.get(),
                "height": self.assay_height_var.get(),
                "fps": self.assay_fps_var.get(),
                "camera_backend": self.assay_camera_backend_var.get(),
                "camera_device": self.assay_camera_device_var.get(),
                "camera_index": self.assay_camera_index_var.get(),
                "min_area": self.assay_min_area_var.get(),
                "max_area": self.assay_max_area_var.get(),
                "min_threshold": self.assay_threshold_var.get(),
                "inner_margin_px": self.assay_margin_var.get(),
                "max_flies_per_vial": self.assay_max_flies_var.get(),
                "no_align": self.assay_no_align_var.get(),
                "show_positions": self.assay_show_xy_overlay_var.get(),
            }
        except Exception as exc:
            self.log(f"[error] {exc}")
            messagebox.showerror("Preview error", str(exc))
            return

        def worker():
            try:
                frame = capture_assay_frame_once(
                    width=params["width"],
                    height=params["height"],
                    fps=params["fps"],
                    camera_backend=params["camera_backend"],
                    camera_device=params["camera_device"],
                    camera_index=params["camera_index"],
                )
                preview_images, rows, meta = preview_assay_frame(
                    background_bgr=params["background_bgr"],
                    frame_bgr=frame,
                    calibration=params["calibration"],
                    min_area=params["min_area"],
                    max_area=params["max_area"],
                    min_threshold=params["min_threshold"],
                    inner_margin_px=params["inner_margin_px"],
                    max_flies_per_vial=params["max_flies_per_vial"],
                    no_align=params["no_align"],
                    show_positions=params["show_positions"],
                )
                self.ui_queue.put(("assay_preview", preview_images, rows, meta))
            except Exception as exc:
                self.ui_queue.put(("assay_error", str(exc)))

        if force_view:
            self.assay_view_var.set(force_view)
        threading.Thread(target=worker, daemon=True).start()
        self.log("Captured one frame for assay calibration preview.")

    def _ensure_assay_calibration_path(self) -> Optional[Path]:
        path = Path(self.assay_calibration_var.get())
        if path.exists() and not self.assay_calibration_dirty:
            return path

        if self.assay_calibration_dirty:
            choice = messagebox.askyesnocancel(
                "Unsaved calibration",
                "The assay calibration has unsaved changes.\n\nSave the current calibration before starting the assay?",
            )
            if choice is None:
                return None
            if choice:
                if not self.save_assay_calibration_from_editor():
                    return None
                return Path(self.assay_calibration_var.get())
            if not path.exists():
                raise ValueError("Save the calibration before starting the assay.")
            return path

        if not path.exists():
            if not self.save_assay_calibration_from_editor():
                return None
        return path

    def start_assay(self) -> None:
        try:
            if self._assay_is_running():
                self.log("Assay is already running.")
                return

            calibration_path = self._ensure_assay_calibration_path()
            if calibration_path is None:
                return

            requested_fps = float(self.assay_fps_var.get())
            live_fps = max(8.0, requested_fps)
            params = {
                "background_path": self.assay_background_var.get(),
                "calibration_path": str(calibration_path),
                "output_dir": self.assay_output_var.get(),
                "seconds": self.assay_seconds_var.get(),
                "fps": live_fps,
                "camera_width": self.assay_width_var.get(),
                "camera_height": self.assay_height_var.get(),
                "camera_backend": self.assay_camera_backend_var.get(),
                "camera_device": self.assay_camera_device_var.get(),
                "camera_index": self.assay_camera_index_var.get(),
                "min_area": self.assay_min_area_var.get(),
                "max_area": self.assay_max_area_var.get(),
                "min_threshold": self.assay_threshold_var.get(),
                "inner_margin_px": self.assay_margin_var.get(),
                "max_flies_per_vial": self.assay_max_flies_var.get(),
                "snapshot_interval_s": self.assay_snapshot_var.get(),
                "no_align": self.assay_no_align_var.get(),
                "show_positions": self.assay_show_xy_overlay_var.get(),
            }
        except Exception as exc:
            self.log(f"[error] {exc}")
            messagebox.showerror("Assay start error", str(exc))
            return

        self.assay_stop_event = threading.Event()
        self._assay_stop_requested = False
        self.assay_status_var.set("Assay running")
        self.assay_view_var.set("annotated")
        self._assay_live_presented_with_frame = False
        self._last_assay_tree_update_s = 0.0
        self._last_assay_display_update_s = 0.0
        with self._assay_preview_lock:
            self._pending_assay_preview = None
        if requested_fps < live_fps:
            self.log(f"Raised assay FPS from {requested_fps:g} to {live_fps:g} for smoother live tracking.")
        self.log("Assay started.")

        def worker():
            try:
                result = run_assay_session(
                    background_path=params["background_path"],
                    calibration_path=params["calibration_path"],
                    output_dir=params["output_dir"],
                    seconds=params["seconds"],
                    fps=params["fps"],
                    camera_width=params["camera_width"],
                    camera_height=params["camera_height"],
                    camera_backend=params["camera_backend"],
                    camera_device=params["camera_device"],
                    camera_index=params["camera_index"],
                    min_area=params["min_area"],
                    max_area=params["max_area"],
                    min_threshold=params["min_threshold"],
                    inner_margin_px=params["inner_margin_px"],
                    max_flies_per_vial=params["max_flies_per_vial"],
                    snapshot_interval_s=params["snapshot_interval_s"],
                    no_align=params["no_align"],
                    show_positions=params["show_positions"],
                    preview_callback=self._assay_preview_callback,
                    stop_event=self.assay_stop_event,
                )
                stopped = bool(self.assay_stop_event is not None and self.assay_stop_event.is_set())
                self.ui_queue.put(("assay_stopped" if stopped else "assay_done", result))
            except Exception as exc:
                self.ui_queue.put(("assay_error", str(exc)))

        self.assay_thread = threading.Thread(target=worker, daemon=True)
        self.assay_thread.start()
        self._update_assay_ui_state()
        if self.assay_fullscreen_live_var.get():
            self.after(0, self._ensure_assay_live_window)
            self.after(180, self._ensure_assay_live_window)

    def _assay_preview_callback(self, preview_images, rows, meta) -> None:
        if self._assay_stop_requested or (self.assay_stop_event is not None and self.assay_stop_event.is_set()):
            return
        with self._assay_preview_lock:
            self._pending_assay_preview = (preview_images, rows, meta)

    def stop_assay(self) -> None:
        if self.assay_stop_event is not None:
            self._assay_stop_requested = True
            self.assay_stop_event.set()
            with self._assay_preview_lock:
                self._pending_assay_preview = None
            self.assay_status_var.set("Stopping assay")
            self._close_assay_live_window()
            self._update_assay_ui_state()
            self.log("Assay stop requested.")

    def _on_close(self) -> None:
        self._save_settings()
        self._close_assay_live_window()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
