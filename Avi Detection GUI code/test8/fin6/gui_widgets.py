#!/usr/bin/env python3
"""
Reusable Tkinter image widgets for the fin6 GUI.

These are adapted from the original assay editor so the refactored GUI keeps a
familiar direct-manipulation calibration workflow while also exposing the new
threshold line.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import tkinter as tk
from PIL import Image, ImageTk  # type: ignore

from assay_tracking import VialCalibration


Point = Tuple[int, int]


def read_bgr(path: str | Path):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    return image


@dataclass
class EditorRegion:
    x: int
    y: int
    w: int
    h: int
    top_y: int
    baseline_y: int
    threshold_y: int
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
            threshold_y=int(vial.threshold_y),
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
            threshold_point_px=[int(self.center_x), int(self.threshold_y)],
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
        self.bind("<Delete>", lambda _event: self._handle_delete_key())
        self.bind("<BackSpace>", lambda _event: self._handle_delete_key())
        self.bind("<Escape>", lambda _event: self._handle_cancel_key())
        self.bind("<Control-z>", lambda _event: self._handle_undo_key())
        self.bind("<Control-y>", lambda _event: self._handle_redo_key())
        self.bind("<Up>", lambda _event: self._handle_threshold_nudge_key(-1))
        self.bind("<Down>", lambda _event: self._handle_threshold_nudge_key(1))
        self.bind("<Shift-Up>", lambda _event: self._handle_threshold_nudge_key(-5))
        self.bind("<Shift-Down>", lambda _event: self._handle_threshold_nudge_key(5))

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

    def current_region(self) -> Optional[EditorRegion]:
        if self.selected_index is None or not (0 <= self.selected_index < len(self.regions)):
            return None
        return copy.deepcopy(self.regions[self.selected_index])

    def update_selected_region(self, **kwargs: Any) -> None:
        if self.selected_index is None or not (0 <= self.selected_index < len(self.regions)):
            return
        self._push_undo()
        region = self.regions[self.selected_index]
        for key, value in kwargs.items():
            if not hasattr(region, key):
                continue
            setattr(region, key, value)
        self._normalize_region(region)
        self.render()
        self._emit_change()
        self._emit_select()

    def _normalize_region(self, region: EditorRegion) -> None:
        if self._source_bgr is None:
            return
        img_h, img_w = self._source_bgr.shape[:2]
        region.w = max(12, int(region.w))
        region.h = max(12, int(region.h))
        region.x = max(0, min(img_w - region.w, int(region.x)))
        region.y = max(0, min(img_h - region.h, int(region.y)))
        region.top_y = max(region.y, min(region.bottom - 2, int(region.top_y)))
        region.baseline_y = max(region.top_y + 1, min(region.bottom - 1, int(region.baseline_y)))
        region.threshold_y = max(region.top_y + 1, min(region.baseline_y - 1, int(region.threshold_y)))

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
            self._status("Select a vial to move, resize, or edit top / threshold / baseline lines.")

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

    def delete_last(self) -> None:
        if not self.regions:
            self._status("There are no regions to delete.")
            return
        self._push_undo()
        removed = self.regions.pop()
        if self.selected_index is not None and self.selected_index >= len(self.regions):
            self.selected_index = len(self.regions) - 1 if self.regions else None
        self.render()
        self._emit_change()
        self._emit_select()
        label = removed.label or f"Vial {len(self.regions) + 1}"
        self._status(f"Deleted last region: {label}.")

    def nudge_selected_reference(self, which: str, delta_px: int) -> None:
        if self.selected_index is None or not (0 <= self.selected_index < len(self.regions)):
            self._status("Select a region first.")
            return
        which = str(which or "").strip().lower()
        mapping = {
            "top": "top_y",
            "threshold": "threshold_y",
            "baseline": "baseline_y",
        }
        attr = mapping.get(which)
        if attr is None:
            raise ValueError(f"Unknown reference line: {which}")
        self._push_undo()
        region = self.regions[self.selected_index]
        setattr(region, attr, int(getattr(region, attr)) + int(delta_px))
        region.quad_points_px = None
        self._normalize_region(region)
        self.render()
        self._emit_change()
        self._emit_select()
        self._status(f"Moved {which} by {int(delta_px)} px.")

    def _handle_delete_key(self):
        self.delete_selected()
        return "break"

    def _handle_cancel_key(self):
        self.cancel_current_action()
        return "break"

    def _handle_undo_key(self):
        self.undo()
        return "break"

    def _handle_redo_key(self):
        self.redo()
        return "break"

    def _handle_threshold_nudge_key(self, delta_px: int):
        self.nudge_selected_reference("threshold", delta_px)
        return "break"

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
        self._normalize_region(source)
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
        thr_ratio = (ref.threshold_y - ref.y) / max(1.0, float(ref.h))
        base_ratio = (ref.baseline_y - ref.y) / max(1.0, float(ref.h))
        for region in self.regions:
            region.top_y = int(round(region.y + top_ratio * region.h))
            region.threshold_y = int(round(region.y + thr_ratio * region.h))
            region.baseline_y = int(round(region.y + base_ratio * region.h))
            self._normalize_region(region)
        self.render()
        self._emit_change()
        self._status("Applied top, threshold, and baseline style to all vials.")

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
        tx0, top_y = self.image_to_canvas(region.x, region.top_y)
        tx1, _ = self.image_to_canvas(region.right, region.top_y)
        hx0, thr_y = self.image_to_canvas(region.x, region.threshold_y)
        hx1, _ = self.image_to_canvas(region.right, region.threshold_y)
        bx0, base_y = self.image_to_canvas(region.x, region.baseline_y)
        bx1, _ = self.image_to_canvas(region.right, region.baseline_y)
        top_width = 3 if selected else 2
        thr_width = 4 if selected else 2
        base_width = 3 if selected else 2
        self.create_line(tx0, top_y, tx1, top_y, fill="#ffcd38", width=top_width)
        self.create_line(hx0, thr_y, hx1, thr_y, fill="#ff5abf", width=thr_width)
        self.create_line(bx0, base_y, bx1, base_y, fill="#72e06a", width=base_width)
        if selected:
            cx_thr, cy_thr = self.image_to_canvas(region.center_x, region.threshold_y)
            self.create_oval(cx_thr - 7, cy_thr - 7, cx_thr + 7, cy_thr + 7, fill="#ff5abf", outline="#ffffff", width=2)

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
                self.create_rectangle(px - 5, py - 5, px + 5, py + 5, fill="#ffffff", outline="#0f172a", width=1)

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
        threshold_y = int(round((top_y + baseline_y) / 2.0))
        return EditorRegion(
            x=int(x),
            y=int(y),
            w=int(w),
            h=int(h),
            top_y=int(top_y),
            baseline_y=int(baseline_y),
            threshold_y=int(threshold_y),
            enabled=enabled,
            label="",
            group_id=group_id,
        )

    def _hit_test(self, x: int, y: int) -> Tuple[Optional[str], Any]:
        tolerance = max(6, int(round(12 / max(1e-6, self.get_scale()))))
        line_tolerance = max(tolerance + 2, int(round(18 / max(1e-6, self.get_scale()))))

        for left_idx, right_idx in self._group_dividers():
            left = self.regions[left_idx]
            right = self.regions[right_idx]
            if abs(x - left.right) <= tolerance and max(left.y, right.y) <= y <= min(left.bottom, right.bottom):
                return "divider", (left_idx, right_idx)

        if self.selected_index is not None and 0 <= self.selected_index < len(self.regions):
            region = self.regions[self.selected_index]
            if region.x - tolerance <= x <= region.right + tolerance:
                if abs(y - region.top_y) <= line_tolerance:
                    return "top", self.selected_index
                if abs(y - region.threshold_y) <= line_tolerance:
                    return "threshold", self.selected_index
                if abs(y - region.baseline_y) <= line_tolerance:
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
        self.focus_set()
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
            move_dy = region.y - old_y
            move_dx = region.x - old_x
            region.top_y += move_dy
            region.threshold_y += move_dy
            region.baseline_y += move_dy
            if region.quad_points_px:
                moved_points: List[List[int]] = []
                for px, py in region.quad_points_px:
                    moved_points.append([
                        max(0, min(img_w - 1, int(px) + move_dx)),
                        max(0, min(img_h - 1, int(py) + move_dy)),
                    ])
                region.quad_points_px = moved_points
            self._normalize_region(region)

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
            region.quad_points_px = None
            self._normalize_region(region)

        elif kind == "top" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            region.top_y = int(cur_y)
            region.quad_points_px = None
            self._normalize_region(region)

        elif kind == "threshold" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            region.threshold_y = int(cur_y)
            region.quad_points_px = None
            self._normalize_region(region)

        elif kind == "baseline" and self.selected_index is not None:
            region = self.regions[self.selected_index]
            region.baseline_y = int(cur_y)
            region.quad_points_px = None
            self._normalize_region(region)

        elif kind == "divider":
            left_idx, right_idx = self._drag["payload"]
            left = self.regions[left_idx]
            right = self.regions[right_idx]
            right_edge = right.x + right.w
            boundary = max(left.x + 12, min(right.right - 12, cur_x))
            left.w = int(boundary - left.x)
            right.x = int(boundary)
            right.w = int(max(12, right_edge - boundary))
            left.quad_points_px = None
            right.quad_points_px = None
            self._normalize_region(left)
            self._normalize_region(right)

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
                    next_right = x + w if idx == self.split_count - 1 else int(round(x + lane_width * (idx + 1)))
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
