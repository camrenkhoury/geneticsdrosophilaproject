#!/usr/bin/env python3
"""Tkinter control GUI for the Drosophila genetics system."""

from __future__ import annotations

import contextlib
import ctypes
import io
import importlib
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, scrolledtext, ttk

GUI_DIR = Path(__file__).resolve().parent
HOST_APP_DIR = GUI_DIR.parent
REPO_ROOT = HOST_APP_DIR.parent
CODE_DIR = REPO_ROOT / "CodeDirectory"
LINUX_LAUNCHER_DIR = HOST_APP_DIR / "launchers" / "linux"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from host_app.controllers.base_controller import (
    ControllerCommandRejected,
    ControllerConnectionError,
    ControllerError,
)
from host_app.controllers.remote_controller import RemoteController
from host_app.gui.camera_role_panel import CameraRoleActions, CameraRolePanel
from host_app.gui.channel_setup_panel import ChannelSetupActions, ChannelSetupPanel
from host_app.sync.connection_state import ConnectionState
from host_app.sync.remote_sync import RemoteSyncManager
from shared.config.network_config import (
    RemoteConnectionSettings,
    load_remote_connection_settings,
    save_remote_connection_settings,
)
from shared.config.project_paths import CHANNEL_OUTPUT_DIR, FIN6_DIR


class TaskCancelled(Exception):
    """Raised when the operator stops an automated run."""


class OperatorChoiceDialog:
    def __init__(self, parent: tk.Misc, *, title: str, message: str, primary_text: str, cancel_text: str = "Cancel"):
        self.result = False
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

        frame = ttk.Frame(self.window, padding=16)
        frame.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text=message, justify="left", wraplength=420).grid(
            row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 14)
        )

        button_row = ttk.Frame(frame)
        button_row.grid(row=1, column=0, sticky=tk.E)
        ttk.Button(button_row, text=primary_text, command=self._accept).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_row, text=cancel_text, command=self._cancel).grid(row=0, column=1)

        self.window.update_idletasks()
        try:
            parent.update_idletasks()
            parent_x = parent.winfo_rootx()
            parent_y = parent.winfo_rooty()
            parent_w = parent.winfo_width()
            parent_h = parent.winfo_height()
            width = self.window.winfo_width()
            height = self.window.winfo_height()
            pos_x = parent_x + max(0, (parent_w - width) // 2)
            pos_y = parent_y + max(0, (parent_h - height) // 2)
            self.window.geometry(f"+{pos_x}+{pos_y}")
        except Exception:
            pass
        self.window.focus_force()

    def _accept(self) -> None:
        self.result = True
        self.window.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.window.destroy()


@dataclass(frozen=True)
class GUIPlatformProfile:
    is_macos: bool
    use_zoomed_window: bool
    window_width_ratio: float
    window_height_ratio: float
    window_margin_x: int
    window_margin_y: int
    entry_page_scale: float
    entry_fly_column_min: int
    entry_fly_column_pad: int
    entry_fly_max: int
    operations_layout: str
    operations_logo_column_min: int
    operations_logo_size: int
    operations_button_gap: int
    operations_button_margin: int
    standard_button_pady: int
    footer_banner_width: int
    footer_banner_height: int
    small_screen_threshold_w: int
    small_screen_threshold_h: int
    small_entry_scale: float
    small_entry_fly_max: int
    small_entry_fly_column_min: int
    small_footer_banner_width: int
    small_footer_banner_height: int
    small_operations_logo_size: int
    small_operations_logo_column_min: int
    small_operations_button_gap: int
    small_operations_button_margin: int
    small_standard_button_pady: int
    tiny_screen_threshold_w: int
    tiny_screen_threshold_h: int
    tiny_entry_scale: float
    tiny_entry_fly_max: int
    tiny_entry_fly_column_min: int
    tiny_footer_banner_width: int
    tiny_footer_banner_height: int
    tiny_operations_logo_size: int
    tiny_operations_logo_column_min: int
    tiny_operations_button_gap: int
    tiny_operations_button_margin: int
    tiny_standard_button_pady: int


def build_gui_platform_profile() -> GUIPlatformProfile:
    is_macos = sys.platform == "darwin"
    if is_macos:
        return GUIPlatformProfile(
            is_macos=True,
            use_zoomed_window=False,
            window_width_ratio=0.94,
            window_height_ratio=0.92,
            window_margin_x=40,
            window_margin_y=70,
            entry_page_scale=1.48,
            entry_fly_column_min=308,
            entry_fly_column_pad=4,
            entry_fly_max=300,
            operations_layout="stacked",
            operations_logo_column_min=142,
            operations_logo_size=112,
            operations_button_gap=8,
            operations_button_margin=8,
            standard_button_pady=5,
            footer_banner_width=260,
            footer_banner_height=74,
            small_screen_threshold_w=1280,
            small_screen_threshold_h=800,
            small_entry_scale=1.28,
            small_entry_fly_max=240,
            small_entry_fly_column_min=256,
            small_footer_banner_width=210,
            small_footer_banner_height=58,
            small_operations_logo_size=92,
            small_operations_logo_column_min=124,
            small_operations_button_gap=6,
            small_operations_button_margin=6,
            small_standard_button_pady=4,
            tiny_screen_threshold_w=1100,
            tiny_screen_threshold_h=650,
            tiny_entry_scale=1.12,
            tiny_entry_fly_max=200,
            tiny_entry_fly_column_min=220,
            tiny_footer_banner_width=180,
            tiny_footer_banner_height=50,
            tiny_operations_logo_size=72,
            tiny_operations_logo_column_min=110,
            tiny_operations_button_gap=4,
            tiny_operations_button_margin=4,
            tiny_standard_button_pady=3,
        )
    return GUIPlatformProfile(
        is_macos=False,
        use_zoomed_window=True,
        window_width_ratio=0.94,
        window_height_ratio=0.92,
        window_margin_x=40,
        window_margin_y=70,
        entry_page_scale=1.54,
        entry_fly_column_min=300,
        entry_fly_column_pad=8,
        entry_fly_max=372,
        operations_layout="side_by_side",
        operations_logo_column_min=150,
        operations_logo_size=120,
        operations_button_gap=12,
        operations_button_margin=20,
        standard_button_pady=7,
        footer_banner_width=312,
        footer_banner_height=88,
        small_screen_threshold_w=1280,
        small_screen_threshold_h=800,
        small_entry_scale=1.35,
        small_entry_fly_max=280,
        small_entry_fly_column_min=260,
        small_footer_banner_width=240,
        small_footer_banner_height=68,
        small_operations_logo_size=96,
        small_operations_logo_column_min=130,
        small_operations_button_gap=8,
        small_operations_button_margin=8,
        small_standard_button_pady=5,
        tiny_screen_threshold_w=1100,
        tiny_screen_threshold_h=650,
        tiny_entry_scale=1.18,
        tiny_entry_fly_max=230,
        tiny_entry_fly_column_min=240,
        tiny_footer_banner_width=200,
        tiny_footer_banner_height=56,
        tiny_operations_logo_size=72,
        tiny_operations_logo_column_min=110,
        tiny_operations_button_gap=6,
        tiny_operations_button_margin=6,
        tiny_standard_button_pady=4,
    )


class QueueWriter:
    """Line-buffered writer that forwards backend print output to the GUI log."""

    def __init__(self, ui_queue: queue.Queue):
        self.ui_queue = ui_queue
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self.ui_queue.put(("log", line))
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self.ui_queue.put(("log", self._buffer.strip()))
        self._buffer = ""


class SliderSwitch(tk.Canvas):
    """Modern on/off switch used for device control."""

    def __init__(
        self,
        parent,
        command=None,
        initial=False,
        width=78,
        height=38,
        **kwargs,
    ):
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0, **kwargs)
        self.command = command
        self.value = bool(initial)
        self.enabled = True
        self.width = width
        self.height = height
        self._pressed = False
        self.configure(cursor="hand2")
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Leave>", self._on_leave)
        self.draw()

    def _toggle(self) -> None:
        if not self.enabled:
            return
        self.value = not self.value
        self.draw()
        if callable(self.command):
            self.command(self.value)

    def _on_press(self, _event=None) -> None:
        if not self.enabled:
            return
        self._pressed = True

    def _on_release(self, event=None) -> None:
        if not self.enabled:
            self._pressed = False
            return
        was_pressed = self._pressed
        self._pressed = False
        if not was_pressed or event is None:
            return
        if 0 <= event.x <= self.width and 0 <= event.y <= self.height:
            self._toggle()

    def _on_leave(self, _event=None) -> None:
        self._pressed = False

    def set_value(self, value: bool) -> None:
        self.value = bool(value)
        self.draw()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self.draw()

    def draw(self):
        self.delete("all")

        if not self.enabled:
            track_fill = "#D8E0E8"
            track_outline = "#CCD5DE"
            knob_fill = "#F4F7FA"
            knob_outline = "#C4CDD7"
        elif self.value:
            track_fill = "#1F8A70"
            track_outline = "#176A57"
            knob_fill = "#FFFFFF"
            knob_outline = "#D9E2EC"
        else:
            track_fill = "#D4DCE5"
            track_outline = "#B7C2CE"
            knob_fill = "#FFFFFF"
            knob_outline = "#C5CED8"

        inset = 2
        radius = (self.height - (inset * 2)) / 2
        left = inset
        top = inset
        right = self.width - inset
        bottom = self.height - inset

        self.create_oval(left, top, left + (radius * 2), bottom, fill=track_fill, outline=track_outline, width=1)
        self.create_oval(
            right - (radius * 2),
            top,
            right,
            bottom,
            fill=track_fill,
            outline=track_outline,
            width=1,
        )
        self.create_rectangle(
            left + radius,
            top,
            right - radius,
            bottom,
            fill=track_fill,
            outline=track_outline,
            width=1,
        )

        knob_padding = 4
        knob_size = self.height - (knob_padding * 2)
        knob_x = self.width - knob_size - knob_padding if self.value else knob_padding
        self.create_oval(
            knob_x,
            knob_padding,
            knob_x + knob_size,
            knob_padding + knob_size,
            fill=knob_fill,
            outline=knob_outline,
            width=1,
        )


class ActionButton(tk.Canvas):
    """Cross-platform colored button that does not rely on native Tk button theming."""

    def __init__(
        self,
        parent,
        text: str,
        color: str,
        command=None,
        *,
        font=("Arial", 10, "bold"),
        padx=12,
        pady=7,
        disabled_color="#B0B7C3",
        active_color=None,
        **kwargs,
    ):
        super().__init__(parent, highlightthickness=0, bd=0, relief="flat", **kwargs)
        self.command = command
        self._text = text
        self._base_color = color
        self._active_color = active_color or color
        self._disabled_color = disabled_color
        self._fg = "#FFFFFF"
        self._font = tkfont.Font(font=font)
        self._padx = padx
        self._pady = pady
        self._state = tk.NORMAL
        self._hovered = False
        self._pressed = False
        self.configure(cursor="hand2")
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", self._on_resize)
        self._redraw()

    def _current_fill(self) -> str:
        if self._state != tk.NORMAL:
            return self._disabled_color
        if self._pressed:
            return self._active_color
        if self._hovered:
            return self._active_color
        return self._base_color

    def _outline_color(self) -> str:
        color = self._current_fill().lstrip("#")
        red = max(0, int(color[0:2], 16) - 28)
        green = max(0, int(color[2:4], 16) - 28)
        blue = max(0, int(color[4:6], 16) - 28)
        return f"#{red:02X}{green:02X}{blue:02X}"

    def _redraw(self) -> None:
        self.delete("all")
        text_width = self._font.measure(self._text)
        text_height = self._font.metrics("linespace")
        requested_width = text_width + (self._padx * 2)
        requested_height = text_height + (self._pady * 2)
        width = max(requested_width, self.winfo_width(), 1)
        height = max(requested_height, self.winfo_height(), 1)
        super().configure(width=width, height=height, cursor="hand2" if self._state == tk.NORMAL else "arrow")
        fill = self._current_fill()
        outline = self._outline_color()
        self.create_rectangle(0, 0, width, height, fill=fill, outline=outline, width=2)
        self.create_text(
            width / 2,
            height / 2,
            text=self._text,
            fill=self._fg if self._state == tk.NORMAL else "#F5F7FA",
            font=self._font,
        )

    def _on_press(self, _event=None) -> None:
        if self._state != tk.NORMAL:
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, event=None) -> None:
        if self._state != tk.NORMAL:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if not was_pressed or event is None:
            return
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if callable(self.command):
                self.command()

    def _on_enter(self, _event=None) -> None:
        if self._state == tk.NORMAL:
            self._hovered = True
            self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._pressed = False
        if self._hovered:
            self._hovered = False
            self._redraw()

    def _on_resize(self, _event=None) -> None:
        self._redraw()

    def config(self, cnf=None, **kwargs):
        self.configure(cnf, **kwargs)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        redraw_needed = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            redraw_needed = True
        if "command" in kwargs:
            self.command = kwargs.pop("command")
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            redraw_needed = True
        if "bg" in kwargs:
            self._base_color = kwargs.pop("bg")
            redraw_needed = True
        if "activebackground" in kwargs:
            self._active_color = kwargs.pop("activebackground")
            redraw_needed = True
        if "fg" in kwargs:
            self._fg = kwargs.pop("fg")
            redraw_needed = True
        if "font" in kwargs:
            self._font = tkfont.Font(font=kwargs.pop("font"))
            redraw_needed = True
        if "padx" in kwargs:
            self._padx = kwargs.pop("padx")
            redraw_needed = True
        if "pady" in kwargs:
            self._pady = kwargs.pop("pady")
            redraw_needed = True
        super().configure(**kwargs)
        if redraw_needed:
            self._redraw()

    configure = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        if key == "bg":
            return self._base_color
        return super().cget(key)


class DrosophilaGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._configure_windows_app_id()
        self.root.title("Drosophila Genetics Control Panel")
        self.root.geometry("1100x760")
        self.root.minsize(960, 680)
        self.gui_profile = build_gui_platform_profile()
        self.is_macos = self.gui_profile.is_macos
        self._capture_screen_metrics()
        self._configure_entry_profile()
        if self.gui_profile.use_zoomed_window:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                self.root.after(10, self._fit_window_to_screen)
        else:
            self.root.after(10, self._fit_window_to_screen)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.gui_dir = GUI_DIR
        self.host_app_dir = HOST_APP_DIR
        self.code_dir = CODE_DIR
        self.linux_launcher_dir = LINUX_LAUNCHER_DIR
        self.repo_root = REPO_ROOT
        self.ui_queue: queue.Queue = queue.Queue()
        self.stop_requested = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.current_task_name: str | None = None
        self.current_task_cancellable = False
        self.local_task_busy = False
        self.local_task_cancellable = False
        self.remote_request_in_flight = False
        self.remote_connected = False
        self.remote_backend_busy = False
        self.remote_stop_allowed = False
        self.remote_backend_degraded = False
        self.remote_home_calibration_required = False
        self.remote_home_prompt_scheduled = False
        self.remote_motion_available = True
        self.remote_vacuum_available = True
        self.remote_vibration_available = True
        self.remote_classifier_available = True
        self.remote_assay_available = True
        self.remote_seen_log_keys: set[tuple[str, str, str]] = set()
        self._last_remote_status_revision: int | None = None
        self._last_remote_preview_source_mtime: float | None = None
        self._remote_preview_fetch_in_flight = False
        self._last_remote_classification_signature: tuple[str, float, tuple[str, ...]] | None = None
        self._remote_classification_seen_once = False
        self.connection_state = ConnectionState.LOCAL
        self.preview_image = None
        self.preview_source_image = None
        self.sexing_preview_image = None
        self.sexing_preview_source_image = None
        self._last_sexing_preview_key: str | None = None
        self.workspace_notebook = None
        self.workspace_channel_tab = None
        self.workspace_sexing_tab = None
        self.workspace_assay_tab = None
        self.operations_logo_image = None
        self.footer_banner_images = []
        self.window_icon_images = []
        self.entry_fly_display_image = None
        self.entry_fly_frames = []
        self.entry_fly_job: str | None = None
        self.entry_fly_frame_index = 0
        self.entry_fly_source_image = None
        self.operations_logo_source = None
        self.operations_logo_photo_base = None
        self.operations_logo_hidden = False
        self._ops_resize_job: str | None = None
        self._ops_layout_mode: str | None = None
        self._ops_top_spacer = None
        self._ops_gap1 = None
        self._ops_gap2 = None
        self._ops_bottom_spacer = None
        self.last_preview_mtime: float | None = None
        self.last_result_mtime: float | None = None
        self.last_used_detection_mtime: float | None = None
        self._awaiting_current_detection = False
        self._current_detection_baseline_mtime: float | None = None
        self._startup_preview_requested = False
        self._startup_preview_completed = False
        self._last_output_dir_text = ""
        self.remote_settings = load_remote_connection_settings(self.repo_root)
        self.remote_controller: RemoteController | None = None
        self.remote_sync: RemoteSyncManager | None = None
        self._local_runtime_cache: dict[str, object] | None = None
        self._local_runtime_error: str | None = None
        self._fin6_bridge_cache: object | None = None
        self._fin6_bridge_error: str | None = None
        self._pending_channel_setup_resume = None
        self._pending_channel_setup_action_label: str | None = None
        self._channel_setup_panel: ChannelSetupPanel | None = None
        self._open_channel_setup_after_prep = False
        self._camera_role_panel: CameraRolePanel | None = None
        self._channel_setup_completed_this_session = False
        self.entry_page_scale = self.entry_profile["scale"]

        self.state_var = tk.StringVar(value="IDLE")
        self.position_var = tk.StringVar(value="0.00 mm")
        self.message_var = tk.StringVar(value="Ready")
        self.mode_var = tk.StringVar(value="Local Mode")
        self.connection_var = tk.StringVar(value="Local controller active.")
        self.controller_mode_choice = tk.StringVar(value="Local")
        self.remote_url_var = tk.StringVar(value=self.remote_settings.base_url)
        self.remote_api_key_var = tk.StringVar(value=self.remote_settings.api_key)
        self.detection_var = tk.StringVar(value="Waiting for channel detection output.")
        self.output_dir_var = tk.StringVar(value=str(self._default_channel_output_dir()))
        self.sort_stage_var = tk.StringVar(value="Waiting for START.")
        self.sort_cycle_var = tk.StringVar(value="0")
        self.sort_detected_var = tk.StringVar(value="--")
        self.sort_pickup_var = tk.StringVar(value="--")
        self.sort_last_sex_var = tk.StringVar(value="--")
        self.sort_confidence_var = tk.StringVar(value="--")
        self.sort_destination_var = tk.StringVar(value="--")
        self.sort_notes_var = tk.StringVar(value="Tube counts and classifier output will appear here during automated loading.")
        self.channel_workspace_summary_var = tk.StringVar(value="")
        self.sexing_workspace_summary_var = tk.StringVar(value="")
        self.assay_workspace_summary_var = tk.StringVar(value="")
        self.sort_tube_count_vars = {
            "T1": tk.StringVar(value="0 / 10"),
            "T2": tk.StringVar(value="0 / 10"),
            "T3": tk.StringVar(value="0 / 10"),
            "T4": tk.StringVar(value="0 / 10"),
            "T5": tk.StringVar(value="0 / 10"),
        }
        self.device_state_labels: dict[str, tk.Label] = {}
        self.device_state_text: dict[str, tk.StringVar] = {}
        self.device_detail_text: dict[str, tk.StringVar] = {}

        self.control_widgets = []
        self.toggle_widgets = []
        self.motion_widgets = []
        self.remote_unsupported_widgets = []
        self.local_vision_widgets = []
        self.manual_move_entry: ttk.Entry | None = None

        self._refresh_workspace_copy()
        self.create_widgets()
        self._set_window_icon()
        self._reset_sorting_status_display()
        self._clear_channel_preview_state(clear_artifacts=True, placeholder="Waiting for calibration or a current channel detection image...")
        self.set_status("idle", "Ready")
        self.log_message(f"Channel output directory: {self.output_dir_var.get()}")
        self.update_position()
        self.update_channel_preview()
        self.process_queue()
        self._apply_connection_state(ConnectionState.LOCAL, "Local controller active.")
        self.root.after(600, self._maybe_request_startup_channel_preview)

    def _load_local_runtime(self) -> dict[str, object]:
        if self._local_runtime_cache is not None:
            return self._local_runtime_cache

        try:
            motion = importlib.import_module("motion")
            vacuum = importlib.import_module("vacuum")
            vibration = importlib.import_module("vibration")
            classifier_module = importlib.import_module("fly_classifier")
        except Exception as exc:
            self._local_runtime_error = f"{type(exc).__name__}: {exc}"
            self._refresh_local_mode_display()
            raise RuntimeError(f"Local runtime dependencies are unavailable: {self._local_runtime_error}") from exc

        self._local_runtime_error = None
        self._local_runtime_cache = {
            "motion": motion,
            "vacuum": vacuum,
            "vibration": vibration,
            "classify_fly": getattr(classifier_module, "classify_fly"),
            "gpio_available": bool(getattr(motion, "GPIO_AVAILABLE", False)),
        }
        self._refresh_local_mode_display()
        return self._local_runtime_cache

    def _get_local_runtime_if_loaded(self) -> dict[str, object] | None:
        return self._local_runtime_cache

    def _ensure_local_runtime_or_warn(self, action_label: str) -> dict[str, object] | None:
        try:
            return self._load_local_runtime()
        except RuntimeError as exc:
            self.set_status("error", str(exc))
            self.log_message(f"{action_label} unavailable: {exc}")
            messagebox.showerror("Local Mode Unavailable", f"{action_label} requires local dependencies.\n\n{exc}")
            return None

    def _load_fin6_bridge(self):
        if self._fin6_bridge_cache is not None:
            return self._fin6_bridge_cache

        try:
            fin6_bridge = importlib.import_module("host_app.operator_bridge")
        except Exception as exc:
            self._fin6_bridge_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"fin6 integration is unavailable: {self._fin6_bridge_error}") from exc

        self._fin6_bridge_error = None
        self._fin6_bridge_cache = fin6_bridge
        return fin6_bridge

    def _ensure_fin6_bridge_or_warn(self, action_label: str):
        try:
            return self._load_fin6_bridge()
        except RuntimeError as exc:
            self.set_status("error", str(exc))
            self.log_message(f"{action_label} unavailable: {exc}")
            messagebox.showerror("fin6 Integration Unavailable", f"{action_label} requires the fin6 integration.\n\n{exc}")
            return None

    def _get_local_gpio_available(self) -> bool | None:
        runtime = self._get_local_runtime_if_loaded()
        if runtime is None:
            return None
        return bool(runtime["gpio_available"])

    def _local_mode_presentation(self) -> tuple[str, str]:
        if self._local_runtime_error:
            return "Local Mode (Unavailable)", "#F44336"

        gpio_available = self._get_local_gpio_available()
        if gpio_available is True:
            return "Local Hardware Mode", "#4CAF50"
        if gpio_available is False:
            return "Local Simulation Mode", "#FF9800"
        return "Local Mode", "#607D8B"

    def _refresh_local_mode_display(self) -> None:
        if self.is_remote_mode():
            return

        label_text, label_color = self._local_mode_presentation()
        self.mode_var.set(label_text)
        if getattr(self, "mode_label", None) is not None:
            self.mode_label.config(bg=label_color)
        if getattr(self, "connection_label", None) is not None:
            self.connection_label.config(bg=label_color)

    @staticmethod
    def _to_namespace(value):
        if isinstance(value, dict):
            return SimpleNamespace(**{key: DrosophilaGUI._to_namespace(item) for key, item in value.items()})
        if isinstance(value, list):
            return [DrosophilaGUI._to_namespace(item) for item in value]
        return value

    @staticmethod
    def _channel_setup_ready(fin6_status) -> bool:
        return bool(
            getattr(fin6_status, "channel_background_ready", False)
            and getattr(fin6_status, "channel_calibration_ready", False)
        )

    @staticmethod
    def _assay_setup_ready(fin6_status) -> bool:
        return bool(
            getattr(fin6_status, "assay_background_ready", False)
            and getattr(fin6_status, "assay_calibration_ready", False)
        )

    def _channel_setup_required_message(self, action_label: str) -> str:
        return (
            f"{action_label} needs Channel Detection Setup completed first.\n\n"
            "What you need to do:\n\n"
            "Empty the channel\n"
            "Move the nozzle out of the camera view\n"
            "Capture a clean channel background\n"
            "Run channel calibration\n"
            "Save the setup\n\n"
            "Open Channel Detection Setup now?"
        )

    def _assay_setup_required_message(self, action_label: str) -> str:
        return (
            f"{action_label} needs Assay Setup completed first.\n\n"
            "Open Assay Setup now?"
        )

    def _automated_start_motion_message(self) -> str:
        return (
            "Load the flies now and make sure they are distributed in the channel before starting.\n\n"
            "When you continue, the machine will home first and then begin the automated loading "
            "and classification sequence."
        )

    def _confirm_automated_run_loaded(self) -> bool:
        dialog = OperatorChoiceDialog(
            self.root,
            title="Start Automated Run?",
            message=self._automated_start_motion_message(),
            primary_text="Flies Are Loaded",
            cancel_text="Cancel",
        )
        self.root.wait_window(dialog.window)
        return bool(dialog.result)

    def _refresh_workspace_copy(self) -> None:
        location = "the Pi" if self.is_remote_mode() else "this machine"
        self.channel_workspace_summary_var.set(
            f"Channel detection uses the saved Channel Detection Setup on {location}. "
            "Use Detect Channel here for a fresh annotated preview. Background capture is only needed when setup is missing."
        )
        self.sexing_workspace_summary_var.set(
            "Sexing is driven by chamber classification during automated routing. "
            "This tab shows the latest classification, confidence, routing destination, and live tube counts."
        )
        self.assay_workspace_summary_var.set(
            f"Assay runs use the saved assay setup on {location}. "
            "Use Run Assay here for a direct assay run, or START to run automation first and launch assay at the end."
        )

    def _ensure_remote_connection_for_action(self, action_label: str) -> bool:
        if not self.is_remote_mode():
            return True
        if self.remote_connected and self.remote_controller is not None:
            return True
        messagebox.showwarning("Disconnected", f"Connect to the Pi backend before using {action_label}.")
        self.set_status("error", f"{action_label} unavailable while disconnected from the Pi backend.")
        return False

    def _open_fin6_setup_with_bridge(self, fin6_bridge) -> bool:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "Wait for the current task to finish before opening the fin6 setup GUI.")
            return False
        try:
            process = fin6_bridge.launch_fin6_gui()
        except Exception as exc:
            self.set_status("error", f"Could not open fin6 setup: {exc}")
            self.log_message(f"Could not open fin6 setup GUI: {exc}")
            messagebox.showerror("fin6 Setup Error", str(exc))
            return False

        pid_text = getattr(process, "pid", None)
        if pid_text is None:
            self.log_message("Opened fin6 setup GUI.")
        else:
            self.log_message(f"Opened fin6 setup GUI (pid {pid_text}).")
        self.set_status("running", "Opened fin6 setup GUI.")
        return True

    def _open_remote_fin6_setup(self) -> bool:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "Wait for the current task to finish before opening the fin6 setup GUI.")
            return False
        if not self.is_remote_mode() or not self.remote_connected or self.remote_controller is None:
            messagebox.showwarning("Disconnected", "Connect to the Pi backend before opening fin6 setup on the Pi.")
            return False
        try:
            response = self.remote_controller.launch_fin6_setup()
        except (ControllerConnectionError, ControllerError) as exc:
            self.set_status("error", f"Could not open Pi fin6 setup: {exc}")
            self.log_message(f"Could not open Pi fin6 setup GUI: {exc}")
            messagebox.showerror("fin6 Setup Error", str(exc))
            return False

        ok = bool(response.get("ok", True))
        message = str(response.get("message", "Opened fin6 setup GUI on the Pi."))
        if not ok:
            self.set_status("error", message)
            self.log_message(f"Could not open Pi fin6 setup GUI: {message}")
            messagebox.showerror("fin6 Setup Error", message)
            return False

        self.log_message(message)
        self.set_status("running", message)
        return True

    def _get_remote_fin6_setup_status_or_warn(self, action_label: str):
        return self._get_fin6_setup_status(action_label, show_errors=True)

    def open_fin6_setup(self):
        if not self._ensure_remote_connection_for_action("Open Assay Setup"):
            return
        fin6_bridge = self._ensure_fin6_bridge_or_warn("Open fin6 Setup")
        if fin6_bridge is None:
            return
        self._open_fin6_setup_with_bridge(fin6_bridge)

    def open_channel_setup(self):
        if not self._ensure_remote_connection_for_action("Open Channel Detection Setup"):
            return
        self._open_channel_setup_panel("Channel Detection Setup")

    def open_camera_roles(self):
        if not self._ensure_remote_connection_for_action("Open Camera Roles"):
            return
        self._open_camera_role_panel()

    def _get_fin6_setup_status(self, action_label: str, *, show_errors: bool):
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                if show_errors:
                    messagebox.showwarning("Disconnected", f"Connect to the Pi backend before using {action_label}.")
                return None
            try:
                payload = self.remote_controller.get_fin6_setup_status()
            except (ControllerConnectionError, ControllerError) as exc:
                if show_errors:
                    self.set_status("error", str(exc))
                    self.log_message(f"{action_label} unavailable: {exc}")
                    messagebox.showerror("Channel Detection Setup Error", f"{action_label} could not read setup on the Pi.\n\n{exc}")
                return None
            return self._to_namespace(payload)

        try:
            fin6_bridge = self._load_fin6_bridge()
        except RuntimeError as exc:
            if show_errors:
                self.set_status("error", str(exc))
                self.log_message(f"{action_label} unavailable: {exc}")
                messagebox.showerror("fin6 Integration Unavailable", f"{action_label} requires the fin6 integration.\n\n{exc}")
            return None
        try:
            return fin6_bridge.get_setup_status()
        except Exception as exc:
            if show_errors:
                self.set_status("error", str(exc))
                self.log_message(f"{action_label} unavailable: {exc}")
                messagebox.showerror("Channel Detection Setup Error", f"{action_label} could not read setup.\n\n{exc}")
            return None

    def _cancel_pending_channel_setup_resume(self) -> None:
        if self._channel_setup_panel is not None:
            try:
                self._channel_setup_panel.close()
            except Exception:
                pass
            self._channel_setup_panel = None
        self._pending_channel_setup_resume = None
        self._pending_channel_setup_action_label = None
        self._update_control_interactivity()

    def _capture_channel_setup_background(self) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before capturing a setup background.")
            payload = self.remote_controller.capture_channel_setup_background()
        else:
            try:
                fin6_bridge = self._load_fin6_bridge()
            except RuntimeError as exc:
                raise RuntimeError("Channel Detection Setup integration is unavailable.") from exc
            payload = fin6_bridge.capture_channel_background_from_saved_settings()
        if not payload.get("ok", True):
            raise RuntimeError(str(payload.get("message", "Channel background capture failed.")))
        return payload

    def _capture_channel_setup_preview(self) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before capturing a setup photo.")
            payload = self.remote_controller.capture_channel_setup_preview()
        else:
            try:
                fin6_bridge = self._load_fin6_bridge()
            except RuntimeError as exc:
                raise RuntimeError("Channel Detection Setup integration is unavailable.") from exc
            payload = fin6_bridge.capture_channel_preview_from_saved_settings()
        if not payload.get("ok", True):
            raise RuntimeError(str(payload.get("message", "Channel setup photo capture failed.")))
        return payload

    def _save_channel_setup_calibration(
        self,
        left_point_px: tuple[int, int],
        right_point_px: tuple[int, int],
        channel_mm: float,
    ) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before saving setup calibration.")
            payload = self.remote_controller.save_channel_setup_calibration(
                left_point_px,
                right_point_px,
                channel_mm=channel_mm,
            )
        else:
            try:
                fin6_bridge = self._load_fin6_bridge()
            except RuntimeError as exc:
                raise RuntimeError("Channel Detection Setup integration is unavailable.") from exc
            payload = fin6_bridge.save_channel_calibration_from_points(
                left_point_px,
                right_point_px,
                channel_mm=channel_mm,
            )
        if not payload.get("ok", True):
            raise RuntimeError(str(payload.get("message", "Channel calibration save failed.")))
        return payload

    def _get_channel_setup_preview_bytes(self) -> bytes | None:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                return None
            return self.remote_controller.get_channel_setup_preview_image()

        fin6_status = self._get_fin6_setup_status("Load Channel Detection Setup", show_errors=False)
        if fin6_status is None:
            return None
        preview_path = self.repo_root / "vision" / "fin6" / "backgrounds" / "channel_setup_preview.jpg"
        if not preview_path.exists():
            return None
        try:
            return preview_path.read_bytes()
        except OSError:
            return None

    def _get_channel_setup_status_for_panel(self):
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before opening Channel Detection Setup.")
            return self._to_namespace(self.remote_controller.get_fin6_setup_status())

        try:
            fin6_bridge = self._load_fin6_bridge()
        except RuntimeError as exc:
            raise RuntimeError("Channel Detection Setup integration is unavailable.") from exc
        return fin6_bridge.get_setup_status()

    def _get_channel_setup_camera_options(self) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before loading channel cameras.")
            return self.remote_controller.get_channel_setup_cameras()

        try:
            fin6_bridge = self._load_fin6_bridge()
        except RuntimeError as exc:
            raise RuntimeError("Channel camera discovery is unavailable.") from exc
        return fin6_bridge.list_available_cameras()

    def _get_camera_roles_for_panel(self) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before loading camera roles.")
            return self.remote_controller.get_camera_roles()
        try:
            fin6_bridge = self._load_fin6_bridge()
        except RuntimeError as exc:
            raise RuntimeError("Camera role discovery is unavailable.") from exc
        return fin6_bridge.list_camera_role_assignments()

    def _save_camera_roles_for_panel(
        self,
        channel_device: str,
        channel_preferred_hint: str,
        sexing_camera_index: int,
        assay_device: str,
        assay_preferred_hint: str,
    ) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before saving camera roles.")
            return self.remote_controller.save_camera_roles(
                channel_device=channel_device,
                channel_preferred_hint=channel_preferred_hint,
                sexing_camera_index=sexing_camera_index,
                assay_device=assay_device,
                assay_preferred_hint=assay_preferred_hint,
            )
        try:
            fin6_bridge = self._load_fin6_bridge()
        except RuntimeError as exc:
            raise RuntimeError("Camera role save is unavailable.") from exc
        return fin6_bridge.save_camera_role_assignments(
            channel_device=channel_device,
            channel_preferred_hint=channel_preferred_hint,
            sexing_camera_index=sexing_camera_index,
            assay_device=assay_device,
            assay_preferred_hint=assay_preferred_hint,
        )

    def _save_channel_setup_camera_selection(self, device_reference: str, preferred_hint: str) -> dict[str, Any]:
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                raise RuntimeError("Connect to the Pi backend before saving channel camera selection.")
            payload = self.remote_controller.select_channel_setup_camera(
                device_reference,
                preferred_hint=preferred_hint,
            )
        else:
            try:
                fin6_bridge = self._load_fin6_bridge()
            except RuntimeError as exc:
                raise RuntimeError("Channel camera selection is unavailable.") from exc
            payload = fin6_bridge.update_channel_camera_selection(
                device_reference,
                preferred_hint=preferred_hint,
            )
        if not payload.get("ok", True):
            raise RuntimeError(str(payload.get("message", "Channel camera selection failed.")))
        return payload

    def _build_channel_setup_actions(self) -> ChannelSetupActions:
        return ChannelSetupActions(
            fetch_status=self._get_channel_setup_status_for_panel,
            fetch_camera_options=self._get_channel_setup_camera_options,
            save_camera_selection=self._save_channel_setup_camera_selection,
            capture_background=self._capture_channel_setup_background,
            capture_preview=self._capture_channel_setup_preview,
            save_calibration=self._save_channel_setup_calibration,
            fetch_preview_bytes=self._get_channel_setup_preview_bytes,
        )

    def _handle_channel_setup_ready(self) -> None:
        action_label = self._pending_channel_setup_action_label
        resume_callable = self._pending_channel_setup_resume
        self._channel_setup_completed_this_session = True
        self._channel_setup_panel = None
        self._pending_channel_setup_action_label = None
        self._pending_channel_setup_resume = None
        self.log_message("Channel Detection Setup saved.")
        if action_label and resume_callable is not None:
            self.set_status("running", f"Channel Detection Setup saved. Resuming {action_label}.")
            self.log_message(f"Resuming {action_label}.")
            self._update_control_interactivity()
            self.root.after(100, resume_callable)
            return
        self.set_status("idle", "Channel Detection Setup saved.")
        self._update_control_interactivity()

    def _handle_channel_setup_cancelled(self) -> None:
        action_label = self._pending_channel_setup_action_label
        had_resume = self._pending_channel_setup_resume is not None
        self._channel_setup_panel = None
        self._pending_channel_setup_action_label = None
        self._pending_channel_setup_resume = None
        if had_resume and action_label:
            self.set_status("idle", f"{action_label} cancelled. Channel Detection Setup is still required.")
        else:
            self.set_status("idle", "Channel Detection Setup closed.")
        self._update_control_interactivity()

    def _handle_camera_roles_saved(self) -> None:
        self._camera_role_panel = None
        self.log_message("Camera roles saved.")
        self.set_status("idle", "Camera roles saved.")
        if self._channel_setup_panel is not None and self._channel_setup_panel.is_open():
            try:
                self._channel_setup_panel.refresh_status_and_preview()
            except Exception:
                pass
        self._update_control_interactivity()

    def _handle_camera_roles_cancelled(self) -> None:
        self._camera_role_panel = None
        self.set_status("idle", "Camera roles window closed.")
        self._update_control_interactivity()

    def _open_camera_role_panel(self) -> bool:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "Wait for the current task to finish before opening Camera Roles.")
            return False
        if self._camera_role_panel is not None and self._camera_role_panel.is_open():
            self._camera_role_panel.show()
            return True
        self._camera_role_panel = CameraRolePanel(
            self.root,
            actions=CameraRoleActions(
                fetch_roles=self._get_camera_roles_for_panel,
                save_roles=self._save_camera_roles_for_panel,
            ),
            on_saved=self._handle_camera_roles_saved,
            on_cancel=self._handle_camera_roles_cancelled,
            log_callback=self.log_message,
        )
        self.set_status("running", "Camera Roles is open.")
        self.log_message("Opened Camera Roles.")
        self._update_control_interactivity()
        return True

    def _open_channel_setup_panel(self, action_label: str, resume_callable=None) -> bool:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "Wait for the current task to finish before opening Channel Detection Setup.")
            return False
        if not self._ensure_remote_connection_for_action(action_label):
            return False
        if self._channel_setup_panel is not None and self._channel_setup_panel.is_open():
            self._pending_channel_setup_action_label = action_label if resume_callable is not None else None
            self._pending_channel_setup_resume = resume_callable
            self._channel_setup_panel.show()
            self.set_status("running", "Channel Detection Setup is open.")
            self._update_control_interactivity()
            return True

        if resume_callable is not None:
            self._pending_channel_setup_action_label = action_label
            self._pending_channel_setup_resume = resume_callable
        else:
            self._pending_channel_setup_action_label = None
            self._pending_channel_setup_resume = None

        self._channel_setup_panel = ChannelSetupPanel(
            self.root,
            actions=self._build_channel_setup_actions(),
            on_ready=self._handle_channel_setup_ready,
            on_cancel=self._handle_channel_setup_cancelled,
            status_callback=self.set_status,
            log_callback=self.log_message,
        )
        self.set_status("running", "Channel Detection Setup is open.")
        self.log_message("Opened Channel Detection Setup.")
        self._update_control_interactivity()
        return True

    def _prepare_for_automated_channel_setup(self, action_label: str, resume_callable) -> None:
        should_position = messagebox.askyesno(
            "Prepare for Channel Detection Setup?",
            (
                "The machine will home and move to the initial channel image position so you can "
                "calibrate from the current empty-channel view.\n\n"
                "Is it okay to do that now?"
            ),
        )
        if not should_position:
            self.set_status("idle", f"{action_label} cancelled before Channel Detection Setup.")
            return

        self._pending_channel_setup_action_label = action_label
        self._pending_channel_setup_resume = resume_callable
        self._open_channel_setup_after_prep = True
        self.start_task(
            "channel setup prep",
            "moving",
            "Preparing the machine for Channel Detection Setup.",
            self._run_channel_setup_prep_worker,
        )

    def _run_channel_setup_prep_worker(self) -> None:
        target_position = float(config.CHAMBER_CENTER + 26.0)
        if self.is_remote_mode():
            self.worker_status("moving", "Homing before Channel Detection Setup.")
            self._run_remote_home_for_automation()
            self.worker_status("moving", "Moving to the initial channel image position for calibration.")
            self._run_remote_move_absolute_for_automation(target_position)
            return

        runtime = self._load_local_runtime()
        motion = runtime["motion"]
        self.worker_status("moving", "Homing before Channel Detection Setup.")
        motion.home_to_zero()
        self.worker_status("moving", "Moving to the initial channel image position for calibration.")
        motion.move_to_absolute(target_position)

    def _ensure_channel_setup_ready_or_begin_setup(self, action_label: str, resume_callable) -> bool:
        fin6_status = self._get_fin6_setup_status(action_label, show_errors=True)
        if fin6_status is None:
            return False
        if self._channel_setup_ready(fin6_status) and self._channel_setup_completed_this_session:
            return True

        if self._channel_setup_ready(fin6_status) and not self._channel_setup_completed_this_session:
            should_open = messagebox.askyesno(
                "Calibration Required",
                (
                    f"{action_label} needs Channel Detection Setup completed for this control-panel session.\n\n"
                    "Open Channel Detection Setup now?"
                ),
            )
            if not should_open:
                self.set_status("idle", f"{action_label} cancelled. Channel Detection Setup is still required.")
                return False
            self._startup_preview_requested = False
            self._startup_preview_completed = False
            self._begin_new_detection_session(
                placeholder="Waiting for Channel Detection Setup calibration in this session..."
            )
            if action_label == "Automated Run":
                self._prepare_for_automated_channel_setup(action_label, resume_callable)
            else:
                self._open_channel_setup_panel(action_label, resume_callable)
            return False

        should_open = messagebox.askyesno("Setup Required", self._channel_setup_required_message(action_label))
        if not should_open:
            self.set_status("idle", f"{action_label} cancelled. Channel Detection Setup is still required.")
            return False

        if action_label == "Automated Run":
            self._prepare_for_automated_channel_setup(action_label, resume_callable)
        else:
            self._open_channel_setup_panel(action_label, resume_callable)
        return False

    def _ensure_assay_setup_ready_or_prompt(self, action_label: str) -> bool:
        fin6_status = self._get_fin6_setup_status(action_label, show_errors=True)
        if fin6_status is None:
            return False
        if self._assay_setup_ready(fin6_status):
            return True

        should_open = messagebox.askyesno("Setup Required", self._assay_setup_required_message(action_label))
        if not should_open:
            self.set_status("idle", f"{action_label} cancelled. Assay Setup is still required.")
            return False

        fin6_bridge = self._ensure_fin6_bridge_or_warn("Open Assay Setup")
        if fin6_bridge is not None:
            self._open_fin6_setup_with_bridge(fin6_bridge)
        return False

    def _ask_user_yes_no_from_worker(self, title: str, message: str) -> bool:
        prompt_state = {
            "title": title,
            "message": message,
            "response": False,
            "event": threading.Event(),
        }
        self.ui_queue.put(("prompt_yes_no", prompt_state))
        while not prompt_state["event"].wait(0.1):
            self._check_stop()
        return bool(prompt_state["response"])

    def _resolve_asset_path(self, *candidate_names: str) -> Path | None:
        search_roots = (
            self.repo_root / "assets",
            self.code_dir / "assets",
        )
        for asset_root in search_roots:
            for candidate_name in candidate_names:
                candidate_path = asset_root / candidate_name
                if candidate_path.exists():
                    return candidate_path
        return None

    def _set_window_icon(self) -> None:
        ico_path = self._resolve_asset_path("drosophila.ico")
        icon_path = self._resolve_asset_path(
            "drosophila.png",
            "drosophilafly.png",
            "drosphoila.png",
        )
        if icon_path is None and ico_path is None:
            return

        if sys.platform.startswith("win") and ico_path is not None:
            try:
                self.root.iconbitmap(default=str(ico_path))
            except tk.TclError:
                pass

        if icon_path is None:
            return

        try:
            from PIL import Image, ImageTk

            icon_source = Image.open(icon_path).convert("RGBA")
            resample = getattr(Image, "Resampling", Image)
            large_icon = ImageTk.PhotoImage(icon_source.resize((128, 128), resample.LANCZOS))
            medium_icon = ImageTk.PhotoImage(icon_source.resize((64, 64), resample.LANCZOS))
            small_icon = ImageTk.PhotoImage(icon_source.resize((32, 32), resample.LANCZOS))
            self.window_icon_images = [large_icon, medium_icon, small_icon]
            self.root.iconphoto(True, *self.window_icon_images)
        except Exception:
            try:
                fallback_icon = tk.PhotoImage(file=str(icon_path))
                self.window_icon_images = [fallback_icon]
                self.root.iconphoto(True, fallback_icon)
            except tk.TclError:
                self.window_icon_images = []

    def _configure_windows_app_id(self) -> None:
        if not sys.platform.startswith("win"):
            return

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "CamrenKhoury.AutomatedDrosophilaSortingSystem"
            )
        except Exception:
            pass

    def _capture_screen_metrics(self) -> None:
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.screen_aspect = self.screen_width / max(1.0, float(self.screen_height))

    def _configure_entry_profile(self) -> None:
        scale = min(1.0, self.screen_width / 1366.0, self.screen_height / 768.0)
        if self.screen_aspect < 1.35:
            scale *= max(0.75, self.screen_aspect / 1.35)
        if scale > 0.98:
            scale = 1.0
        scale = max(0.7, scale)

        def scaled(value: int, minimum: int) -> int:
            return max(minimum, int(round(value * scale)))

        self.entry_profile = {
            "scale": round(self.gui_profile.entry_page_scale * scale, 3),
            "fly_max": scaled(self.gui_profile.entry_fly_max, 180),
            "fly_column_min": scaled(self.gui_profile.entry_fly_column_min, 200),
            "fly_column_pad": scaled(self.gui_profile.entry_fly_column_pad, 2),
            "footer_w": scaled(self.gui_profile.footer_banner_width, 180),
            "footer_h": scaled(self.gui_profile.footer_banner_height, 50),
        }
        if self.screen_aspect < 1.35 or self.screen_height <= 800:
            self.entry_profile["fly_max"] = max(160, int(self.entry_profile["fly_max"] * 0.9))

    def _fit_window_to_screen(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        target_width = min(
            max(1100, int(screen_width * self.gui_profile.window_width_ratio)),
            screen_width - self.gui_profile.window_margin_x,
        )
        target_height = min(
            max(760, int(screen_height * self.gui_profile.window_height_ratio)),
            screen_height - self.gui_profile.window_margin_y,
        )
        x_offset = max(20, (screen_width - target_width) // 2)
        y_offset = max(14, (screen_height - target_height) // 4)
        self.root.geometry(f"{target_width}x{target_height}+{x_offset}+{y_offset}")

    def _is_compact_screen(self) -> bool:
        return self.screen_height <= 800 or self.screen_aspect < 1.35

    def _entry_scale(self, value: int) -> int:
        return max(1, math.ceil(value * self.entry_page_scale))

    def _entry_button_scale(self, value: int) -> int:
        return max(1, round(self._entry_scale(value) * 0.75))

    def _ui_scale_font(self, size: int) -> int:
        return size

    def _blend_hex(self, start_hex: str, end_hex: str, ratio: float) -> str:
        ratio = max(0.0, min(1.0, ratio))
        start = tuple(int(start_hex[index : index + 2], 16) for index in (1, 3, 5))
        end = tuple(int(end_hex[index : index + 2], 16) for index in (1, 3, 5))
        blended = tuple(
            round(start[channel] + ((end[channel] - start[channel]) * ratio))
            for channel in range(3)
        )
        return f"#{blended[0]:02X}{blended[1]:02X}{blended[2]:02X}"

    def _fit_photoimage(self, photo: tk.PhotoImage, max_size: int) -> tk.PhotoImage:
        if max_size <= 0:
            return photo
        width = photo.width()
        height = photo.height()
        if width <= max_size and height <= max_size:
            return photo
        scale = max(1, int(max(width, height) / max_size))
        if scale <= 1:
            return photo
        return photo.subsample(scale, scale)

    def create_widgets(self):
        style = ttk.Style()
        style.configure("Status.TLabelframe", background="#E8F4F8", relief="raised", borderwidth=2)
        style.configure("Motion.TLabelframe", background="#F0F8E8", relief="raised", borderwidth=2)
        style.configure("Device.TLabelframe", background="#FFF8E8", relief="raised", borderwidth=2)
        style.configure("Ops.TLabelframe", background="#F8E8F0", relief="raised", borderwidth=2)
        style.configure("Log.TLabelframe", background="#F8F8F8", relief="sunken", borderwidth=1)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.page_container = tk.Frame(self.root, bg="#F4EFE6")
        self.page_container.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.page_container.columnconfigure(0, weight=1)
        self.page_container.rowconfigure(0, weight=1)

        self.entry_frame = tk.Frame(
            self.page_container,
            bg=self._blend_hex("#FFFFFF", "#686766", 0.58),
            padx=self._entry_scale(20),
            pady=self._entry_scale(20),
        )
        self.entry_frame.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.entry_frame.columnconfigure(0, weight=1)
        self.entry_frame.rowconfigure(0, weight=1)
        self.entry_frame.rowconfigure(1, weight=0)
        self.entry_frame.rowconfigure(2, weight=0)
        self.create_entry_page(self.entry_frame)

        self.main_frame = ttk.Frame(self.page_container, padding="15")
        self.main_frame.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.main_frame.columnconfigure(0, weight=0)
        self.main_frame.columnconfigure(1, weight=1)
        self.main_frame.columnconfigure(2, weight=0)
        self.main_frame.rowconfigure(1, weight=1)
        self.main_frame.rowconfigure(3, weight=1)

        self.create_status_section(self.main_frame)
        self.create_main_content(self.main_frame)
        self.create_system_controls(self.main_frame)
        self.create_log_section(self.main_frame)
        self.show_entry_page()

    def create_entry_page(self, parent):
        halo_shell = tk.Frame(
            parent,
            bg=self._blend_hex("#FFFFFF", "#686766", 0.58),
            padx=self._entry_scale(10),
            pady=self._entry_scale(10),
        )
        halo_shell.grid(row=0, column=0)

        halo_ring_specs = (
            (0.74, 5),
        )
        halo_container = halo_shell
        for blend_ratio, padding in halo_ring_specs:
            halo_color = self._blend_hex("#FFFFFF", "#686766", blend_ratio)
            halo_container = tk.Frame(
                halo_container,
                bg=halo_color,
                bd=0,
                highlightthickness=0,
                padx=self._entry_scale(padding),
                pady=self._entry_scale(padding),
            )
            halo_container.grid(row=0, column=0)

        entry_card = tk.Frame(
            halo_container,
            bg="#686766",
            highlightbackground="#686766",
            highlightthickness=self._entry_scale(1),
            bd=0,
            padx=self._entry_scale(61),
            pady=self._entry_scale(61),
        )
        entry_card.grid(row=0, column=0)
        entry_card.columnconfigure(0, weight=1)
        entry_card.columnconfigure(1, weight=0, minsize=self._entry_scale(self.entry_profile["fly_column_min"]))

        left_panel = tk.Frame(entry_card, bg="#686766")
        left_panel.grid(row=0, column=0, sticky=(tk.N, tk.W), padx=(0, self._entry_scale(47)))

        tk.Label(
            left_panel,
            text="Drosophila Genetics GUI",
            bg="#686766",
            fg="#F3F4F6",
            font=("Arial", self._entry_scale(38), "bold"),
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, self._entry_scale(17)))

        tk.Label(
            left_panel,
            text="Open the control panel when you are ready.",
            bg="#686766",
            fg="#E5E7EA",
            font=("Arial", self._entry_scale(18)),
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky=tk.W, pady=(0, self._entry_scale(38)))

        button_row = tk.Frame(left_panel, bg="#686766")
        button_row.grid(row=2, column=0, sticky=tk.W)
        button_gap = self._entry_scale(18)

        enter_button = ActionButton(
            button_row,
            text="Enter Control Panel",
            color="#8E2D2B",
            active_color="#732220",
            font=("Arial", self._entry_button_scale(21), "bold"),
            padx=self._entry_button_scale(30),
            pady=self._entry_button_scale(17),
            command=self.show_control_panel,
        )
        enter_button.grid(row=0, column=0, sticky=tk.W, padx=(0, button_gap))

        update_button = ActionButton(
            button_row,
            text="Check for Updates",
            color="#8E2D2B",
            active_color="#732220",
            font=("Arial", self._entry_button_scale(21), "bold"),
            padx=self._entry_button_scale(30),
            pady=self._entry_button_scale(17),
            command=self.check_for_updates,
        )
        update_button.grid(row=0, column=1, sticky=tk.W)

        fly_panel = tk.Frame(
            entry_card,
            bg="#686766",
            padx=self._entry_scale(10),
            pady=self._entry_scale(7),
        )
        fly_panel.grid(
            row=0,
            column=1,
            sticky=(tk.N, tk.W),
            padx=(self._entry_scale(self.entry_profile["fly_column_pad"]), 0),
        )
        fly_panel.columnconfigure(0, weight=1)
        fly_panel.rowconfigure(0, weight=1)

        self.entry_fly_label = tk.Label(
            fly_panel,
            bg="#686766",
            bd=0,
            highlightthickness=0,
            anchor="center",
            justify="center",
        )
        self.entry_fly_label.grid(row=0, column=0, sticky="")

        self.create_entry_metadata(
            parent,
            row=1,
            column=0,
            background=self.entry_frame.cget("bg"),
            pady=(self._entry_scale(18), 0),
        )
        self.create_footer_banners(
            parent,
            row=2,
            column=0,
            background=self.entry_frame.cget("bg"),
            pady=(self._entry_scale(14), 0),
        )

    def _repo_version_summary(self) -> tuple[str, str]:
        commit_count = "?"
        last_update = "Unavailable"

        try:
            count_result = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if count_result.returncode == 0:
                parsed_count = count_result.stdout.strip()
                if parsed_count.isdigit():
                    commit_count = parsed_count
        except Exception:
            pass

        try:
            date_result = subprocess.run(
                ["git", "-C", str(self.repo_root), "log", "-1", "--date=short", "--format=%cd"],
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if date_result.returncode == 0:
                parsed_date = date_result.stdout.strip()
                if parsed_date:
                    last_update = parsed_date
        except Exception:
            pass

        return f"V1.{commit_count}", last_update

    def _run_git_command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def _relaunch_application(self) -> None:
        launch_bat = self.code_dir / "launch_gui.bat"
        launch_sh = self.linux_launcher_dir / "launch_gui.sh"

        if sys.platform.startswith("win") and launch_bat.exists():
            subprocess.Popen(
                ["cmd", "/c", "start", "", str(launch_bat)],
                cwd=str(self.code_dir),
            )
            return

        if not sys.platform.startswith("win") and launch_sh.exists():
            subprocess.Popen(
                ["bash", str(launch_sh)],
                cwd=str(self.repo_root),
                start_new_session=True,
            )
            return

        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable], cwd=str(self.repo_root))
            return

        subprocess.Popen([sys.executable, str(self.gui_dir / "gui.py")], cwd=str(self.repo_root))

    def check_for_updates(self) -> None:
        repo_git_dir = self.repo_root / ".git"
        if not repo_git_dir.exists():
            messagebox.showerror("Updates", "This copy of the application is not inside a git repository.")
            return

        try:
            status_result = self._run_git_command("status", "--porcelain")
        except Exception as exc:
            messagebox.showerror("Updates", f"Could not inspect repository state:\n{exc}")
            return

        if status_result.returncode != 0:
            detail = status_result.stderr.strip() or status_result.stdout.strip() or "Unknown git error."
            messagebox.showerror("Updates", f"Could not inspect repository state:\n{detail}")
            return

        if status_result.stdout.strip():
            messagebox.showwarning(
                "Updates",
                "The repository has local changes. Commit or stash them before updating from GitHub.",
            )
            return

        try:
            self.root.config(cursor="watch")
            self.root.update_idletasks()

            fetch_result = self._run_git_command("fetch", "--quiet")
            if fetch_result.returncode != 0:
                detail = fetch_result.stderr.strip() or fetch_result.stdout.strip() or "Unknown git error."
                messagebox.showerror("Updates", f"Could not check for updates:\n{detail}")
                return

            upstream_result = self._run_git_command("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if upstream_result.returncode != 0:
                detail = upstream_result.stderr.strip() or upstream_result.stdout.strip() or "No upstream branch configured."
                messagebox.showerror("Updates", f"Could not determine the tracked branch:\n{detail}")
                return

            behind_result = self._run_git_command("rev-list", "--count", "HEAD..@{u}")
            if behind_result.returncode != 0:
                detail = behind_result.stderr.strip() or behind_result.stdout.strip() or "Unknown git error."
                messagebox.showerror("Updates", f"Could not compare repository versions:\n{detail}")
                return

            behind_count = behind_result.stdout.strip()
            if not behind_count.isdigit() or int(behind_count) <= 0:
                messagebox.showinfo("Updates", "This installation is already up to date.")
                return

            should_update = messagebox.askyesno(
                "Update Available",
                (
                    f"{behind_count} update(s) are available.\n\n"
                    "The application will pull the latest changes, close, and reopen.\n\n"
                    "Continue?"
                ),
            )
            if not should_update:
                return

            pull_result = self._run_git_command("pull", "--ff-only")
            if pull_result.returncode != 0:
                detail = pull_result.stderr.strip() or pull_result.stdout.strip() or "Unknown git error."
                messagebox.showerror("Updates", f"Update failed:\n{detail}")
                return

            self._relaunch_application()
            self.on_close()
        finally:
            with contextlib.suppress(tk.TclError):
                self.root.config(cursor="")

    def create_entry_metadata(self, parent, row: int, column: int, background: str, pady=(12, 0)):
        metadata_frame = tk.Frame(parent, bg=background)
        metadata_frame.grid(row=row, column=column, sticky=(tk.W, tk.E), pady=pady)
        metadata_frame.columnconfigure(0, weight=1)

        version_text, date_text = self._repo_version_summary()
        metadata_label = tk.Label(
            metadata_frame,
            text=f"{version_text}  |  Last Update: {date_text}",
            bg=background,
            fg="#8E2D2B",
            font=("Arial", self._entry_scale(11), "bold"),
            justify="center",
            anchor="center",
            padx=self._entry_scale(14),
            pady=self._entry_scale(6),
        )
        metadata_label.grid(row=0, column=0)

    def show_entry_page(self):
        self.main_frame.grid_remove()
        self.entry_frame.grid()
        self._display_entry_photo()

    def show_control_panel(self):
        self.entry_frame.grid_remove()
        self.main_frame.grid()

    def is_remote_mode(self) -> bool:
        return self.controller_mode_choice.get().strip().lower() == "remote"

    def _save_remote_settings(self) -> RemoteConnectionSettings:
        settings = RemoteConnectionSettings(
            base_url=self.remote_url_var.get().strip().rstrip("/"),
            api_key=self.remote_api_key_var.get().strip(),
            poll_interval_s=self.remote_settings.poll_interval_s,
            request_timeout_s=self.remote_settings.request_timeout_s,
            config_path=self.remote_settings.config_path,
        )
        save_remote_connection_settings(settings)
        self.remote_settings = settings
        return settings

    def _build_remote_controller(self) -> RemoteController:
        settings = self._save_remote_settings()
        return RemoteController(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout_s=settings.request_timeout_s,
        )

    def _start_remote_sync(self) -> None:
        if not self.is_remote_mode():
            return

        try:
            self.remote_controller = self._build_remote_controller()
        except OSError as exc:
            self._apply_connection_state(ConnectionState.CLIENT_DISCONNECTED, f"Could not save remote settings: {exc}")
            self.set_status("error", "Remote settings could not be saved.")
            return

        if self.remote_sync is not None:
            self.remote_sync.stop()

        self.remote_sync = RemoteSyncManager(
            self.remote_controller,
            self.ui_queue,
            idle_poll_interval_s=self.remote_settings.poll_interval_s,
        )
        self.remote_sync.start()

    def _stop_remote_sync(self) -> None:
        if self.remote_sync is not None:
            self.remote_sync.stop()
            self.remote_sync = None

    def reconnect_remote(self) -> None:
        if not self.is_remote_mode():
            messagebox.showinfo("Remote Mode", "Switch the controller mode to Remote first.")
            return
        self._apply_connection_state(ConnectionState.CONNECTING_TO_PI, "Connecting to Pi backend.")
        self._start_remote_sync()

    def on_controller_mode_changed(self, _event=None) -> None:
        if self.is_remote_mode():
            self.remote_connected = False
            self.remote_backend_busy = False
            self.remote_stop_allowed = False
            self.remote_request_in_flight = False
            self.remote_home_calibration_required = False
            self.remote_home_prompt_scheduled = False
            self.remote_seen_log_keys.clear()
            self._last_remote_status_revision = None
            self._last_remote_preview_source_mtime = None
            self._remote_preview_fetch_in_flight = False
            self.set_preview_placeholder("Waiting for remote channel detection image...")
            self._apply_connection_state(ConnectionState.CONNECTING_TO_PI, "Connecting to Pi backend.")
            self._start_remote_sync()
            return

        self._stop_remote_sync()
        self.remote_controller = None
        self.remote_connected = False
        self.remote_backend_busy = False
        self.remote_stop_allowed = False
        self.remote_request_in_flight = False
        self.remote_backend_degraded = False
        self.remote_home_calibration_required = False
        self.remote_home_prompt_scheduled = False
        self.remote_motion_available = True
        self.remote_vacuum_available = True
        self.remote_vibration_available = True
        self.remote_classifier_available = True
        self.remote_assay_available = True
        self.remote_seen_log_keys.clear()
        self._last_remote_status_revision = None
        self._last_remote_preview_source_mtime = None
        self._remote_preview_fetch_in_flight = False
        self._last_remote_classification_signature = None
        self._remote_classification_seen_once = False
        self.output_dir_var.set(str(self._current_channel_output_dir()))
        self._apply_connection_state(ConnectionState.LOCAL, "Local controller active.")
        self.set_status("idle", "Ready")

    def _apply_connection_state(self, state: ConnectionState, message: str) -> None:
        self.connection_state = state
        self.connection_var.set(message)

        if state == ConnectionState.LOCAL:
            label_text, label_color = self._local_mode_presentation()
            connection_color = label_color
            self.remote_connected = False
            self.remote_home_calibration_required = False
            self.remote_home_prompt_scheduled = False
            if self.controller_mode_choice.get() != "Remote":
                self._startup_preview_requested = False
                self._startup_preview_completed = False
        elif state in {ConnectionState.CLIENT_CONNECTED, ConnectionState.CLIENT_RECONNECTED}:
            label_text = "Remote Mode (Degraded)" if self.remote_backend_degraded else "Remote Mode"
            label_color = "#FF9800" if self.remote_backend_degraded else "#4CAF50"
            connection_color = "#4CAF50"
            self.remote_connected = True
            self.remote_home_calibration_required = False
            self.remote_home_prompt_scheduled = False
            if not self._startup_preview_completed:
                self.root.after(400, self._maybe_request_startup_channel_preview)
        elif state in {ConnectionState.CONNECTING_TO_PI, ConnectionState.RECONNECT_ATTEMPT, ConnectionState.RETRY_WAIT}:
            label_text = "Remote Mode"
            label_color = "#FF9800"
            connection_color = "#FF9800"
            self.remote_connected = False
            self.remote_backend_busy = False
            self.remote_stop_allowed = False
            self.remote_home_calibration_required = False
            self.remote_home_prompt_scheduled = False
            self._last_remote_status_revision = None
            self._startup_preview_requested = False
            self._startup_preview_completed = False
        else:
            label_text = "Remote Mode"
            label_color = "#F44336"
            connection_color = "#F44336"
            self.remote_connected = False
            self.remote_backend_busy = False
            self.remote_stop_allowed = False
            self.remote_home_calibration_required = False
            self.remote_home_prompt_scheduled = False
            self._last_remote_status_revision = None
            self._startup_preview_requested = False
            self._startup_preview_completed = False

        self.mode_var.set(label_text)
        self.mode_label.config(bg=label_color)
        if getattr(self, "connection_label", None) is not None:
            self.connection_label.config(bg=connection_color)
        self._refresh_workspace_copy()
        self._update_control_interactivity()

    def _schedule_remote_home_prompt(self) -> None:
        return

    def _prompt_remote_home_calibration(self) -> None:
        return

    def _backend_busy_from_status(self, status: dict) -> bool:
        current_task = status.get("current_task")
        task_state = str(status.get("task_state") or "")
        orchestrator_state = str(status.get("orchestrator_state") or "")
        if current_task:
            return True
        busy_tokens = ("RUNNING", "REQUESTED", "STARTING", "VALIDATING", "APPLYING", "STOP")
        return any(token in task_state.upper() for token in busy_tokens) or any(
            token in orchestrator_state.upper() for token in busy_tokens
        )

    def _apply_remote_status(self, status: dict) -> None:
        status_revision = status.get("status_revision")
        if status_revision is not None:
            normalized_revision = int(status_revision)
            if normalized_revision == self._last_remote_status_revision:
                return
            self._last_remote_status_revision = normalized_revision

        self.remote_backend_degraded = bool(status.get("backend_boot_degraded", False))
        subsystem_health = status.get("subsystem_health", {}) or {}
        subsystem_errors = status.get("subsystem_errors", {}) or {}
        vacuum_status = self._remote_subsystem_status(subsystem_health, "vacuum")
        vibration_status = self._remote_subsystem_status(subsystem_health, "vibration")

        self.remote_motion_available = self._remote_subsystem_is_usable(subsystem_health, "motion")
        self.remote_vacuum_available = self._remote_subsystem_is_usable(subsystem_health, "vacuum")
        self.remote_vibration_available = self._remote_subsystem_is_usable(subsystem_health, "vibration")
        self.remote_classifier_available = self._remote_subsystem_is_usable(subsystem_health, "classifier")
        self.remote_assay_available = self._remote_subsystem_is_usable(subsystem_health, "assay")
        self.remote_backend_busy = self._backend_busy_from_status(status)
        self.remote_stop_allowed = self.remote_connected and self.remote_backend_busy

        state_text = self._summarize_remote_state(status)
        self.set_status(state_text, str(status.get("latest_message", "Remote status updated.")))
        self.position_var.set(f"{float(status.get('current_position_mm', 0.0)):.2f} mm")
        self.mode_var.set("Remote Mode (Degraded)" if self.remote_backend_degraded else "Remote Mode")
        self.mode_label.config(bg="#FF9800" if self.remote_backend_degraded else "#4CAF50")
        detection_summary = status.get("detection_summary", {}) or {}
        source_mtime_raw = detection_summary.get("source_mtime")
        source_mtime = float(source_mtime_raw) if source_mtime_raw is not None else None
        if self._awaiting_current_detection:
            baseline = self._current_detection_baseline_mtime
            if source_mtime is None or (baseline is not None and source_mtime <= baseline):
                self.detection_var.set("Waiting for current channel detection output.")
            else:
                self._awaiting_current_detection = False
                self._current_detection_baseline_mtime = source_mtime
                self._startup_preview_requested = False
                self._startup_preview_completed = True
                self.detection_var.set(self._format_remote_detection(detection_summary))
        else:
            if self._startup_preview_completed:
                self.detection_var.set(self._format_remote_detection(detection_summary))
            else:
                self.detection_var.set("Waiting for current channel detection output.")

        source_path = detection_summary.get("source_path")
        allow_preview_update = not self._awaiting_current_detection and self._startup_preview_completed
        if source_path and allow_preview_update:
            self.output_dir_var.set(str(source_path))
        self._request_remote_preview_if_needed(detection_summary if allow_preview_update else {})

        self.update_actuator_state("vacuum", bool(status.get("vacuum_on", False)))
        self.update_actuator_state("vibration", bool(status.get("vibration_on", False)))
        self._update_device_availability("vacuum", vacuum_status, subsystem_errors.get("vacuum"))
        self._update_device_availability("vibration", vibration_status, subsystem_errors.get("vibration"))
        self._handle_remote_classification_result(status)

        recent_logs = status.get("recent_logs", []) or []
        self._append_remote_logs(recent_logs)
        self._update_control_interactivity()

    def _summarize_remote_state(self, status: dict) -> str:
        task_state = status.get("task_state")
        if task_state:
            return str(task_state)
        orchestrator_state = status.get("orchestrator_state")
        if orchestrator_state:
            return str(orchestrator_state)
        return str(status.get("backend_lifecycle_state", "CONNECTED"))

    def _format_remote_detection(self, summary: dict) -> str:
        status_text = str(summary.get("status", "unknown"))
        fly_remaining = summary.get("fly_remaining")
        positions = summary.get("x_positions_mm") or []
        count = len(positions) if isinstance(positions, list) else 0
        if fly_remaining is None:
            return f"status={status_text} count={count}"
        return f"status={status_text} remaining={bool(fly_remaining)} count={count}"

    @staticmethod
    def _remote_subsystem_status(subsystem_health: dict, subsystem: str) -> str:
        return str(subsystem_health.get(f"{subsystem}_status", "unavailable")).strip().lower()

    def _remote_subsystem_is_usable(self, subsystem_health: dict, subsystem: str) -> bool:
        status = self._remote_subsystem_status(subsystem_health, subsystem)
        if status == "deferred":
            return True
        return bool(subsystem_health.get(f"{subsystem}_available", False))

    def _append_remote_logs(self, recent_logs: list[dict]) -> None:
        for entry in recent_logs:
            created_at = str(entry.get("created_at", ""))
            level = str(entry.get("level", "INFO"))
            message = str(entry.get("message", ""))
            key = (created_at, level, message)
            if key in self.remote_seen_log_keys:
                continue
            self.remote_seen_log_keys.add(key)
            formatted_message = f"[REMOTE {level}] {message}"
            self.log_message(formatted_message)

    def _handle_remote_classification_result(self, status: dict) -> None:
        result = status.get("classification_result")
        if not result:
            return

        signature = (
            str(result.get("result_class", "UNCERTAIN")),
            float(result.get("confidence", 0.0)),
            tuple(str(error) for error in result.get("errors", [])),
        )

        if not self._remote_classification_seen_once:
            self._remote_classification_seen_once = True
            self._last_remote_classification_signature = signature
            return

        if signature == self._last_remote_classification_signature:
            return

        self._last_remote_classification_signature = signature
        raw_result = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        chamber_count = result.get("count")
        if chamber_count is None:
            chamber_count = raw_result.get("count")
        normalized_result = {
            "class": result.get("result_class", "UNCERTAIN"),
            "confidence": float(result.get("confidence", 0.0)),
            "errors": list(result.get("errors", [])),
            "count": None if chamber_count is None else int(chamber_count),
            "image_path": raw_result.get("image_path"),
        }
        if self.current_task_name == "automated run":
            self.sort_detected_var.set(
                "--" if normalized_result["count"] is None else str(int(normalized_result["count"]))
            )
            self._show_workspace_tab("sexing")
            self._update_sexing_preview_from_result(normalized_result)
            self.sort_last_sex_var.set(
                str(normalized_result["class"]).title()
                if str(normalized_result["class"]).lower() in {"male", "female"}
                else str(normalized_result["class"])
            )
            self.sort_confidence_var.set(f"{float(normalized_result['confidence']):.4f}")
            self.sort_notes_var.set(
                ", ".join(str(error) for error in normalized_result["errors"])
                if normalized_result["errors"]
                else "Remote classification updated."
            )
            return
        self.show_classification_result(normalized_result)

    def _request_remote_preview_if_needed(self, detection_summary: dict) -> None:
        if not self.is_remote_mode() or not self.remote_connected or self.remote_controller is None:
            return

        source_exists = bool(detection_summary.get("source_exists", False))
        source_mtime = detection_summary.get("source_mtime")
        if not source_exists:
            self._last_remote_preview_source_mtime = None
            self.preview_image = None
            self.set_preview_placeholder("Waiting for remote channel detection image...")
            return

        if self._remote_preview_fetch_in_flight:
            return

        if source_mtime is not None and source_mtime == self._last_remote_preview_source_mtime and self.preview_image is not None:
            return

        self._remote_preview_fetch_in_flight = True
        threading.Thread(
            target=self._remote_preview_worker,
            args=(float(source_mtime) if source_mtime is not None else None,),
            daemon=True,
        ).start()

    def _remote_preview_worker(self, source_mtime: float | None) -> None:
        try:
            image_bytes = self.remote_controller.get_channel_preview_image() if self.remote_controller is not None else None
            self.ui_queue.put(("remote_preview", image_bytes, source_mtime))
        except (ControllerConnectionError, ControllerError) as exc:
            self.ui_queue.put(("remote_preview_error", str(exc)))

    def _apply_remote_preview(self, image_bytes: bytes | None, source_mtime: float | None) -> None:
        self._remote_preview_fetch_in_flight = False
        if image_bytes is None:
            self.preview_image = None
            self._last_remote_preview_source_mtime = None
            self.set_preview_placeholder("Remote channel detection image not available yet.")
            return

        self.load_channel_preview_bytes(image_bytes)
        self._last_remote_preview_source_mtime = source_mtime

    def _fail_remote_preview(self, message: str) -> None:
        self._remote_preview_fetch_in_flight = False
        self.preview_image = None
        self.set_preview_placeholder(f"Remote preview unavailable:\n{message}")

    def _start_remote_command(
        self,
        label: str,
        status_state: str,
        message: str,
        command_callable,
        *,
        allow_calibration_bypass: bool = False,
    ) -> None:
        if not self.is_remote_mode():
            return
        if self.remote_request_in_flight:
            messagebox.showwarning("Busy", "Wait for the current remote request to finish.")
            return
        if not self.remote_connected or self.remote_controller is None:
            messagebox.showwarning("Disconnected", "Connect to the Pi backend before sending commands.")
            return
        if self.remote_home_calibration_required and not allow_calibration_bypass:
            self._schedule_remote_home_prompt()
            return

        self.remote_request_in_flight = True
        self.set_status(status_state, message)
        self.log_message(f"Sending remote {label} command.")
        self._update_control_interactivity()

        threading.Thread(
            target=self._remote_command_worker,
            args=(label, command_callable),
            daemon=True,
        ).start()

    def _remote_command_worker(self, label: str, command_callable) -> None:
        try:
            response = command_callable()
            self.ui_queue.put(("remote_command_result", label, response))
        except ControllerCommandRejected as exc:
            self.ui_queue.put(("remote_command_error", label, str(exc), exc.payload))
        except (ControllerConnectionError, ControllerError) as exc:
            self.ui_queue.put(("remote_command_error", label, str(exc), None))

    def _complete_remote_command(self, label: str, response: dict) -> None:
        self.remote_request_in_flight = False
        if label == "home calibration":
            self.remote_home_calibration_required = False
            self.connection_var.set("Connected to Pi backend.")
        message = str(response.get("message", f"Remote {label} request completed."))
        self.log_message(f"Remote {label}: {message}")
        self.set_status("running", message)
        self._update_control_interactivity()
        if self.remote_sync is not None:
            self.remote_sync.request_immediate_poll()

    def _fail_remote_command(self, label: str, message: str, payload: dict | None) -> None:
        self.remote_request_in_flight = False
        if label == "home calibration":
            self.remote_home_calibration_required = True
            self.connection_var.set("Connected. Waiting for home calibration approval.")
        self.log_message(f"Remote {label} failed: {message}")
        self.set_status("error", message)
        self._update_control_interactivity()
        if payload:
            self.message_var.set(str(payload.get("message", message)))

    def _get_remote_controller_for_automation(self) -> RemoteController:
        if not self.is_remote_mode() or not self.remote_connected or self.remote_controller is None:
            raise RuntimeError("Connect to the Pi backend before starting the remote automated process.")
        return self.remote_controller

    @staticmethod
    def _remote_status_has_terminal_error(status: dict) -> bool:
        task_state = str(status.get("task_state") or "").upper()
        orchestrator_state = str(status.get("orchestrator_state") or "").upper()
        return task_state.endswith("_ERROR") or orchestrator_state == "TASK_ERROR"

    def _poll_remote_status_fresh(self) -> dict:
        controller = self._get_remote_controller_for_automation()
        status = controller.get_status_fresh()
        self.ui_queue.put(("remote_status", status))
        return status

    def _wait_for_remote_backend_idle(self, label: str, timeout_s: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout_s
        saw_busy = False
        last_status: dict | None = None

        while time.monotonic() < deadline:
            self._check_stop()
            status = self._poll_remote_status_fresh()
            last_status = status
            busy = self._backend_busy_from_status(status)
            if busy:
                saw_busy = True
            elif saw_busy or status.get("current_task") is None:
                if self._remote_status_has_terminal_error(status):
                    raise RuntimeError(str(status.get("latest_message", f"Remote {label} failed.")))
                return status
            time.sleep(0.2)

        if last_status is not None and self._remote_status_has_terminal_error(last_status):
            raise RuntimeError(str(last_status.get("latest_message", f"Remote {label} failed.")))
        raise RuntimeError(f"Timed out waiting for remote {label} to finish.")

    def _run_remote_command_and_wait(self, label: str, command_callable, timeout_s: float = 90.0) -> dict:
        controller = self._get_remote_controller_for_automation()
        response = command_callable()
        message = str(response.get("message", f"Accepted remote {label} request."))
        self.worker_log(f"Remote {label}: {message}")

        status = controller.get_status_fresh()
        self.ui_queue.put(("remote_status", status))
        if not self._backend_busy_from_status(status):
            if self._remote_status_has_terminal_error(status):
                raise RuntimeError(str(status.get("latest_message", f"Remote {label} failed.")))
            return status
        return self._wait_for_remote_backend_idle(label, timeout_s=timeout_s)

    def _remote_operational_max_mm(self) -> float:
        return max(0.0, float(config.GANTRY_MAX_MM) - (2.0 * float(config.VACUUM_CENTER_OFFSET_MM)))

    def _run_remote_home_for_automation(self) -> None:
        controller = self._get_remote_controller_for_automation()
        self._run_remote_command_and_wait("home", controller.home)

    def _run_remote_move_absolute_for_automation(self, target_mm: float) -> None:
        controller = self._get_remote_controller_for_automation()
        self._run_remote_command_and_wait(
            f"move to {target_mm:.2f} mm",
            lambda: controller.move_absolute(float(target_mm)),
        )

    def _run_remote_set_vacuum_for_automation(self, enabled: bool) -> None:
        controller = self._get_remote_controller_for_automation()
        self._run_remote_command_and_wait(
            f"vacuum {'on' if enabled else 'off'}",
            lambda: controller.set_vacuum(bool(enabled)),
            timeout_s=20.0,
        )

    def _run_remote_classify_for_automation(self) -> dict[str, Any]:
        controller = self._get_remote_controller_for_automation()
        status = self._run_remote_command_and_wait("classification", controller.classify_fly, timeout_s=30.0)
        result = status.get("classification_result") or {}
        if not result:
            raise RuntimeError("Remote classification completed without returning a classification result.")
        raw = dict(result.get("raw", {}) or {})
        normalized = {
            "class": str(result.get("result_class", "UNCERTAIN")),
            "confidence": float(result.get("confidence", 0.0)),
            "errors": list(result.get("errors", []) or []),
            "count": int(raw.get("count", 0) or 0),
            "image_path": raw.get("image_path"),
            "raw": raw,
        }
        return normalized

    def _update_control_interactivity(self) -> None:
        busy = (
            self.local_task_busy
            or self.remote_request_in_flight
            or (self.is_remote_mode() and self.remote_backend_busy)
            or self._pending_channel_setup_resume is not None
        )
        entry_state = "disabled" if busy else "normal"
        remote_calibration_locked = self.is_remote_mode() and self.remote_home_calibration_required
        controls_enabled = not busy and (not self.is_remote_mode() or self.remote_connected) and not remote_calibration_locked

        for widget in self.control_widgets:
            if widget in (self.stop_button, self.reset_button):
                continue
            if getattr(self, "clear_log_button", None) is widget:
                if remote_calibration_locked:
                    try:
                        widget.config(state=tk.DISABLED)
                    except tk.TclError:
                        pass
                    continue
                try:
                    widget.config(state=tk.NORMAL)
                except tk.TclError:
                    pass
                continue
            if widget in self.local_vision_widgets:
                local_vision_enabled = not busy and (not self.is_remote_mode() or self.remote_connected)
                target_state = tk.NORMAL if local_vision_enabled else tk.DISABLED
                target_entry_state = "normal" if local_vision_enabled else "disabled"
            elif widget in self.remote_unsupported_widgets and self.is_remote_mode():
                target_state = tk.DISABLED
                target_entry_state = "disabled"
            else:
                target_state = tk.NORMAL if controls_enabled else tk.DISABLED
                target_entry_state = "normal" if controls_enabled else "disabled"

            if self.is_remote_mode() and widget in self.motion_widgets and not self.remote_motion_available:
                target_state = tk.DISABLED
                target_entry_state = "disabled"
            if self.is_remote_mode() and getattr(self, "classify_button", None) is widget and not self.remote_classifier_available:
                target_state = tk.DISABLED
                target_entry_state = "disabled"
            if self.is_remote_mode() and getattr(self, "assay_button", None) is widget and not self.remote_assay_available:
                target_state = tk.DISABLED
                target_entry_state = "disabled"

            try:
                if isinstance(widget, ttk.Entry):
                    widget.config(state=target_entry_state)
                else:
                    widget.config(state=target_state)
            except tk.TclError:
                pass

        for toggle in self.toggle_widgets:
            enabled = controls_enabled
            if self.is_remote_mode():
                if toggle is self.vacuum_switch:
                    enabled = enabled and self.remote_vacuum_available
                elif toggle is self.vibration_switch:
                    enabled = enabled and self.remote_vibration_available
            toggle.set_enabled(enabled)

        stop_enabled = False
        if self.is_remote_mode():
            stop_enabled = self.remote_connected and (
                self.remote_stop_allowed or (self.local_task_busy and self.local_task_cancellable)
            )
        else:
            stop_enabled = self.local_task_busy and self.local_task_cancellable

        self.stop_button.config(state=tk.NORMAL if stop_enabled else tk.DISABLED)
        self.reset_button.config(state=tk.DISABLED if busy or (self.is_remote_mode() and not self.remote_connected) else tk.NORMAL)

    def _update_device_availability(self, actuator: str, status: str, error_detail: str | None) -> None:
        if status in {"available", "simulation"}:
            current_on = False
            if actuator == "vacuum":
                current_on = self.vacuum_switch.value
            elif actuator == "vibration":
                current_on = self.vibration_switch.value
            self.update_device_card_state(actuator, current_on)
            return

        if status == "deferred":
            state_var = self.device_state_text.get(actuator)
            detail_var = self.device_detail_text.get(actuator)
            state_label = self.device_state_labels.get(actuator)
            if state_var is None or detail_var is None or state_label is None:
                return
            state_var.set("DEFERRED")
            detail_var.set("Will initialize on first use")
            state_label.config(bg="#EEF2F6", fg="#344054")
            return

        state_var = self.device_state_text.get(actuator)
        detail_var = self.device_detail_text.get(actuator)
        state_label = self.device_state_labels.get(actuator)
        if state_var is None or detail_var is None or state_label is None:
            return

        state_var.set("UNAVAILABLE")
        detail_var.set(error_detail or "Remote subsystem unavailable")
        state_label.config(bg="#FDECEC", fg="#B42318")

    def _load_entry_fly_source_image(self):
        if self.entry_fly_source_image is not None:
            return self.entry_fly_source_image

        try:
            from PIL import Image
        except ImportError:
            return None

        image_path = self._resolve_asset_path(
            "3DDrosophilaFrontView.png",
            "drosophilafly.png",
            "drosophila.png",
            "drosphoila.png",
        )
        if image_path is None:
            return None

        try:
            image = Image.open(image_path).convert("RGBA")
        except OSError:
            return None

        cleaned = image.copy()

        base_display_size = 304
        longest_side = max(cleaned.size)
        if longest_side > 0 and longest_side != base_display_size:
            scale = base_display_size / longest_side
            normalized_size = (
                max(1, int(round(cleaned.width * scale))),
                max(1, int(round(cleaned.height * scale))),
            )
            resample = getattr(Image, "Resampling", Image)
            cleaned = cleaned.resize(normalized_size, resample.LANCZOS)

        self.entry_fly_source_image = cleaned
        return self.entry_fly_source_image

    def _sample_entry_photo_border(self, image):
        width, height = image.size
        sample_positions = []

        for fraction in (0.0, 0.22, 0.5, 0.78, 1.0):
            sample_positions.append((round((width - 1) * fraction), 0))
        for fraction in (0.12, 0.32, 0.5, 0.68, 0.88):
            sample_positions.append((width - 1, round((height - 1) * fraction)))
        for fraction in (1.0, 0.78, 0.5, 0.22, 0.0):
            sample_positions.append((round((width - 1) * fraction), height - 1))
        for fraction in (0.88, 0.68, 0.5, 0.32, 0.12):
            sample_positions.append((0, round((height - 1) * fraction)))

        return [(x_pos, y_pos, image.getpixel((x_pos, y_pos))) for x_pos, y_pos in sample_positions]

    def _build_entry_photo_stage(self, source_image):
        try:
            from PIL import Image
        except ImportError:
            return source_image

        margin_x = self._entry_scale(51)
        margin_y = self._entry_scale(44)
        stage_width = source_image.width + (margin_x * 2)
        stage_height = source_image.height + (margin_y * 2)
        stage_image = Image.new("RGBA", (stage_width, stage_height), (255, 255, 255, 255))
        stage_pixels = stage_image.load()

        border_samples = []
        for sample_x, sample_y, rgba in self._sample_entry_photo_border(source_image):
            border_samples.append((sample_x + margin_x, sample_y + margin_y, rgba))

        fade_span = min(margin_x, margin_y)
        for x_pos in range(stage_width):
            for y_pos in range(stage_height):
                accum_r = 0.0
                accum_g = 0.0
                accum_b = 0.0
                weight_sum = 0.0
                for sample_x, sample_y, rgba in border_samples:
                    dx = x_pos - sample_x
                    dy = y_pos - sample_y
                    distance_sq = (dx * dx) + (dy * dy) + 1.0
                    weight = 1.0 / (distance_sq ** 0.72)
                    accum_r += rgba[0] * weight
                    accum_g += rgba[1] * weight
                    accum_b += rgba[2] * weight
                    weight_sum += weight

                base_r = accum_r / weight_sum
                base_g = accum_g / weight_sum
                base_b = accum_b / weight_sum

                distance_to_outer_edge = min(x_pos, y_pos, stage_width - 1 - x_pos, stage_height - 1 - y_pos)
                white_mix = max(0.0, 1.0 - min(distance_to_outer_edge / fade_span, 1.0))
                final_r = int(round(base_r + ((255 - base_r) * white_mix)))
                final_g = int(round(base_g + ((255 - base_g) * white_mix)))
                final_b = int(round(base_b + ((255 - base_b) * white_mix)))
                stage_pixels[x_pos, y_pos] = (final_r, final_g, final_b, 255)

        stage_image.alpha_composite(source_image, (margin_x, margin_y))
        return stage_image

    def _display_entry_photo(self):
        try:
            from PIL import Image, ImageTk
        except ImportError:
            image_path = self._resolve_asset_path(
                "3DDrosophilaFrontView.png",
                "drosophilafly.png",
                "drosophila.png",
                "drosphoila.png",
            )
            if image_path is None:
                self.entry_fly_display_image = None
                self.entry_fly_label.config(image="", text="Fly preview unavailable")
                return
            try:
                fallback_photo = tk.PhotoImage(file=str(image_path))
                fallback_photo = self._fit_photoimage(fallback_photo, self.entry_profile["fly_max"])
                self.entry_fly_display_image = fallback_photo
                self.entry_fly_label.config(image=self.entry_fly_display_image, text="", bg="#FFFFFF")
            except tk.TclError:
                self.entry_fly_display_image = None
                self.entry_fly_label.config(image="", text="Fly preview unavailable")
            return

        source_image = self._load_entry_fly_source_image()
        if source_image is None:
            self.entry_fly_display_image = None
            self.entry_fly_label.config(image="", text="Fly preview unavailable")
            return

        image = self._build_entry_photo_stage(source_image)
        max_bound = self._entry_scale(self.entry_profile["fly_max"])
        max_bounds = (max_bound, max_bound)
        resample = getattr(Image, "Resampling", Image)
        image.thumbnail(max_bounds, resample.LANCZOS)
        self.entry_fly_display_image = ImageTk.PhotoImage(image)
        self.entry_fly_label.config(image=self.entry_fly_display_image, text="", bg="#FFFFFF")

    def _build_entry_fly_frames(self):
        if self.entry_fly_frames:
            return self.entry_fly_frames

        source_image = self._load_entry_fly_source_image()
        if source_image is None:
            return []

        try:
            from PIL import Image, ImageDraw, ImageEnhance, ImageTk
        except ImportError:
            return []

        frame_count = 20
        canvas_size = self._entry_scale(240)
        center_x = canvas_size // 2
        center_y = self._entry_scale(104)

        built_frames = []
        for frame_index in range(frame_count):
            orbit_angle = (2.0 * math.pi * frame_index) / frame_count
            image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image, "RGBA")

            glow_center_y = self._entry_scale(194)
            draw.ellipse(
                (
                    self._entry_scale(56),
                    glow_center_y - self._entry_scale(11),
                    self._entry_scale(184),
                    glow_center_y + self._entry_scale(11),
                ),
                fill=(88, 237, 255, 28),
            )
            draw.ellipse(
                (
                    self._entry_scale(68),
                    glow_center_y - self._entry_scale(7),
                    self._entry_scale(172),
                    glow_center_y + self._entry_scale(7),
                ),
                fill=(88, 237, 255, 54),
            )
            draw.ellipse(
                (
                    self._entry_scale(78),
                    glow_center_y - self._entry_scale(3),
                    self._entry_scale(162),
                    glow_center_y + self._entry_scale(3),
                ),
                outline=(159, 247, 255, 180),
                width=self._entry_scale(2),
            )
            draw.arc(
                (
                    self._entry_scale(54),
                    glow_center_y - self._entry_scale(14),
                    self._entry_scale(186),
                    glow_center_y + self._entry_scale(14),
                ),
                start=18,
                end=162,
                fill=(146, 244, 255, 150),
                width=self._entry_scale(2),
            )

            frame_image = source_image.copy()
            x_shift = float(self._entry_scale(14)) * math.cos(orbit_angle)
            vertical_bob = -float(self._entry_scale(10)) * math.sin(orbit_angle)
            depth_factor = (math.sin(orbit_angle) + 1.0) / 2.0
            orbit_scale = 0.9 + (0.14 * depth_factor)
            rotation = 5.0 * math.cos(orbit_angle)
            brightness = 0.9 + (0.16 * depth_factor)

            if abs(brightness - 1.0) > 0.01:
                frame_image = ImageEnhance.Brightness(frame_image).enhance(brightness)

            base_width, base_height = frame_image.size
            scaled_size = (
                max(1, int(base_width * orbit_scale)),
                max(1, int(base_height * orbit_scale)),
            )
            resample = getattr(Image, "Resampling", Image)
            frame_image = frame_image.resize(scaled_size, resample.LANCZOS)
            frame_image = frame_image.rotate(rotation, resample=resample.BICUBIC, expand=True)

            max_frame_bound = self._entry_scale(146)
            max_bounds = (max_frame_bound, max_frame_bound)
            frame_image.thumbnail(max_bounds, resample.LANCZOS)
            paste_x = int(center_x - (frame_image.width / 2) + x_shift)
            paste_y = int(center_y - (frame_image.height / 2) + vertical_bob)
            image.alpha_composite(frame_image, (paste_x, paste_y))

            built_frames.append(ImageTk.PhotoImage(image))

        self.entry_fly_frames = built_frames
        return self.entry_fly_frames

    def start_entry_animation(self):
        if getattr(self, "entry_fly_label", None) is None:
            return

        self.stop_entry_animation()

        if not self.entry_frame.winfo_ismapped():
            self.entry_fly_job = self.root.after(60, self.start_entry_animation)
            return

        if not self.entry_fly_frames:
            self._build_entry_fly_frames()

        if not self.entry_fly_frames:
            self.entry_fly_label.config(text="Fly preview unavailable")
            return

        self.entry_fly_frame_index = 0
        self._advance_entry_animation()

    def _advance_entry_animation(self):
        if not self.entry_fly_frames or not self.entry_frame.winfo_ismapped():
            self.entry_fly_job = None
            return

        frame = self.entry_fly_frames[self.entry_fly_frame_index]
        self.entry_fly_label.config(image=frame, text="")
        self.entry_fly_frame_index = (self.entry_fly_frame_index + 1) % len(self.entry_fly_frames)
        self.entry_fly_job = self.root.after(80, self._advance_entry_animation)

    def stop_entry_animation(self):
        if self.entry_fly_job is not None:
            self.root.after_cancel(self.entry_fly_job)
            self.entry_fly_job = None

    def create_status_section(self, parent):
        status_frame = ttk.LabelFrame(parent, text="System Status", style="Status.TLabelframe", padding="10")
        status_frame.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        status_frame.columnconfigure(1, weight=1)

        ttk.Label(status_frame, text="State:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky=tk.W, pady=2)
        self.state_label = tk.Label(
            status_frame,
            textvariable=self.state_var,
            bg="#4CAF50",
            fg="white",
            relief="sunken",
            font=("Arial", 10, "bold"),
            padx=10,
            pady=2,
        )
        self.state_label.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Message:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky=tk.W, pady=2)
        self.message_label = tk.Label(
            status_frame,
            textvariable=self.message_var,
            bg="#4CAF50",
            fg="white",
            relief="sunken",
            font=("Arial", 10),
            padx=5,
            pady=2,
            anchor="w",
        )
        self.message_label.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Mode:", font=("Arial", 10, "bold")).grid(row=2, column=0, sticky=tk.W, pady=2)
        _, mode_color = self._local_mode_presentation()
        self.mode_label = tk.Label(
            status_frame,
            textvariable=self.mode_var,
            bg=mode_color,
            fg="white",
            relief="sunken",
            font=("Arial", 10),
            padx=5,
            pady=2,
        )
        self.mode_label.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Controller:", font=("Arial", 10, "bold")).grid(row=3, column=0, sticky=tk.W, pady=2)
        controller_row = ttk.Frame(status_frame)
        controller_row.grid(row=3, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)
        controller_row.columnconfigure(0, weight=1)
        self.controller_mode_combo = ttk.Combobox(
            controller_row,
            textvariable=self.controller_mode_choice,
            values=("Local", "Remote"),
            state="readonly",
            width=14,
        )
        self.controller_mode_combo.grid(row=0, column=0, sticky=(tk.W, tk.E))
        self.controller_mode_combo.bind("<<ComboboxSelected>>", self.on_controller_mode_changed)

        self.reconnect_button = ttk.Button(controller_row, text="Reconnect", command=self.reconnect_remote)
        self.reconnect_button.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(status_frame, text="Remote URL:", font=("Arial", 10, "bold")).grid(row=4, column=0, sticky=tk.W, pady=2)
        self.remote_url_entry = ttk.Entry(status_frame, textvariable=self.remote_url_var)
        self.remote_url_entry.grid(row=4, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="API Key:", font=("Arial", 10, "bold")).grid(row=5, column=0, sticky=tk.W, pady=2)
        self.remote_api_key_entry = ttk.Entry(status_frame, textvariable=self.remote_api_key_var, show="*")
        self.remote_api_key_entry.grid(row=5, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Connection:", font=("Arial", 10, "bold")).grid(row=6, column=0, sticky=tk.W, pady=2)
        self.connection_label = tk.Label(
            status_frame,
            textvariable=self.connection_var,
            bg=mode_color,
            fg="white",
            relief="sunken",
            font=("Arial", 10),
            padx=5,
            pady=2,
            anchor="w",
        )
        self.connection_label.grid(row=6, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Output Dir:", font=("Arial", 10, "bold")).grid(row=7, column=0, sticky=tk.W, pady=2)
        self.output_dir_label = tk.Label(
            status_frame,
            textvariable=self.output_dir_var,
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 9),
            padx=5,
            pady=2,
            anchor="w",
            justify="left",
        )
        self.output_dir_label.grid(row=7, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Workspace:", font=("Arial", 10, "bold")).grid(row=8, column=0, sticky=tk.W, pady=2)
        self.workspace_label = tk.Label(
            status_frame,
            text="Use the Channel, Sexing, and Assay tabs below for vision setup and results.",
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 9),
            padx=5,
            pady=2,
            anchor="w",
            justify="left",
        )
        self.workspace_label.grid(row=8, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

    def create_main_content(self, parent):
        content_frame = ttk.Frame(parent)
        content_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.N, tk.S, tk.W, tk.E), pady=(0, 10))
        content_frame.columnconfigure(0, weight=0)
        content_frame.columnconfigure(1, weight=1, minsize=520 if self.gui_profile.is_macos else 0)
        content_frame.columnconfigure(2, weight=0)
        content_frame.rowconfigure(0, weight=1)

        self.create_motion_control(content_frame)
        self.create_workspace_tabs(content_frame)
        self.create_device_operations(content_frame)

    def create_motion_control(self, parent):
        motion_frame = ttk.LabelFrame(parent, text="Motion Control", style="Motion.TLabelframe", padding="10")
        motion_frame.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E), padx=(0, 8))

        home_button = self.make_button(motion_frame, "Home", "#4CAF50", self.home_gantry)
        home_button.grid(row=0, column=0, pady=3, sticky=(tk.W, tk.E))
        self.motion_widgets.append(home_button)

        positions = [
            ("Channel", config.CHANNEL_CENTER),
            ("Chamber", config.CHAMBER_CENTER),
            ("Tube 1", config.TUBE_1_CENTER),
            ("Tube 2", config.TUBE_2_CENTER),
            ("Tube 3", config.TUBE_3_CENTER),
            ("Tube 4", config.TUBE_4_CENTER),
            ("Tube 5", config.TUBE_5_CENTER),
        ]

        for index, (label, position) in enumerate(positions, start=1):
            button = self.make_button(
                motion_frame,
                label,
                "#2196F3",
                lambda pos=position, name=label: self.move_to_position(pos, name),
            )
            button.grid(row=index, column=0, pady=2, sticky=(tk.W, tk.E))
            self.motion_widgets.append(button)

        ttk.Separator(motion_frame, orient="horizontal").grid(row=8, column=0, sticky=(tk.W, tk.E), pady=8)
        ttk.Label(motion_frame, text="Manual Position (mm):", font=("Arial", 9, "bold")).grid(
            row=9,
            column=0,
            pady=(5, 2),
            sticky=tk.W,
        )
        self.manual_move_entry = ttk.Entry(motion_frame, width=12, font=("Arial", 10))
        self.manual_move_entry.grid(row=10, column=0, pady=2, sticky=(tk.W, tk.E))
        self.register_control(self.manual_move_entry)
        self.motion_widgets.append(self.manual_move_entry)

        manual_button = self.make_button(motion_frame, "Move Absolute", "#607D8B", self.manual_move)
        manual_button.grid(row=11, column=0, pady=3, sticky=(tk.W, tk.E))
        self.motion_widgets.append(manual_button)

        ttk.Label(motion_frame, text="Current Position:", font=("Arial", 9, "bold")).grid(
            row=12,
            column=0,
            pady=(10, 2),
            sticky=tk.W,
        )
        self.motion_position_label = tk.Label(
            motion_frame,
            textvariable=self.position_var,
            bg="white",
            relief="sunken",
            font=("Arial", 10),
            padx=5,
            pady=2,
            anchor="w",
        )
        self.motion_position_label.grid(row=13, column=0, sticky=(tk.W, tk.E))

    def create_workspace_tabs(self, parent):
        workspace_frame = ttk.LabelFrame(parent, text="Vision Workspace", style="Log.TLabelframe", padding="10")
        workspace_frame.grid(row=0, column=1, sticky=(tk.N, tk.S, tk.W, tk.E), padx=8)
        workspace_frame.columnconfigure(0, weight=1)
        workspace_frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(workspace_frame)
        notebook.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.workspace_notebook = notebook

        channel_tab = ttk.Frame(notebook, padding="10")
        channel_tab.columnconfigure(0, weight=1)
        channel_tab.rowconfigure(2, weight=1)
        notebook.add(channel_tab, text="Channel")
        self.create_channel_tab(channel_tab)
        self.workspace_channel_tab = channel_tab

        sexing_tab = ttk.Frame(notebook, padding="10")
        sexing_tab.columnconfigure(0, weight=1)
        sexing_tab.rowconfigure(0, weight=1)
        notebook.add(sexing_tab, text="Sexing")
        self.create_sexing_tab(sexing_tab)
        self.workspace_sexing_tab = sexing_tab

        assay_tab = ttk.Frame(notebook, padding="10")
        assay_tab.columnconfigure(0, weight=1)
        notebook.add(assay_tab, text="Assay")
        self.create_assay_tab(assay_tab)
        self.workspace_assay_tab = assay_tab

    def _show_workspace_tab(self, name: str) -> None:
        if self.workspace_notebook is None:
            return
        lookup = {
            "channel": self.workspace_channel_tab,
            "sexing": self.workspace_sexing_tab,
            "assay": self.workspace_assay_tab,
        }
        target = lookup.get(str(name).strip().lower())
        if target is None:
            return
        try:
            self.workspace_notebook.select(target)
        except Exception:
            pass

    def create_channel_tab(self, parent):
        summary_card = ttk.LabelFrame(parent, text="Channel Detection", padding="10")
        summary_card.grid(row=0, column=0, sticky=(tk.W, tk.E))
        summary_card.columnconfigure(0, weight=1)
        tk.Label(
            summary_card,
            textvariable=self.channel_workspace_summary_var,
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 9),
            padx=8,
            pady=6,
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E))

        ttk.Label(summary_card, text="Detection:", font=("Arial", 9, "bold")).grid(row=1, column=0, sticky=tk.W, pady=(8, 2))
        tk.Label(
            summary_card,
            textvariable=self.detection_var,
            bg="#FFFFFF",
            relief="sunken",
            font=("Arial", 9),
            padx=5,
            pady=2,
            anchor="w",
            justify="left",
        ).grid(row=1, column=1, sticky=(tk.W, tk.E), pady=(8, 2))

        ttk.Label(summary_card, text="Output Dir:", font=("Arial", 9, "bold")).grid(row=2, column=0, sticky=tk.W, pady=2)
        tk.Label(
            summary_card,
            textvariable=self.output_dir_var,
            bg="#FFFFFF",
            relief="sunken",
            font=("Arial", 9),
            padx=5,
            pady=2,
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=2, column=1, sticky=(tk.W, tk.E), pady=2)

        actions = ttk.Frame(summary_card)
        actions.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        self.detect_channel_button = self.make_button(actions, "Detect Channel", "#9C27B0", self.run_channel_detection)
        self.detect_channel_button.grid(row=0, column=0, sticky=tk.W)
        self.local_vision_widgets.append(self.detect_channel_button)

        self.camera_roles_button = self.make_button(actions, "Camera Roles", "#455A64", self.open_camera_roles)
        self.camera_roles_button.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        self.local_vision_widgets.append(self.camera_roles_button)

        self.channel_setup_button = self.make_button(actions, "Open Channel Setup", "#607D8B", self.open_channel_setup)
        self.channel_setup_button.grid(row=0, column=2, sticky=tk.W, padx=(8, 0))
        self.local_vision_widgets.append(self.channel_setup_button)

        preview_frame = ttk.LabelFrame(parent, text="Annotated Preview", padding="10")
        preview_frame.grid(row=2, column=0, sticky=(tk.N, tk.S, tk.W, tk.E), pady=(10, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        self.preview_label = tk.Label(
            preview_frame,
            text="Waiting for channel detection image...",
            bg="black",
            fg="white",
            font=("Arial", 10, "bold"),
            anchor=tk.CENTER,
            justify="center",
        )
        self.preview_label.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.preview_label.bind("<Configure>", self._refresh_preview_image)

    def create_sexing_tab(self, parent):
        preview_card = ttk.LabelFrame(parent, text="Classification Preview", padding="10")
        preview_card.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        preview_card.columnconfigure(0, weight=1)
        preview_card.rowconfigure(0, weight=1)
        self.sexing_preview_label = tk.Label(
            preview_card,
            bg="black",
            fg="white",
            text="Waiting for classification image...",
            anchor="center",
            justify="center",
        )
        self.sexing_preview_label.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.sexing_preview_label.bind("<Configure>", self._refresh_sexing_preview_image)

        summary_card = ttk.LabelFrame(parent, text="Sexing / Routing", padding="10")
        summary_card.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(10, 0))
        summary_card.columnconfigure(1, weight=1)

        tk.Label(
            summary_card,
            textvariable=self.sexing_workspace_summary_var,
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 9),
            padx=8,
            pady=6,
            anchor="w",
            justify="left",
            wraplength=560,
        ).grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 8))

        sexing_rows = [
            ("Stage", self.sort_stage_var),
            ("Cycle", self.sort_cycle_var),
            ("In Chamber", self.sort_detected_var),
            ("Pickup X", self.sort_pickup_var),
            ("Last Sex", self.sort_last_sex_var),
            ("Confidence", self.sort_confidence_var),
            ("Destination", self.sort_destination_var),
        ]
        for row_index, (label_text, value_var) in enumerate(sexing_rows, start=1):
            ttk.Label(summary_card, text=f"{label_text}:", font=("Arial", 9, "bold")).grid(row=row_index, column=0, sticky=tk.W, pady=2)
            tk.Label(
                summary_card,
                textvariable=value_var,
                bg="#FFFFFF",
                relief="sunken",
                font=("Arial", 9),
                padx=5,
                pady=2,
                anchor="w",
                justify="left",
            ).grid(row=row_index, column=1, sticky=(tk.W, tk.E), pady=2)

        tube_frame = ttk.LabelFrame(parent, text="Tube Counts", padding="10")
        tube_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=(10, 0))
        tube_frame.columnconfigure(1, weight=1)
        tube_roles = {
            "T1": "Damaged / Rejected",
            "T2": "Male",
            "T3": "Female",
            "T4": "Male",
            "T5": "Female",
        }
        for row_index, tube_key in enumerate(("T1", "T2", "T3", "T4", "T5")):
            ttk.Label(tube_frame, text=f"{tube_key}:", font=("Arial", 9, "bold")).grid(row=row_index, column=0, sticky=tk.W, pady=2)
            ttk.Label(tube_frame, text=tube_roles[tube_key], font=("Arial", 8), foreground="#52606D").grid(row=row_index, column=1, sticky=tk.W, pady=2)
            tk.Label(
                tube_frame,
                textvariable=self.sort_tube_count_vars[tube_key],
                bg="#FFFFFF",
                relief="sunken",
                font=("Arial", 9),
                padx=5,
                pady=2,
                width=8,
                anchor="center",
            ).grid(row=row_index, column=2, sticky=tk.E, padx=(8, 0), pady=2)

        ttk.Label(summary_card, text="Notes:", font=("Arial", 9, "bold")).grid(row=len(sexing_rows) + 1, column=0, sticky=tk.NW, pady=(8, 2))
        tk.Label(
            summary_card,
            textvariable=self.sort_notes_var,
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 8),
            padx=5,
            pady=4,
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=len(sexing_rows) + 1, column=1, sticky=(tk.W, tk.E), pady=(8, 2))

    def create_assay_tab(self, parent):
        assay_card = ttk.LabelFrame(parent, text="Assay", padding="10")
        assay_card.grid(row=0, column=0, sticky=(tk.W, tk.E))
        assay_card.columnconfigure(0, weight=1)

        tk.Label(
            assay_card,
            textvariable=self.assay_workspace_summary_var,
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 9),
            padx=8,
            pady=6,
            anchor="w",
            justify="left",
            wraplength=560,
        ).grid(row=0, column=0, sticky=(tk.W, tk.E))

        button_row = ttk.Frame(assay_card)
        button_row.grid(row=1, column=0, sticky=tk.W, pady=(10, 0))

        self.assay_setup_button = self.make_button(button_row, "Open Assay Setup", "#607D8B", self.open_fin6_setup)
        self.assay_setup_button.grid(row=0, column=0, sticky=tk.W)
        self.local_vision_widgets.append(self.assay_setup_button)

        self.assay_button = self.make_button(button_row, "Run Assay", "#9C27B0", self.run_assay)
        self.assay_button.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))

    def create_device_operations(self, parent):
        controls_frame = ttk.Frame(parent)
        controls_frame.grid(row=0, column=2, sticky=(tk.N, tk.S))
        controls_frame.columnconfigure(0, weight=1)
        controls_frame.rowconfigure(1, weight=1)

        device_padding = (8, 6) if self._is_compact_screen() else (10, 8)
        device_frame = ttk.LabelFrame(
            controls_frame,
            text="Device Control",
            style="Device.TLabelframe",
            padding=device_padding,
        )
        device_frame.grid(row=0, column=0, sticky=(tk.W, tk.E))
        device_frame.columnconfigure(0, weight=1)

        self.vacuum_switch = self.create_device_card(
            device_frame,
            row=0,
            actuator="vacuum",
            title="Vacuum",
            description="Controls pickup suction for holding or releasing flies.",
            command=self.set_vacuum_from_ui,
        )
        self.register_toggle(self.vacuum_switch)

        self.vibration_switch = self.create_device_card(
            device_frame,
            row=1,
            actuator="vibration",
            title="Vibration",
            description="Runs the vibration motor used during assay handling.",
            command=self.set_vibration_from_ui,
        )
        self.register_toggle(self.vibration_switch)

        note_frame = ttk.LabelFrame(controls_frame, text="Workspace Note", padding=(10, 8))
        note_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.S), pady=(10, 0))
        tk.Label(
            note_frame,
            text="Live channel, sexing, routing, and assay actions are separated into the tabs in the Vision Workspace.",
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 8),
            padx=6,
            pady=6,
            anchor="w",
            justify="left",
            wraplength=220,
        ).grid(row=0, column=0, sticky=(tk.W, tk.E))

    def create_system_controls(self, parent):
        system_frame = ttk.LabelFrame(parent, text="System Control", padding="10")
        system_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))

        self.start_button = self.make_button(system_frame, "START", "#4CAF50", self.system_start)
        self.start_button.grid(row=0, column=0, pady=3, padx=5, sticky=(tk.W, tk.E))

        self.stop_button = self.make_button(system_frame, "STOP", "#F44336", self.system_stop)
        self.stop_button.grid(row=0, column=1, pady=3, padx=5, sticky=(tk.W, tk.E))
        self.stop_button.config(state=tk.DISABLED)

        self.reset_button = self.make_button(system_frame, "RESET", "#FF9800", self.system_reset)
        self.reset_button.grid(row=0, column=2, pady=3, padx=5, sticky=(tk.W, tk.E))

        self.clear_log_button = self.make_button(system_frame, "CLEAR LOG", "#607D8B", self.clear_log)
        self.clear_log_button.grid(row=0, column=3, pady=3, padx=5, sticky=(tk.W, tk.E))

        for column in range(4):
            system_frame.columnconfigure(column, weight=1)

    def create_log_section(self, parent):
        log_frame = ttk.LabelFrame(parent, text="Activity Log", style="Log.TLabelframe", padding="10")
        log_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.N, tk.S, tk.W, tk.E), pady=(15, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#F8F8F8",
            relief="sunken",
            borderwidth=1,
        )
        self.log_text.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))

    def create_footer_banners(self, parent, row: int, column: int, background: str, pady=(12, 0)):
        footer_frame = tk.Frame(parent, bg=background)
        footer_frame.grid(row=row, column=column, columnspan=3, sticky=(tk.W, tk.E), pady=pady)

        banner_box_width = self.entry_profile["footer_w"]
        banner_box_height = self.entry_profile["footer_h"]
        for gap_column in (0, 2, 4, 6):
            footer_frame.columnconfigure(gap_column, weight=1)
        for banner_column in (1, 3, 5):
            footer_frame.columnconfigure(banner_column, weight=0, minsize=banner_box_width)

        banner_specs = (
            ("Genetcs_Logo_Banner.png",),
            ("Clemson University Banner Logo.png",),
            ("ECE_Banner_Clemson.png", "ECE_Banner_Clemson.jpg"),
        )

        self.footer_banner_images = []
        for index, candidate_names in enumerate(banner_specs):
            banner_column = (index * 2) + 1
            banner_cell = tk.Frame(
                footer_frame,
                bg=background,
                width=banner_box_width,
                height=banner_box_height,
                bd=0,
                highlightthickness=0,
            )
            banner_cell.grid(row=0, column=banner_column, sticky="")
            banner_cell.grid_propagate(False)
            banner_cell.columnconfigure(0, weight=1)
            banner_cell.rowconfigure(0, weight=1)

            banner_label = tk.Label(
                banner_cell,
                bg=background,
                bd=0,
                highlightthickness=0,
            )
            banner_label.grid(row=0, column=0, sticky="")
            banner_path = self._resolve_asset_path(*candidate_names)
            if banner_path is None:
                continue

            try:
                from PIL import Image, ImageTk

                image = Image.open(banner_path).convert("RGBA")
                alpha_bbox = image.getchannel("A").getbbox()
                if alpha_bbox is not None:
                    image = image.crop(alpha_bbox)
                resample = getattr(Image, "Resampling", Image)
                image.thumbnail((banner_box_width, banner_box_height), resample.LANCZOS)
                banner_photo = ImageTk.PhotoImage(image)
                self.footer_banner_images.append(banner_photo)
                banner_label.config(image=banner_photo)
            except Exception:
                try:
                    if banner_path.suffix.lower() == ".png":
                        banner_photo = tk.PhotoImage(file=str(banner_path))
                        banner_photo = self._fit_photoimage(banner_photo, banner_box_width)
                        self.footer_banner_images.append(banner_photo)
                        banner_label.config(image=banner_photo)
                    else:
                        banner_label.config(text="")
                except tk.TclError:
                    banner_label.config(text="")

    def make_button(self, parent, text: str, color: str, command):
        button = ActionButton(
            parent,
            text=text,
            color=color,
            font=("Arial", 10, "bold"),
            command=command,
            active_color=self._blend_hex(color, "#111111", 0.18),
            padx=12,
            pady=self.gui_profile.standard_button_pady,
        )
        self.register_control(button)
        return button

    def create_device_card(self, parent, row: int, actuator: str, title: str, description: str, command):
        compact = self._is_compact_screen()
        card_padx = 8 if compact else 10
        card_pady = 6 if compact else 8
        header_font_size = 10 if compact else 11
        badge_font_size = 7 if compact else 8
        desc_font_size = 7 if compact else 8
        detail_font_size = 9 if compact else 10
        wrap_length = 200 if compact else 220
        switch_width = 70 if compact else 78
        switch_height = 32 if compact else 38
        card = tk.Frame(
            parent,
            bg="#FFFFFF",
            highlightbackground="#D4DCE5",
            highlightthickness=1,
            bd=0,
            padx=card_padx,
            pady=card_pady,
        )
        card.grid(row=row, column=0, pady=(0, 8 if row == 0 else 0), sticky=(tk.W, tk.E))
        card.columnconfigure(0, weight=1)

        header = tk.Frame(card, bg="#FFFFFF")
        header.grid(row=0, column=0, sticky=(tk.W, tk.E))
        header.columnconfigure(0, weight=1)

        tk.Label(
            header,
            text=title,
            bg="#FFFFFF",
            fg="#1F2933",
            font=("Arial", header_font_size, "bold"),
        ).grid(row=0, column=0, sticky=tk.W)

        state_var = tk.StringVar(value="OFF")
        detail_var = tk.StringVar(value="Currently off")
        state_label = tk.Label(
            header,
            textvariable=state_var,
            bg="#EEF2F6",
            fg="#52606D",
            font=("Arial", badge_font_size, "bold"),
            padx=8 if compact else 10,
            pady=3 if compact else 4,
        )
        state_label.grid(row=0, column=1, sticky=tk.E)
        self.device_state_labels[actuator] = state_label
        self.device_state_text[actuator] = state_var
        self.device_detail_text[actuator] = detail_var

        tk.Label(
            card,
            text=description,
            bg="#FFFFFF",
            fg="#52606D",
            font=("Arial", desc_font_size),
            justify="left",
            wraplength=wrap_length,
        ).grid(row=1, column=0, pady=(4, 6), sticky=tk.W)

        control_row = tk.Frame(card, bg="#FFFFFF")
        control_row.grid(row=2, column=0, sticky=(tk.W, tk.E))
        control_row.columnconfigure(1, weight=1)

        switch = SliderSwitch(
            control_row,
            command=command,
            initial=False,
            width=switch_width,
            height=switch_height,
            bg="#FFFFFF",
        )
        switch.grid(row=0, column=0, sticky=tk.W)

        tk.Label(
            control_row,
            textvariable=detail_var,
            bg="#FFFFFF",
            fg="#1F2933",
            font=("Arial", detail_font_size, "bold"),
            anchor="w",
        ).grid(row=0, column=1, padx=(10, 0), sticky=(tk.W, tk.E))

        self.update_device_card_state(actuator, False)
        return switch

    def create_operations_logo(self, parent):
        logo_area = tk.Frame(parent, bg="#F8E8F0")
        logo_area.columnconfigure(0, weight=1)
        logo_area.rowconfigure(1, weight=1)
        logo_margin = 6 if self.gui_profile.is_macos else 10
        tk.Frame(logo_area, bg="#F8E8F0", height=logo_margin).grid(row=0, column=0, sticky=(tk.W, tk.E))

        self.operations_logo_label = tk.Label(
            logo_area,
            text="Team logo unavailable",
            bg="#F8E8F0",
            fg="#6B4C63",
            font=("Arial", 9, "bold"),
            justify="center",
            wraplength=150,
            anchor="center",
            bd=0,
            highlightthickness=0,
        )
        self.operations_logo_label.grid(row=1, column=0, sticky=tk.N)
        tk.Frame(logo_area, bg="#F8E8F0", height=logo_margin).grid(row=2, column=0, sticky=(tk.W, tk.E))
        self.load_operations_logo()
        return logo_area

    def load_operations_logo(self):
        logo_path = self._resolve_asset_path(
            "drosophilafly.png",
            "drosophila.png",
            "drosphoila.png",
        )
        if logo_path is None:
            self.operations_logo_image = None
            self.operations_logo_source = None
            self.operations_logo_photo_base = None
            self.operations_logo_label.config(
                image="",
                text="Team fly logo unavailable",
                bg="#F8E8F0",
            )
            return
        try:
            from PIL import Image, ImageTk

            image = Image.open(logo_path).convert("RGBA")
            self.operations_logo_source = image
            self.operations_logo_photo_base = None
        except Exception:
            try:
                fallback_logo = tk.PhotoImage(file=str(logo_path))
                self.operations_logo_photo_base = fallback_logo
                self.operations_logo_source = None
            except tk.TclError:
                self.operations_logo_image = None
                self.operations_logo_source = None
                self.operations_logo_photo_base = None
                self.operations_logo_label.config(
                    image="",
                    text="Team fly logo unavailable",
                    bg="#F8E8F0",
                )
                return

        self._apply_operations_logo_size(self.gui_profile.operations_logo_size)

    def _apply_operations_logo_size(self, max_logo_size: int) -> None:
        if max_logo_size <= 0:
            self.operations_logo_label.config(image="", text="")
            return

        if self.operations_logo_source is not None:
            try:
                from PIL import Image, ImageTk

                image = self.operations_logo_source.copy()
                resample = getattr(Image, "Resampling", Image)
                image.thumbnail((max_logo_size, max_logo_size), resample.LANCZOS)
                self.operations_logo_image = ImageTk.PhotoImage(image)
                self.operations_logo_label.config(
                    image=self.operations_logo_image,
                    text="",
                    bg="#F8E8F0",
                    compound="center",
                    anchor="center",
                )
                return
            except Exception:
                pass

        if self.operations_logo_photo_base is not None:
            fallback_logo = self._fit_photoimage(self.operations_logo_photo_base, max_logo_size)
            self.operations_logo_image = fallback_logo
            self.operations_logo_label.config(
                image=self.operations_logo_image,
                text="",
                bg="#F8E8F0",
                compound="center",
                anchor="center",
            )

    def _on_operations_resize(self, _event=None) -> None:
        if self._ops_resize_job is not None:
            self.root.after_cancel(self._ops_resize_job)
        self._ops_resize_job = self.root.after(50, self._update_operations_layout)

    def _update_operations_layout(self) -> None:
        self._ops_resize_job = None
        if not hasattr(self, "operations_frame"):
            return

        frame = self.operations_frame
        content = self.operations_content
        action_frame = self.operations_action_frame
        logo_area = self.operations_logo_area

        width = max(1, frame.winfo_width())
        height = max(1, frame.winfo_height())
        if width <= 1 or height <= 1:
            return

        # Estimate available height after label frame padding.
        available_height = max(1, height - 32)

        base_font = 10
        base_pady = self.gui_profile.standard_button_pady
        base_gap = self.gui_profile.operations_button_gap
        base_margin = self.gui_profile.operations_button_margin
        base_logo = self.gui_profile.operations_logo_size

        font_obj = tkfont.Font(font=("Arial", base_font, "bold"))
        button_height = font_obj.metrics("linespace") + (base_pady * 2) + 4
        buttons_height = (button_height * 3) + (base_gap * 2) + (base_margin * 2)

        # Decide layout mode based on width budget.
        min_side_by_side = 140 + base_logo + 32
        layout = "side_by_side" if width >= min_side_by_side else "stacked"

        if layout == "stacked":
            desired_height = buttons_height + base_logo + base_gap
        else:
            desired_height = max(buttons_height, base_logo + (base_margin * 2))

        # Linux: hide logo only when the panel is truly too short.
        hide_logo = sys.platform.startswith("linux") and available_height < (buttons_height + 18)
        if hide_logo and layout == "stacked":
            desired_height = buttons_height

        scale = 1.0 if available_height >= desired_height else (available_height / max(1, desired_height))
        scale = max(0.75, min(1.0, scale))

        if layout != self._ops_layout_mode:
            if layout == "stacked":
                content.columnconfigure(0, weight=1, minsize=126)
                content.columnconfigure(1, weight=0, minsize=0)
                action_frame.grid(row=0, column=0, sticky=(tk.N, tk.W, tk.E), padx=(0, 0), pady=(0, 8))
                logo_area.grid(row=1, column=0, sticky=(tk.N, tk.W, tk.E), padx=(0, 0))
            else:
                content.columnconfigure(0, weight=0, minsize=126)
                content.columnconfigure(1, weight=0, minsize=self.gui_profile.operations_logo_column_min)
                action_frame.grid(row=0, column=0, sticky=(tk.N, tk.W), padx=(0, 12), pady=(0, 0))
                logo_area.grid(row=0, column=1, sticky=(tk.N, tk.W, tk.E), padx=(0, 0))
            self._ops_layout_mode = layout

        # Update spacing and button sizing.
        scaled_pady = max(2, int(round(base_pady * scale)))
        scaled_gap = max(2, int(round(base_gap * scale)))
        scaled_margin = max(2, int(round(base_margin * scale)))
        scaled_font = max(8, int(round(base_font * scale)))

        if self._ops_top_spacer is not None:
            self._ops_top_spacer.configure(height=scaled_margin)
        if self._ops_gap1 is not None:
            self._ops_gap1.configure(height=scaled_gap)
        if self._ops_gap2 is not None:
            self._ops_gap2.configure(height=scaled_gap)
        if self._ops_bottom_spacer is not None:
            self._ops_bottom_spacer.configure(height=scaled_margin)

        for button_name in ("run_button", "assay_button", "classify_button"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.configure(font=("Arial", scaled_font, "bold"), pady=scaled_pady)

        scaled_logo = max(40, int(round(base_logo * scale)))
        if hide_logo:
            if not self.operations_logo_hidden:
                logo_area.grid_remove()
                self.operations_logo_hidden = True
        else:
            if self.operations_logo_hidden:
                logo_area.grid()
                self.operations_logo_hidden = False
            self._apply_operations_logo_size(scaled_logo)

    def register_control(self, widget):
        self.control_widgets.append(widget)

    def register_toggle(self, widget: SliderSwitch):
        self.toggle_widgets.append(widget)

    def update_position(self):
        if self.is_remote_mode():
            self.root.after(1000, self.update_position)
            return
        runtime = self._get_local_runtime_if_loaded()
        if runtime is None:
            self.root.after(1000, self.update_position)
            return
        try:
            motion = runtime["motion"]
            self.position_var.set(f"{motion.get_current_position():.2f} mm")
        except Exception as exc:
            self.log_message(f"Position update error: {exc}")
        self.root.after(1000, self.update_position)

    def process_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    self.log_message(item[1])
                elif kind == "status":
                    self.set_status(item[1], item[2])
                elif kind == "task_finished":
                    self.finish_task(item[1], item[2], item[3])
                    if item[1] == "channel setup prep" and item[2] and self._open_channel_setup_after_prep:
                        self._open_channel_setup_after_prep = False
                        self._open_channel_setup_panel(
                            self._pending_channel_setup_action_label or "Channel Detection Setup",
                            self._pending_channel_setup_resume,
                        )
                elif kind == "actuator":
                    self.update_actuator_state(item[1], item[2])
                elif kind == "classification_result":
                    self.show_classification_result(item[1])
                elif kind == "local_channel_detection":
                    self._apply_local_channel_detection(item[1])
                elif kind == "automation_snapshot":
                    self._apply_automation_snapshot(item[1])
                elif kind == "clear_stop":
                    self.stop_requested.clear()
                elif kind == "prompt_yes_no":
                    prompt_state = item[1]
                    prompt_state["response"] = messagebox.askyesno(prompt_state["title"], prompt_state["message"])
                    prompt_state["event"].set()
                elif kind == "remote_connection":
                    connection_state = ConnectionState(item[1])
                    self._apply_connection_state(connection_state, item[2])
                elif kind == "remote_status":
                    self._apply_remote_status(item[1])
                elif kind == "remote_preview":
                    self._apply_remote_preview(item[1], item[2])
                elif kind == "remote_preview_error":
                    self._fail_remote_preview(item[1])
                elif kind == "remote_command_result":
                    self._complete_remote_command(item[1], item[2])
                elif kind == "remote_command_error":
                    self._fail_remote_command(item[1], item[2], item[3])
        except queue.Empty:
            pass

        self.root.after(100, self.process_queue)

    def set_status(self, state: str, message: str):
        self.state_var.set(state.upper())
        self.message_var.set(message)
        state_lower = state.lower()
        if any(token in state_lower for token in ("error", "failed")):
            color = "#F44336"
        elif any(token in state_lower for token in ("stopped", "disconnect", "retry")):
            color = "#607D8B"
        elif "assay" in state_lower:
            color = "#9C27B0"
        elif any(token in state_lower for token in ("detect", "move", "pick", "home", "run", "start", "task", "connect")):
            color = "#2196F3" if "connect" not in state_lower else "#FF9800"
        elif "idle" in state_lower or "ready" in state_lower:
            color = "#4CAF50"
        else:
            color = "#9E9E9E"
        self.state_label.config(bg=color)
        self.message_label.config(bg=color)

    def log_message(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"{timestamp} - {message}\n")
        self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)
        self.log_message("Log cleared.")

    def update_actuator_state(self, actuator: str, enabled: bool):
        if actuator == "vacuum":
            self.vacuum_switch.set_value(enabled)
        elif actuator == "vibration":
            self.vibration_switch.set_value(enabled)
        self.update_device_card_state(actuator, enabled)

    def update_device_card_state(self, actuator: str, enabled: bool):
        state_var = self.device_state_text.get(actuator)
        detail_var = self.device_detail_text.get(actuator)
        state_label = self.device_state_labels.get(actuator)

        if state_var is None or detail_var is None or state_label is None:
            return

        if enabled:
            state_var.set("ON")
            detail_var.set("Currently on")
            state_label.config(bg="#D9F3EC", fg="#176A57")
        else:
            state_var.set("OFF")
            detail_var.set("Currently off")
            state_label.config(bg="#EEF2F6", fg="#52606D")

    def show_classification_result(self, result: dict):
        class_name = result.get("class", "UNCERTAIN")
        confidence = result.get("confidence", 0.0)
        errors = result.get("errors", [])
        chamber_count = result.get("count")
        self._show_workspace_tab("sexing")
        self._update_sexing_preview_from_result(result)
        self.sort_detected_var.set("--" if chamber_count is None else str(int(chamber_count)))
        self.sort_last_sex_var.set(str(class_name).title() if str(class_name).lower() in {"male", "female"} else str(class_name))
        self.sort_confidence_var.set(f"{float(confidence):.4f}")
        self.sort_notes_var.set(", ".join(str(error) for error in errors) if errors else "Manual classification complete.")
        message = f"Class: {class_name}\nConfidence: {confidence:.4f}"
        if chamber_count is not None:
            message += f"\nFlies in chamber: {int(chamber_count)}"
        if errors:
            message += f"\nErrors: {', '.join(errors)}"
        self.log_message(f"Classification result: {result}")
        messagebox.showinfo("Fly Classification", message)

    def set_controls_busy(self, busy: bool, cancellable: bool = False):
        self.local_task_busy = busy
        self.local_task_cancellable = cancellable
        self._update_control_interactivity()

    def start_task(self, name: str, state: str, message: str, target, cancellable: bool = False):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", f"Finish the current task before starting {name}.")
            return False

        self.stop_requested.clear()
        self.current_task_name = name
        self.current_task_cancellable = cancellable
        self.set_controls_busy(True, cancellable=cancellable)
        self.set_status(state, message)
        self.log_message(f"Starting {name}.")

        self.worker_thread = threading.Thread(
            target=self._task_runner,
            args=(name, target),
            daemon=True,
        )
        self.worker_thread.start()
        return True

    def finish_task(self, name: str, ok: bool, message: str):
        self.current_task_name = None
        self.current_task_cancellable = False
        self.set_controls_busy(False)
        self.stop_requested.clear()

        if ok:
            self.set_status("idle", message)
        else:
            state = "stopped" if "stopped" in message.lower() else "error"
            self.set_status(state, message)

        self.log_message(message)

    def _task_runner(self, name: str, target):
        writer = QueueWriter(self.ui_queue)
        ok = False
        message = f"{name} failed."

        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                target()
            ok = True
            message = f"{name} complete."
        except TaskCancelled:
            message = f"{name} stopped."
        except Exception:
            for line in traceback.format_exc().rstrip().splitlines():
                self.ui_queue.put(("log", line))
            message = f"{name} failed."
        finally:
            writer.flush()
            self.ui_queue.put(("task_finished", name, ok, message))

    def _default_channel_output_dir(self) -> Path:
        return CHANNEL_OUTPUT_DIR

    def _settings_channel_output_dir(self) -> Path | None:
        settings_path = FIN6_DIR / ".fly_tracking_gui_settings.json"
        if not settings_path.exists():
            return None

        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        value = data.get("channel_output_var")
        if not value:
            return None
        return Path(value)

    def _channel_output_candidates(self):
        candidates = []
        settings_dir = self._settings_channel_output_dir()
        if settings_dir is not None and settings_dir.exists():
            candidates.append(settings_dir)

        candidates.extend(
            [
                self._default_channel_output_dir(),
            ]
        )

        unique = []
        seen = set()
        for candidate in candidates:
            key = str(candidate)
            if key not in seen:
                unique.append(candidate)
                seen.add(key)
        return unique

    def _current_channel_output_dir(self) -> Path:
        candidates = self._channel_output_candidates()
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _resolve_channel_file(self, filename: str) -> Path:
        for candidate in self._channel_output_candidates():
            path = candidate / filename
            if path.exists():
                return path
        return self._current_channel_output_dir() / filename

    def _read_detection_result(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _apply_local_channel_detection(self, payload: dict[str, Any]) -> None:
        output_dir = Path(payload["output_dir"])
        annotated_path = Path(payload["annotated_path"])
        result_path = Path(payload["result_path"])
        result = payload.get("result") or {}

        output_text = str(output_dir)
        self._last_output_dir_text = output_text
        self.output_dir_var.set(output_text)

        positions = result.get("x_positions_mm", [])
        count = len(positions) if isinstance(positions, list) else 0
        remaining = bool(result.get("fly_remaining", False))
        modified = time.strftime("%H:%M:%S", time.localtime(result_path.stat().st_mtime))
        self.detection_var.set(f"remaining={remaining} count={count} last_update={modified}")
        self.last_result_mtime = result_path.stat().st_mtime
        self.last_used_detection_mtime = self.last_result_mtime
        self._awaiting_current_detection = False
        self._current_detection_baseline_mtime = self.last_result_mtime
        self._startup_preview_requested = False
        self._startup_preview_completed = True
        self._show_workspace_tab("channel")

        if annotated_path.exists():
            self.load_channel_preview(annotated_path)
            self.last_preview_mtime = annotated_path.stat().st_mtime
        else:
            self.set_preview_placeholder("Waiting for channel detection image...")

    def _reset_sorting_status_display(self) -> None:
        self.sort_stage_var.set("Waiting for START.")
        self.sort_cycle_var.set("0")
        self.sort_detected_var.set("--")
        self.sort_pickup_var.set("--")
        self.sort_last_sex_var.set("--")
        self.sort_confidence_var.set("--")
        self.sort_destination_var.set("--")
        self.sort_notes_var.set("Tube counts and classifier output will appear here during automated loading.")
        self._last_sexing_preview_key = None
        self.set_sexing_preview_placeholder("Waiting for classification image...")
        for var in self.sort_tube_count_vars.values():
            var.set("0 / 10")

    def _begin_new_detection_session(self, *, placeholder: str) -> None:
        baseline = self.last_used_detection_mtime
        if self.is_remote_mode():
            if self._last_remote_preview_source_mtime is not None:
                baseline = max(
                    float(self._last_remote_preview_source_mtime),
                    float(baseline) if baseline is not None else float(self._last_remote_preview_source_mtime),
                )
        elif self.last_result_mtime is not None:
            baseline = max(
                float(self.last_result_mtime),
                float(baseline) if baseline is not None else float(self.last_result_mtime),
            )
            current_result_path = self._resolve_channel_file("last_channel_result.json")
            if current_result_path.exists():
                current_result_mtime = current_result_path.stat().st_mtime
                baseline = max(
                    float(current_result_mtime),
                    float(baseline) if baseline is not None else float(current_result_mtime),
                )

        self._awaiting_current_detection = True
        self._current_detection_baseline_mtime = baseline
        self.preview_image = None
        self.preview_source_image = None
        self.last_preview_mtime = None
        self.detection_var.set("Waiting for current channel detection output.")
        if self.is_remote_mode():
            self._last_remote_preview_source_mtime = None
        self.set_preview_placeholder(placeholder)

    def _clear_channel_preview_state(self, *, clear_artifacts: bool, placeholder: str) -> None:
        self.preview_image = None
        self.preview_source_image = None
        self.last_preview_mtime = None
        self.last_result_mtime = None
        self.last_used_detection_mtime = None
        self._last_remote_preview_source_mtime = None
        self._awaiting_current_detection = False
        self._current_detection_baseline_mtime = None
        self._startup_preview_requested = False
        self._startup_preview_completed = False
        self.detection_var.set(placeholder)
        self.set_preview_placeholder(placeholder)

        if clear_artifacts and not self.is_remote_mode():
            for filename in ("last_channel_annotated.png", "last_channel_result.json"):
                artifact_path = self._resolve_channel_file(filename)
                with contextlib.suppress(OSError):
                    if artifact_path.exists():
                        artifact_path.unlink()

    def _maybe_request_startup_channel_preview(self) -> None:
        if self._startup_preview_requested or self._startup_preview_completed:
            return
        if not self._channel_setup_completed_this_session:
            self._begin_new_detection_session(placeholder="Waiting for calibration before channel preview...")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if self.remote_request_in_flight or self.remote_backend_busy:
            return
        if self._channel_setup_panel is not None and self._channel_setup_panel.is_open():
            return

        fin6_status = self._get_fin6_setup_status("Startup Channel Preview", show_errors=False)
        if fin6_status is None or not self._channel_setup_ready(fin6_status):
            self._begin_new_detection_session(placeholder="Waiting for calibration before channel preview...")
            return

        self._startup_preview_requested = True
        self._begin_new_detection_session(placeholder="Capturing current channel detection image...")

        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                self._startup_preview_requested = False
                return
            threading.Thread(target=self._startup_remote_channel_preview_worker, daemon=True).start()
            return

        threading.Thread(target=self._startup_local_channel_preview_worker, daemon=True).start()

    def _startup_local_channel_preview_worker(self) -> None:
        try:
            capture = self._run_local_channel_detection_capture()
            self.ui_queue.put(("local_channel_detection", capture))
        except Exception as exc:
            self.ui_queue.put(("log", f"Startup channel preview failed: {exc}"))
            self.ui_queue.put(("status", "idle", "Waiting for current channel detection output."))
            self._startup_preview_requested = False

    def _startup_remote_channel_preview_worker(self) -> None:
        try:
            self._run_remote_channel_detection_capture()
        except Exception as exc:
            self.ui_queue.put(("log", f"Startup remote channel preview failed: {exc}"))
            self.ui_queue.put(("status", "idle", "Waiting for current channel detection output."))
            self._startup_preview_requested = False

    def _apply_automation_snapshot(self, snapshot: dict[str, Any]) -> None:
        stage = str(snapshot.get("stage") or "running").strip() or "running"
        cycle_index = int(snapshot.get("cycle_index") or 0)
        pickup_position = snapshot.get("pickup_position_mm")
        destination_label = snapshot.get("destination_label")
        classification = snapshot.get("classification") or {}
        classification_count = classification.get("count")

        self.sort_stage_var.set(stage.replace("_", " ").title())
        self.sort_cycle_var.set(str(cycle_index))
        self.sort_detected_var.set("--" if classification_count is None else str(int(classification_count)))
        self.sort_pickup_var.set("--" if pickup_position is None else f"{float(pickup_position):.2f} mm")
        self.sort_destination_var.set(str(destination_label or "--"))

        class_name = str(classification.get("class") or "--")
        self.sort_last_sex_var.set(class_name.title() if class_name.lower() in {"male", "female"} else class_name)
        if "confidence" in classification and classification.get("confidence") is not None:
            self.sort_confidence_var.set(f"{float(classification.get('confidence', 0.0)):.4f}")
        else:
            self.sort_confidence_var.set("--")

        if stage.lower() in {"detecting", "idle", "complete"}:
            self._show_workspace_tab("channel")
        elif stage.lower() in {"classifying", "classified", "routed"}:
            self._show_workspace_tab("sexing")
            self._update_sexing_preview_from_result(classification)

        errors = classification.get("errors") or []
        if errors:
            self.sort_notes_var.set(", ".join(str(error) for error in errors))
        elif destination_label:
            self.sort_notes_var.set(f"Routing to {destination_label}.")
        else:
            self.sort_notes_var.set("Awaiting the next automated step.")

        tube_counts = snapshot.get("tube_counts") or {}
        for key, var in self.sort_tube_count_vars.items():
            tube_info = tube_counts.get(key) or {}
            count = int(tube_info.get("count", 0) or 0)
            capacity = int(tube_info.get("capacity", 10) or 10)
            var.set(f"{count} / {capacity}")

    def update_channel_preview(self):
        if self.is_remote_mode():
            if not self.remote_connected:
                self.set_preview_placeholder("Remote mode preview unavailable while disconnected.")
            elif not self._channel_setup_completed_this_session:
                self.detection_var.set("Waiting for calibration before channel detection output.")
                self.set_preview_placeholder("Waiting for calibration before channel preview...")
            elif self._awaiting_current_detection:
                self.set_preview_placeholder("Waiting for current remote channel detection image...")
            elif not self._startup_preview_completed:
                self.set_preview_placeholder("Capturing current remote channel detection image...")
            elif self.preview_image is None and not self._remote_preview_fetch_in_flight:
                self.set_preview_placeholder("Waiting for remote channel detection image...")
            self.root.after(1000, self.update_channel_preview)
            return

        output_dir = self._current_channel_output_dir()
        output_text = str(output_dir)
        if output_text != self._last_output_dir_text:
            self._last_output_dir_text = output_text
            self.output_dir_var.set(output_text)

        if not self._channel_setup_completed_this_session:
            self.detection_var.set("Waiting for calibration before channel detection output.")
            self.set_preview_placeholder("Waiting for calibration before channel preview...")
            self.root.after(1000, self.update_channel_preview)
            return

        preview_path = self._resolve_channel_file("last_channel_annotated.png")
        result_path = self._resolve_channel_file("last_channel_result.json")

        allow_startup_local_artifacts = self._startup_preview_completed or self._awaiting_current_detection
        result = self._read_detection_result(result_path) if result_path.exists() and allow_startup_local_artifacts else None
        current_result_mtime = result_path.stat().st_mtime if result_path.exists() else None
        if self._awaiting_current_detection and current_result_mtime is not None:
            baseline = self._current_detection_baseline_mtime
            if baseline is None or current_result_mtime > baseline:
                self._awaiting_current_detection = False
                self._current_detection_baseline_mtime = current_result_mtime
                self._startup_preview_requested = False
                self._startup_preview_completed = True
                result = self._read_detection_result(result_path)
            else:
                result = None

        if result:
            positions = result.get("x_positions_mm", [])
            count = len(positions) if isinstance(positions, list) else 0
            remaining = bool(result.get("fly_remaining", False))
            modified = time.strftime("%H:%M:%S", time.localtime(result_path.stat().st_mtime))
            self.detection_var.set(f"remaining={remaining} count={count} last_update={modified}")
            self.last_result_mtime = result_path.stat().st_mtime
            self.last_used_detection_mtime = self.last_result_mtime
        else:
            if self._awaiting_current_detection:
                self.detection_var.set("Waiting for current channel detection output.")
            elif not self._startup_preview_completed:
                self.detection_var.set("Capturing current channel detection output...")
            else:
                self.detection_var.set("Waiting for channel detection output.")

        if preview_path.exists() and not self._awaiting_current_detection and self._startup_preview_completed:
            preview_mtime = preview_path.stat().st_mtime
            if preview_mtime != self.last_preview_mtime:
                self.load_channel_preview(preview_path)
                self.last_preview_mtime = preview_mtime
        else:
            if self._awaiting_current_detection:
                self.set_preview_placeholder("Waiting for current channel detection image...")
            elif not self._startup_preview_completed:
                self.set_preview_placeholder("Capturing current channel detection image...")
            else:
                self.set_preview_placeholder("Waiting for channel detection image...")

        self.root.after(1000, self.update_channel_preview)

    def load_channel_preview(self, path: Path):
        try:
            from PIL import Image

            image = Image.open(path)
            image = image.convert("RGB")
            self.preview_source_image = image
            self._render_preview_image()
        except Exception:
            self.preview_image = None
            self.preview_source_image = None
            self.set_preview_placeholder(f"Preview unavailable:\n{path.name}")

    def load_channel_preview_bytes(self, image_bytes: bytes):
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes))
            image = image.convert("RGB")
            self.preview_source_image = image
            self._render_preview_image()
        except Exception as exc:
            self.preview_image = None
            self.preview_source_image = None
            self.set_preview_placeholder(f"Remote preview unavailable:\n{exc}")

    def load_sexing_preview(self, path: Path):
        try:
            from PIL import Image

            image = Image.open(path)
            image = image.convert("RGB")
            self.sexing_preview_source_image = image
            self._render_sexing_preview_image()
        except Exception:
            self.sexing_preview_image = None
            self.sexing_preview_source_image = None
            self.set_sexing_preview_placeholder("Classification preview unavailable.")

    def load_sexing_preview_bytes(self, image_bytes: bytes):
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes))
            image = image.convert("RGB")
            self.sexing_preview_source_image = image
            self._render_sexing_preview_image()
        except Exception as exc:
            self.sexing_preview_image = None
            self.sexing_preview_source_image = None
            self.set_sexing_preview_placeholder(f"Classification preview unavailable:\n{exc}")

    def set_preview_placeholder(self, message: str):
        self.preview_source_image = None
        self.preview_label.config(image="", text=message, bg="black", fg="white")

    def set_sexing_preview_placeholder(self, message: str):
        self.sexing_preview_source_image = None
        if hasattr(self, "sexing_preview_label"):
            self.sexing_preview_label.config(image="", text=message, bg="black", fg="white")

    def _refresh_preview_image(self, _event=None):
        if self.preview_source_image is not None:
            self._render_preview_image()

    def _refresh_sexing_preview_image(self, _event=None):
        if self.sexing_preview_source_image is not None:
            self._render_sexing_preview_image()

    def _render_preview_image(self):
        if self.preview_source_image is None:
            return

        try:
            from PIL import Image, ImageTk

            target_width = max(self.preview_label.winfo_width(), 420)
            target_height = max(self.preview_label.winfo_height(), 300)
            if target_width <= 1 or target_height <= 1:
                return

            image = self.preview_source_image.copy()
            resample = getattr(Image, "Resampling", Image)
            image.thumbnail((target_width, target_height), resample.LANCZOS)
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview_label.config(image=self.preview_image, text="")
        except Exception:
            self.preview_image = None

    def _render_sexing_preview_image(self):
        if self.sexing_preview_source_image is None or not hasattr(self, "sexing_preview_label"):
            return

        try:
            from PIL import Image, ImageTk

            target_width = max(self.sexing_preview_label.winfo_width(), 420)
            target_height = max(self.sexing_preview_label.winfo_height(), 280)
            if target_width <= 1 or target_height <= 1:
                return

            image = self.sexing_preview_source_image.copy()
            resample = getattr(Image, "Resampling", Image)
            image.thumbnail((target_width, target_height), resample.LANCZOS)
            self.sexing_preview_image = ImageTk.PhotoImage(image)
            self.sexing_preview_label.config(image=self.sexing_preview_image, text="")
        except Exception:
            self.sexing_preview_image = None

    def _update_sexing_preview_from_result(self, result: dict[str, Any]) -> None:
        image_path = str(result.get("image_path") or "")
        raw = result.get("raw")
        if not image_path and isinstance(raw, dict):
            image_path = str(raw.get("image_path") or "")
        if not image_path:
            self._last_sexing_preview_key = None
            self.set_sexing_preview_placeholder("Waiting for classification image...")
            return

        if image_path == self._last_sexing_preview_key:
            return
        self._last_sexing_preview_key = image_path

        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                self.set_sexing_preview_placeholder("Remote classification preview unavailable while disconnected.")
                return
            try:
                image_bytes = self.remote_controller.get_classification_preview_image()
            except (ControllerConnectionError, ControllerError) as exc:
                self.set_sexing_preview_placeholder(f"Remote classification preview unavailable:\n{exc}")
                return
            if image_bytes:
                self.load_sexing_preview_bytes(image_bytes)
            else:
                self.set_sexing_preview_placeholder("Waiting for classification image...")
            return

        preview_path = Path(image_path)
        if preview_path.exists():
            self.load_sexing_preview(preview_path)
        else:
            self.set_sexing_preview_placeholder("Waiting for classification image...")

    def worker_log(self, message: str):
        self.ui_queue.put(("log", message))

    def worker_status(self, state: str, message: str):
        self.ui_queue.put(("status", state, message))

    def _check_stop(self):
        if self.stop_requested.is_set():
            raise TaskCancelled

    def _sleep_with_stop(self, seconds: float):
        end = time.monotonic() + seconds
        while True:
            self._check_stop()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _set_vacuum(self, enabled: bool):
        runtime = self._load_local_runtime()
        vacuum = runtime["vacuum"]
        if enabled:
            vacuum.vacuum_on()
        else:
            vacuum.vacuum_off()
        self.ui_queue.put(("actuator", "vacuum", enabled))

    def _set_vibration(self, enabled: bool):
        runtime = self._load_local_runtime()
        vibration = runtime["vibration"]
        if enabled:
            vibration.vibration_on()
        else:
            vibration.vibration_off()
        self.ui_queue.put(("actuator", "vibration", enabled))

    def _clamp_operational(self, position_mm: float) -> float:
        runtime = self._load_local_runtime()
        motion = runtime["motion"]
        return max(0.0, min(position_mm, motion.get_operational_max_mm()))

    def _apply_pickup_correction(self, position_mm: float) -> float:
        corrected_position = position_mm + config.PICKUP_POSITION_CORRECTION_MM
        return self._clamp_operational(corrected_position)

    def _load_positions_from_result(self, result: dict | None, source_label: str):
        if result is None:
            self.worker_log(f"Invalid detection result: {source_label}")
            return None

        if not result.get("fly_remaining", False):
            self.worker_log("Detection reported no flies remaining.")
            return "done"

        raw_positions = result.get("x_positions_mm")
        if raw_positions is None:
            self.worker_log("Detection JSON is missing x_positions_mm.")
            return None

        if not isinstance(raw_positions, list):
            self.worker_log("Detection JSON x_positions_mm is not a list.")
            return None

        try:
            positions = [float(value) for value in raw_positions]
        except (TypeError, ValueError):
            self.worker_log("Detection JSON contains non-numeric x_positions_mm values.")
            return None

        if not positions:
            self.worker_log("Detection JSON contains no x_positions_mm entries.")
            return "done"

        return sorted((self._apply_pickup_correction(value) for value in positions), reverse=True)

    def _load_positions_from_json(self, path: Path):
        return self._load_positions_from_result(self._read_detection_result(path), str(path))

    def _run_local_channel_detection_capture(self):
        fin6_bridge = self._load_fin6_bridge()
        capture = fin6_bridge.detect_channel_once_from_saved_settings()
        self.ui_queue.put(("local_channel_detection", capture))
        return capture

    def _run_remote_channel_detection_capture(self):
        controller = self._get_remote_controller_for_automation()
        baseline_status = controller.get_status_fresh()
        baseline_summary = baseline_status.get("detection_summary", {}) or {}
        baseline_mtime_raw = baseline_summary.get("source_mtime")
        baseline_mtime = float(baseline_mtime_raw) if baseline_mtime_raw is not None else self.last_used_detection_mtime
        response = controller.detect_channel()
        self.worker_log(f"Remote channel detection: {response.get('message', 'accepted')}")
        status = self._wait_for_remote_detection_result(baseline_mtime, timeout_s=60.0)
        detection_summary = status.get("detection_summary", {}) or {}
        positions_raw = detection_summary.get("corrected_positions_mm") or detection_summary.get("x_positions_mm") or []
        positions = [float(position) for position in positions_raw]
        source_path_text = str(detection_summary.get("source_path", "") or "")
        source_mtime = detection_summary.get("source_mtime")
        if source_mtime is not None:
            self.last_used_detection_mtime = float(source_mtime)
        return {
            "result": {
                "fly_remaining": bool(detection_summary.get("fly_remaining", False)),
                "count": len(positions),
                "x_positions_mm": positions,
                "detections": [],
            },
            "result_path": Path(source_path_text) if source_path_text else None,
        }

    def _wait_for_remote_detection_result(self, previous_source_mtime: float | None, timeout_s: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout_s
        last_status: dict | None = None
        while time.monotonic() < deadline:
            self._check_stop()
            status = self._poll_remote_status_fresh()
            last_status = status
            if self._remote_status_has_terminal_error(status):
                raise RuntimeError(str(status.get("latest_message", "Remote channel detection failed.")))
            detection_summary = status.get("detection_summary", {}) or {}
            source_mtime_raw = detection_summary.get("source_mtime")
            source_mtime = float(source_mtime_raw) if source_mtime_raw is not None else None
            if source_mtime is not None and (previous_source_mtime is None or source_mtime > previous_source_mtime):
                return status
            time.sleep(0.2)
        if last_status is not None and self._remote_status_has_terminal_error(last_status):
            raise RuntimeError(str(last_status.get("latest_message", "Remote channel detection failed.")))
        raise RuntimeError("Timed out waiting for a fresh remote channel detection result.")

    def _resolve_tube_for_classification(self, classification_result: dict) -> tuple[str, float]:
        class_name = str(classification_result.get("class") or "UNCERTAIN").strip().lower()
        if class_name == "male":
            return "Tube 1", config.TUBE_1_CENTER
        if class_name == "female":
            return "Tube 2", config.TUBE_2_CENTER
        raise RuntimeError(
            "Classification remained uncertain. Automated run stopped with the fly left in the chamber for operator review."
        )

    def _classify_for_automated_routing(self, classify_callable, cycle_index: int) -> dict:
        max_attempts = 2
        settle_retry_s = 2.0
        last_result: dict | None = None
        for attempt in range(1, max_attempts + 1):
            self.worker_status("running", f"Cycle {cycle_index}: classifying fly (attempt {attempt}/{max_attempts}).")
            result = classify_callable()
            last_result = result
            self.worker_log(f"Cycle {cycle_index} classification result: {result}")
            class_name = str(result.get("class") or "UNCERTAIN").strip().lower()
            if class_name in {"male", "female"}:
                return result
            if attempt < max_attempts:
                self.worker_log("Classification was uncertain. Waiting briefly before retrying.")
                self._sleep_with_stop(settle_retry_s)
        raise RuntimeError(
            f"Classification remained uncertain after {max_attempts} attempts: {last_result}"
        )

    def _wait_for_detection_result(self, previous_mtime: float | None):
        first_wait = True
        while True:
            self._check_stop()
            result_path = self._resolve_channel_file("last_channel_result.json")

            if result_path.exists():
                current_mtime = result_path.stat().st_mtime
                if previous_mtime is None or current_mtime > previous_mtime:
                    positions = self._load_positions_from_json(result_path)
                    if positions is not None:
                        if previous_mtime is None:
                            self.worker_log(f"Using detection result from {result_path}")
                        else:
                            self.worker_log(f"Using updated detection result from {result_path}")
                        return current_mtime, positions

            if first_wait:
                if previous_mtime is None:
                    self.worker_log("Waiting for channel detection result JSON.")
                else:
                    self.worker_log("Waiting for a newer channel detection result JSON.")
                first_wait = False

            self.worker_status("detecting", "Waiting for channel detection result...")
            self._sleep_with_stop(1.0)

    def _run_assay_worker(self):
        fin6_bridge = self._load_fin6_bridge()
        self.worker_status("assaying", "Running fin6 assay from saved settings.")
        result = fin6_bridge.run_assay_from_saved_settings()
        output_dir = result.get("output_dir")
        if output_dir:
            self.worker_log(f"Assay completed. Output: {output_dir}")

    def _run_channel_detection_worker(self):
        self.worker_status("detecting", "Capturing channel image with fin6 settings.")
        capture = self._run_local_channel_detection_capture()
        result = capture.get("result") or {}
        count = int(result.get("count", 0))
        remaining = bool(result.get("fly_remaining", False))
        self.worker_log(f"Channel detection complete. remaining={remaining} count={count}")

    def _classify_worker(self):
        runtime = self._load_local_runtime()
        classify_callable = runtime["classify_fly"]
        self.worker_status("running", "Capturing and classifying fly.")
        result = classify_callable()
        self.ui_queue.put(("classification_result", result))

    def _reset_worker(self):
        self.worker_status("running", "Resetting actuators to safe state.")
        self._set_vacuum(False)
        self._set_vibration(False)
        self.ui_queue.put(("clear_stop",))

    def _launch_assay_gui_from_worker(self):
        fin6_bridge = self._load_fin6_bridge()
        process = fin6_bridge.launch_fin6_gui()
        pid_text = getattr(process, "pid", None)
        if pid_text is None:
            self.worker_log("Opened the current fin6 assay GUI.")
        else:
            self.worker_log(f"Opened the current fin6 assay GUI (pid {pid_text}).")
        return process

    def _run_automated_worker(self):
        final_operation = importlib.import_module("pi_app.legacy_pi.FinalOperation")
        home_callable = None
        move_callable = None
        vacuum_callable = None
        get_operational_max_mm_callable = None

        if self.is_remote_mode():
            self._get_remote_controller_for_automation()
            classify_callable = self._run_remote_classify_for_automation
            home_callable = self._run_remote_home_for_automation
            move_callable = self._run_remote_move_absolute_for_automation
            vacuum_callable = self._run_remote_set_vacuum_for_automation
            get_operational_max_mm_callable = self._remote_operational_max_mm
        else:
            runtime = self._load_local_runtime()
            classify_callable = runtime["classify_fly"]

        def detect_channel():
            if self.is_remote_mode():
                capture = self._run_remote_channel_detection_capture()
                result_path = capture.get("result_path")
            else:
                capture = self._run_local_channel_detection_capture()
                result_path = Path(capture["result_path"])
                if result_path.exists():
                    self.last_used_detection_mtime = result_path.stat().st_mtime
            return capture

        def publish_snapshot(snapshot: dict[str, Any]) -> None:
            self.ui_queue.put(("automation_snapshot", snapshot))

        try:
            final_operation.run_operation(
                home=home_callable,
                move_absolute=move_callable,
                set_vacuum=vacuum_callable,
                get_operational_max_mm=get_operational_max_mm_callable,
                detect_channel=detect_channel,
                classify_fly=classify_callable,
                ask_yes_no=self._ask_user_yes_no_from_worker,
                launch_assay_gui=self._launch_assay_gui_from_worker,
                status_callback=self.worker_status,
                log_callback=self.worker_log,
                snapshot_callback=publish_snapshot,
                stop_requested=self.stop_requested.is_set,
            )
        except getattr(final_operation, "OperationCancelled"):
            raise TaskCancelled

    def home_gantry(self):
        if self.is_remote_mode():
            self._start_remote_command("home", "moving", "Sending remote home command.", self.remote_controller.home)
            return
        runtime = self._ensure_local_runtime_or_warn("Home")
        if runtime is None:
            return
        self.start_task("home", "moving", "Homing gantry.", runtime["motion"].home_to_zero)

    def move_to_position(self, position: float, label: str):
        if self.is_remote_mode():
            self._start_remote_command(
                f"move to {label}",
                "moving",
                f"Sending remote move to {label}.",
                lambda: self.remote_controller.move_absolute(position),
            )
            return
        runtime = self._ensure_local_runtime_or_warn(f"Move to {label}")
        if runtime is None:
            return
        self.start_task(
            f"move to {label}",
            "moving",
            f"Moving to {label}.",
            lambda: runtime["motion"].move_to_absolute(position),
        )

    def manual_move(self):
        assert self.manual_move_entry is not None
        try:
            target_position = float(self.manual_move_entry.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid distance value.")
            self.manual_move_entry.delete(0, tk.END)
            return

        if self.is_remote_mode():
            self._start_remote_command(
                "manual absolute move",
                "moving",
                f"Sending remote absolute move to {target_position:.2f} mm.",
                lambda: self.remote_controller.move_absolute(target_position),
            )
            self.manual_move_entry.delete(0, tk.END)
            return
        runtime = self._ensure_local_runtime_or_warn("Manual Move")
        if runtime is None:
            return

        def task():
            runtime["motion"].move_to_absolute(target_position)

        if self.start_task(
            "manual absolute move",
            "moving",
            f"Moving to {target_position:.2f} mm.",
            task,
        ):
            self.manual_move_entry.delete(0, tk.END)

    def set_vacuum_from_ui(self, enabled: bool):
        if self.is_remote_mode():
            self._start_remote_command(
                "vacuum",
                "running",
                f"Sending remote vacuum {'on' if enabled else 'off'} command.",
                lambda: self.remote_controller.set_vacuum(enabled),
            )
            return

        def task():
            self.worker_status("running", f"Turning vacuum {'on' if enabled else 'off'}.")
            self._set_vacuum(enabled)

        self.start_task("vacuum", "running", f"Turning vacuum {'on' if enabled else 'off'}.", task)

    def set_vibration_from_ui(self, enabled: bool):
        if self.is_remote_mode():
            self._start_remote_command(
                "vibration",
                "running",
                f"Sending remote vibration {'on' if enabled else 'off'} command.",
                lambda: self.remote_controller.set_vibration(enabled),
            )
            return

        def task():
            self.worker_status("running", f"Turning vibration {'on' if enabled else 'off'}.")
            self._set_vibration(enabled)

        self.start_task("vibration", "running", f"Turning vibration {'on' if enabled else 'off'}.", task)

    def run_channel_detection(self):
        if not self._ensure_remote_connection_for_action("Detect Channel"):
            return
        if not self._ensure_channel_setup_ready_or_begin_setup("Detect Channel", self.run_channel_detection):
            return
        self._begin_new_detection_session(placeholder="Waiting for current channel detection image...")
        if self.is_remote_mode():
            self._start_remote_command(
                "channel detection",
                "detecting",
                "Running Pi-side channel detection with saved fin6 settings.",
                self.remote_controller.detect_channel,
                allow_calibration_bypass=True,
            )
            return
        self.start_task(
            "channel detection",
            "detecting",
            "Capturing channel image with fin6 settings.",
            self._run_channel_detection_worker,
        )

    def run_automated(self):
        if self.is_remote_mode():
            if not self.remote_connected or self.remote_controller is None:
                messagebox.showwarning("Disconnected", "Connect to the Pi backend before starting the automated process.")
                return
            if not self.remote_motion_available:
                messagebox.showerror("Remote Motion Unavailable", "The remote motion subsystem is not available.")
                return
            if not self.remote_classifier_available:
                messagebox.showerror("Remote Classifier Unavailable", "The remote classifier subsystem is not available.")
                return
        else:
            runtime = self._ensure_local_runtime_or_warn("Run Automated")
            if runtime is None:
                return
        if not self._ensure_channel_setup_ready_or_begin_setup("Automated Run", self.run_automated):
            return
        should_start_motion = self._confirm_automated_run_loaded()
        if not should_start_motion:
            self.set_status("idle", "Automated Run cancelled. Load or redistribute flies, then press START when ready.")
            return
        self._begin_new_detection_session(placeholder="Waiting for current channel detection image...")
        self._reset_sorting_status_display()
        self.sort_stage_var.set("Channel setup ready")
        self.sort_notes_var.set("Automated loading is starting. No further prompts will appear during the sorting loop unless Emergency Stop is used.")
        self.start_task(
            "automated run",
            "running",
            "Automated run started.",
            self._run_automated_worker,
            cancellable=True,
        )

    def run_assay(self):
        if self.is_remote_mode():
            self._start_remote_command(
                "assay",
                "assaying",
                "Sending remote assay request.",
                self.remote_controller.run_assay,
            )
            return
        if not self._ensure_assay_setup_ready_or_prompt("Run Assay"):
            return
        self.start_task("assay", "assaying", "Running assay.", self._run_assay_worker)

    def classify_fly_gui(self):
        if self.is_remote_mode():
            self._start_remote_command(
                "classification",
                "running",
                "Sending remote classification request.",
                self.remote_controller.classify_fly,
            )
            return
        runtime = self._ensure_local_runtime_or_warn("Classify Fly")
        if runtime is None:
            return
        self.start_task("classification", "running", "Classifying fly.", self._classify_worker)

    def system_start(self):
        self.run_automated()

    def system_stop(self):
        if self.is_remote_mode():
            if not self.remote_connected:
                messagebox.showinfo("Stop", "Remote backend is not connected.")
                return
            local_automation_active = bool(self.worker_thread and self.worker_thread.is_alive() and self.current_task_cancellable)
            if not self.remote_stop_allowed and not local_automation_active:
                messagebox.showinfo("Stop", "Stop is only available while the remote backend is busy.")
                return
            if local_automation_active:
                self.stop_requested.set()
                self.set_status("stopped", "Stopping remote automated run at the next safe point.")
                self.log_message("Stop requested for remote automated run.")
            else:
                self.set_status("stopped", "Sending remote stop request.")
                self.log_message("Sending remote stop request.")
            if self.remote_stop_allowed:
                self._start_remote_command("stop", "stopped", "Sending remote stop request.", self.remote_controller.stop)
            return

        if not (self.worker_thread and self.worker_thread.is_alive() and self.current_task_cancellable):
            messagebox.showinfo("Stop", "Stop is only available during an automated run.")
            return

        self.stop_requested.set()
        self.set_status("stopped", "Stopping automated run at the next safe point.")
        self.log_message("Stop requested for automated run.")

    def system_reset(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "Wait for the current task to finish before resetting.")
            return
        if self.is_remote_mode():
            self._clear_channel_preview_state(
                clear_artifacts=False,
                placeholder="Waiting for calibration or a current remote channel detection image...",
            )
            self._apply_connection_state(self.connection_state, self.connection_var.get())
            self.set_status("idle", "Remote UI reset.")
            self.remote_seen_log_keys.clear()
            return
        runtime = self._ensure_local_runtime_or_warn("Reset")
        if runtime is None:
            return
        self._clear_channel_preview_state(
            clear_artifacts=True,
            placeholder="Waiting for calibration or a current channel detection image...",
        )
        self.start_task("reset", "running", "Resetting system.", self._reset_worker)

    def on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Quit", "A task is still running. Close the GUI anyway?"):
                return
        self._cancel_pending_channel_setup_resume()
        self.stop_entry_animation()
        self._stop_remote_sync()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()
