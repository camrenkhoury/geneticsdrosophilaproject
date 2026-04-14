#!/usr/bin/env python3
"""
Operator-friendly Tkinter GUI for the fin6 subsystem.

This refactor keeps the existing Brio channel workflow available while pivoting
assay mode to a record-first / process-later workflow with reusable profiles,
background history, transform handling, calibration editing, optional motor
control, and optional Box upload.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from assay_processing import ProcessingError, batch_process_folder, manual_upload_run, process_assay_run, process_last_assay
from assay_profile import AssayProfile, ProfileStore
from assay_recording import RecordingError, record_assay_run
from assay_tracking import (
    AssayCalibration,
    build_assay_calibration,
    calibrate_assay_interactive,
    load_assay_calibration,
    preview_assay_frame,
    render_assay_calibration_overlay,
    save_assay_calibration,
)
from background_manager import (
    BackgroundError,
    capture_profile_background,
    current_background_preview_path,
    get_background_store,
    import_profile_background,
    restore_previous_background,
)
from box_upload import DEFAULT_BOX_CONFIG_PATH, DEFAULT_BOX_TOKENS_PATH, should_auto_upload, write_box_templates
from camera_sources import describe_camera_selection, list_video_devices
from gui_widgets import CalibrationCanvas, EditorRegion, ImageCanvas, read_bgr
from motor_control import MotorError, pulse_vibration_motor
from shared_utils import ensure_dir, load_json, save_json
from transform_utils import TransformSettings, apply_image_transform, describe_transform, merge_crop_from_regions


APP_BG = "#eef3f9"
CARD_BG = "#ffffff"
MUTED_FG = "#5f6b7a"
TEXT_FG = "#1f2937"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("fin6 Fruit Fly Workflow")
        self.geometry("1740x1100")
        self.minsize(1420, 900)
        self.configure(background=APP_BG)

        self.project_root = Path(__file__).resolve().parent
        self.profile_store = ProfileStore(self.project_root / "profiles")
        self.settings_path = self.project_root / ".fly_tracking_gui_settings.json"
        self.ui_queue: queue.Queue = queue.Queue()

        self.current_profile: Optional[AssayProfile] = None
        self.current_profile_path: Optional[Path] = None
        self.latest_assay_raw_frame = None
        self.assay_preview_images: Dict[str, Any] = {}
        self.assay_log_lines: List[str] = []
        self.channel_log_lines: List[str] = []
        self.processing_target: Optional[Path] = None
        self.assay_preview_paths: Dict[str, str] = {}
        self.assay_playback_cap = None
        self.assay_playback_after_id = None
        self.assay_playback_fps = 0.0
        self.assay_playback_frame_index = 0
        self.assay_playback_kind = ""
        self.assay_playback_video_path: Optional[Path] = None
        self.assay_playback_transform_raw = False

        self.channel_live_stop_event: Optional[threading.Event] = None
        self.channel_live_thread: Optional[threading.Thread] = None
        self.assay_worker_thread: Optional[threading.Thread] = None

        self._build_vars()
        self._configure_styles()
        self._build_ui()
        self._load_settings()
        self._load_startup_profile()
        self._refresh_device_labels()
        self._refresh_assay_background_info()
        self._refresh_assay_canvas()
        self._update_region_tree()
        self._update_region_editor()
        self._update_status_labels()
        self.after(80, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # basic UI scaffolding
    # ------------------------------------------------------------------
    def _build_vars(self) -> None:
        root = self.project_root
        self.app_status_var = tk.StringVar(value="Ready")
        self.footer_var = tk.StringVar(value="Load or create an assay profile to begin.")

        self.profile_name_var = tk.StringVar(value="default")
        self.profile_desc_var = tk.StringVar(value="")
        self.profile_combo_var = tk.StringVar(value="")
        self.profile_path_var = tk.StringVar(value="")
        self.profile_last_run_var = tk.StringVar(value="")
        self.channel_camera_status_var = tk.StringVar(value="Channel camera: unknown")
        self.assay_camera_status_var = tk.StringVar(value="Assay camera: unknown")
        self.transform_status_var = tk.StringVar(value="Transform: identity")

        # assay profile fields
        self.assay_backend_var = tk.StringVar(value="opencv")
        self.assay_device_var = tk.StringVar(value="auto:assay")
        self.assay_preferred_hint_var = tk.StringVar(value="")
        self.assay_width_var = tk.StringVar(value="1536")
        self.assay_height_var = tk.StringVar(value="864")
        self.assay_record_fps_var = tk.StringVar(value="30")
        self.analysis_fps_var = tk.StringVar(value="5")
        self.assay_duration_var = tk.StringVar(value="10")
        self.assay_preroll_var = tk.StringVar(value="0")
        self.assay_alignment_var = tk.BooleanVar(value=False)
        self.assay_auto_process_var = tk.BooleanVar(value=False)
        self.assay_save_mask_video_var = tk.BooleanVar(value=False)
        self.assay_show_positions_var = tk.BooleanVar(value=False)
        self.assay_tracking_mode_var = tk.StringVar(value="stitch")
        self.assay_mask_blobs_var = tk.BooleanVar(value=True)
        self.assay_save_snapshots_var = tk.BooleanVar(value=True)
        self.assay_frame_average_var = tk.StringVar(value="1")
        self.assay_smoothing_window_var = tk.StringVar(value="3")
        self.assay_snapshot_interval_var = tk.StringVar(value="1")
        self.assay_output_root_var = tk.StringVar(value=str(root / "outputs" / "assay"))
        self.assay_calibration_path_var = tk.StringVar(value=str(root / "calibrations" / "assay_calibration.json"))
        self.assay_expected_vials_var = tk.StringVar(value="4")
        self.assay_tube_height_mm_var = tk.StringVar(value="")
        self.assay_tube_width_mm_var = tk.StringVar(value="")

        # transform
        self.transform_rotation_var = tk.StringVar(value="0")
        self.transform_flip_h_var = tk.BooleanVar(value=False)
        self.transform_flip_v_var = tk.BooleanVar(value=False)
        self.transform_crop_x_var = tk.StringVar(value="")
        self.transform_crop_y_var = tk.StringVar(value="")
        self.transform_crop_w_var = tk.StringVar(value="")
        self.transform_crop_h_var = tk.StringVar(value="")

        # detector
        self.detector_min_area_var = tk.StringVar(value="10")
        self.detector_max_area_var = tk.StringVar(value="250")
        self.detector_min_threshold_var = tk.StringVar(value="12")
        self.detector_inner_margin_var = tk.StringVar(value="8")
        self.detector_max_flies_var = tk.StringVar(value="10")
        self.detector_threshold_hysteresis_var = tk.StringVar(value="1.5")

        # motor
        self.motor_enabled_var = tk.BooleanVar(value=False)
        self.motor_pin_var = tk.StringVar(value="18")
        self.motor_pulse_ms_var = tk.StringVar(value="400")
        self.motor_settle_ms_var = tk.StringVar(value="150")
        self.motor_active_high_var = tk.BooleanVar(value=True)

        # box upload
        self.box_enabled_var = tk.BooleanVar(value=False)
        self.box_parent_folder_var = tk.StringVar(value="")
        self.box_tokens_file_var = tk.StringVar(value=DEFAULT_BOX_TOKENS_PATH)
        self.box_config_file_var = tk.StringVar(value="")
        self.box_upload_after_processing_var = tk.BooleanVar(value=False)
        self.box_upload_after_recording_var = tk.BooleanVar(value=False)
        self.box_upload_backgrounds_var = tk.BooleanVar(value=False)
        self.box_artifact_mode_var = tk.StringVar(value="summaries")
        self.box_folder_prefix_var = tk.StringVar(value="fly_assay")

        # assay preview / calibration state
        self.assay_preview_mode_var = tk.StringVar(value="calibration")
        self.assay_preview_info_var = tk.StringVar(value="No assay preview yet.")
        self.playback_status_var = tk.StringVar(value="Playback ready when a run has been recorded or processed.")
        self.assay_background_info_var = tk.StringVar(value="Current background: none")
        self.assay_previous_background_info_var = tk.StringVar(value="Previous background: none")

        # selected region editor
        self.region_label_var = tk.StringVar(value="")
        self.region_enabled_var = tk.BooleanVar(value=True)
        self.region_x_var = tk.StringVar(value="")
        self.region_y_var = tk.StringVar(value="")
        self.region_w_var = tk.StringVar(value="")
        self.region_h_var = tk.StringVar(value="")
        self.region_top_var = tk.StringVar(value="")
        self.region_threshold_var = tk.StringVar(value="")
        self.region_baseline_var = tk.StringVar(value="")

        # channel mode fields
        self.channel_background_var = tk.StringVar(value=str(root / "backgrounds" / "channel_bg.png"))
        self.channel_calibration_var = tk.StringVar(value=str(root / "calibrations" / "channel_calibration.json"))
        self.channel_mm_var = tk.StringVar(value="111")
        self.channel_width_var = tk.StringVar(value="1920")
        self.channel_height_var = tk.StringVar(value="1080")
        self.channel_fps_var = tk.StringVar(value="30")
        self.channel_score_thresh_var = tk.StringVar(value="20")
        self.channel_band_half_width_var = tk.StringVar(value="35")
        self.channel_no_align_var = tk.BooleanVar(value=False)
        self.channel_preview_info_var = tk.StringVar(value="No channel preview yet.")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=APP_BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("TLabel", background=CARD_BG, foreground=TEXT_FG)
        style.configure("Muted.TLabel", background=CARD_BG, foreground=MUTED_FG)
        style.configure("Section.TLabel", background=CARD_BG, foreground="#0f172a", font=("DejaVu Sans", 10, "bold"))
        style.configure("Title.TLabel", background=CARD_BG, foreground="#0f172a", font=("DejaVu Sans", 12, "bold"))
        style.configure("Primary.TButton", padding=(10, 6))
        style.configure("Small.TButton", padding=(6, 3))
        style.configure("TNotebook", background=APP_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 8), font=("DejaVu Sans", 10, "bold"))
        style.configure("Treeview", rowheight=26)

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        assay_tab = ttk.Frame(self.notebook)
        debug_tab = ttk.Frame(self.notebook)
        self.notebook.add(assay_tab, text="Assay")
        self.notebook.add(debug_tab, text="Debug")

        self._build_biologist_tab(assay_tab)
        self._build_assay_tab(debug_tab)

        footer = ttk.Frame(self, style="Card.TFrame")
        footer.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(footer, textvariable=self.app_status_var, style="Title.TLabel").pack(side="left", padx=10, pady=8)
        ttk.Label(footer, textvariable=self.footer_var, style="Muted.TLabel").pack(side="left", padx=10, pady=8)

    def _build_biologist_tab(self, parent) -> None:
        container = ttk.Frame(parent, style="Card.TFrame")
        container.pack(fill="both", expand=True, padx=10, pady=10)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        summary = ttk.Frame(container, style="Card.TFrame")
        summary.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        summary.columnconfigure(1, weight=1)
        ttk.Label(summary, text="Assay", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            summary,
            text="Simple view for assay recording, processing, playback, and Box export.",
            style="Muted.TLabel",
            wraplength=1200,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        ttk.Label(summary, text="Profile", style="Section.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 2))
        ttk.Label(summary, textvariable=self.profile_name_var).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(0, 2))
        ttk.Label(summary, textvariable=self.profile_last_run_var, style="Muted.TLabel", wraplength=1200, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        ttk.Label(summary, textvariable=self.assay_camera_status_var, style="Muted.TLabel", wraplength=1200, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        ttk.Label(
            summary,
            text="Assay capture is locked to the HD Webcam eMeet C960 on usb-xhci-hcd.1-2.",
            style="Muted.TLabel",
            wraplength=1200,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w")

        preview_card = ttk.Frame(container, style="Card.TFrame")
        preview_card.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        preview_card.columnconfigure(0, weight=1)
        preview_card.rowconfigure(1, weight=1)
        ttk.Label(preview_card, text="Preview", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.assay_simple_canvas = ImageCanvas(preview_card, background="#0f172a")
        self.assay_simple_canvas.grid(row=1, column=0, sticky="nsew")
        self.assay_simple_canvas.set_empty_text("Record an assay or process a run to review it here.")

        info_bar = ttk.Frame(container, style="Card.TFrame")
        info_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        ttk.Label(info_bar, textvariable=self.assay_preview_info_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(info_bar, textvariable=self.playback_status_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))

        playback_bar = ttk.Frame(container, style="Card.TFrame")
        playback_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Button(playback_bar, text="Play processed", command=lambda: self.play_assay_video("annotated")).pack(side="left", padx=(0, 4))
        ttk.Button(playback_bar, text="Play raw", command=lambda: self.play_assay_video("raw")).pack(side="left", padx=4)
        ttk.Button(playback_bar, text="Play mask", command=lambda: self.play_assay_video("mask")).pack(side="left", padx=4)
        ttk.Button(playback_bar, text="Pause", command=self.pause_assay_video_playback).pack(side="left", padx=4)
        ttk.Button(playback_bar, text="Stop", command=self.stop_assay_video_playback).pack(side="left", padx=4)

        actions = ttk.Frame(container, style="Card.TFrame")
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 6))
        for col in range(3):
            actions.columnconfigure(col, weight=1)
        ttk.Button(actions, text="Run Assay Recording", style="Primary.TButton", command=self.run_assay_recording).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(actions, text="Process Assay", style="Primary.TButton", command=self.process_assay_action).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(actions, text="Export to Box", style="Primary.TButton", command=self.export_assay_action).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        ttk.Label(
            container,
            text="Use the Debug tab for calibration, backgrounds, transforms, motor settings, and advanced processing options.",
            style="Muted.TLabel",
            wraplength=1200,
            justify="left",
        ).grid(row=5, column=0, sticky="w", padx=10, pady=(0, 10))


    def _build_channel_tab(self, parent) -> None:
        paned = ttk.Panedwindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, style="Card.TFrame")
        right = ttk.Frame(paned, style="Card.TFrame")
        paned.add(left, weight=1)
        paned.add(right, weight=3)

        self._card_title(left, "Brio channel mode", "Background capture, calibration, and live detect remain available here.")
        self._labeled_file_row(left, "Background", self.channel_background_var, mode="save")
        self._labeled_file_row(left, "Calibration", self.channel_calibration_var, mode="save", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        self._grid_labeled_entry(left, 4, 0, "Channel mm", self.channel_mm_var)
        self._grid_labeled_entry(left, 4, 2, "Width", self.channel_width_var)
        self._grid_labeled_entry(left, 5, 0, "Height", self.channel_height_var)
        self._grid_labeled_entry(left, 5, 2, "FPS", self.channel_fps_var)
        self._grid_labeled_entry(left, 6, 0, "Score thr", self.channel_score_thresh_var)
        self._grid_labeled_entry(left, 6, 2, "Band half width", self.channel_band_half_width_var)
        ttk.Checkbutton(left, text="No alignment", variable=self.channel_no_align_var).grid(row=7, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 2))
        ttk.Label(left, textvariable=self.channel_camera_status_var, style="Muted.TLabel").grid(row=8, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 10))

        actions = ttk.Frame(left, style="Card.TFrame")
        actions.grid(row=9, column=0, columnspan=4, sticky="ew", padx=10, pady=(6, 10))
        ttk.Button(actions, text="Capture background", command=self.capture_channel_background).pack(fill="x", pady=2)
        ttk.Button(actions, text="Calibrate channel", command=self.run_channel_calibration).pack(fill="x", pady=2)
        ttk.Button(actions, text="Detect once", command=self.detect_channel_once).pack(fill="x", pady=2)
        self.channel_live_btn = ttk.Button(actions, text="Start live", command=self.toggle_channel_live)
        self.channel_live_btn.pack(fill="x", pady=2)

        for col in range(4):
            left.columnconfigure(col, weight=1)

        self.channel_canvas = ImageCanvas(right, background="#0f172a")
        self.channel_canvas.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.channel_canvas.set_empty_text("Capture a Brio background or run a detection to populate this preview.")
        ttk.Label(right, textvariable=self.channel_preview_info_var, style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 6))

        channel_log_card = ttk.Frame(right, style="Card.TFrame")
        channel_log_card.pack(fill="both", padx=10, pady=(0, 10))
        ttk.Label(channel_log_card, text="Channel log", style="Title.TLabel").pack(anchor="w", padx=6, pady=(4, 2))
        self.channel_log_widget = tk.Text(channel_log_card, height=10, wrap="word", background="#0f172a", foreground="#dce8f7", insertbackground="#dce8f7", relief="flat")
        self.channel_log_widget.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _bind_vertical_scroll(self, widget, canvas: tk.Canvas) -> None:
        def _scroll(event):
            step = 0
            if getattr(event, "num", None) == 4:
                step = -1
            elif getattr(event, "num", None) == 5:
                step = 1
            elif getattr(event, "delta", 0):
                step = -1 if int(event.delta) > 0 else 1
            if step:
                canvas.yview_scroll(step, "units")
                return "break"
            return None

        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, _scroll, add="+")

    def _bind_vertical_scroll_recursive(self, widget, canvas: tk.Canvas) -> None:
        self._bind_vertical_scroll(widget, canvas)
        for child in widget.winfo_children():
            self._bind_vertical_scroll_recursive(child, canvas)

    def _build_assay_tab(self, parent) -> None:
        paned = ttk.Panedwindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)

        sidebar = ttk.Frame(paned, style="Card.TFrame")
        viewer = ttk.Frame(paned, style="Card.TFrame")
        paned.add(sidebar, weight=2)
        paned.add(viewer, weight=3)

        # make sidebar scrollable and wheel-friendly
        sidebar_canvas = tk.Canvas(sidebar, background=CARD_BG, highlightthickness=0, borderwidth=0)
        sidebar_scrollbar = ttk.Scrollbar(sidebar, orient="vertical", command=sidebar_canvas.yview)
        sidebar_body = ttk.Frame(sidebar_canvas, style="Card.TFrame")
        sidebar_window = sidebar_canvas.create_window((0, 0), window=sidebar_body, anchor="nw")
        sidebar_body.bind("<Configure>", lambda _e: sidebar_canvas.configure(scrollregion=sidebar_canvas.bbox("all")))
        sidebar_canvas.bind("<Configure>", lambda event: sidebar_canvas.itemconfigure(sidebar_window, width=event.width))
        sidebar_canvas.configure(yscrollcommand=sidebar_scrollbar.set)
        sidebar_canvas.pack(side="left", fill="both", expand=True)
        sidebar_scrollbar.pack(side="right", fill="y")
        self._bind_vertical_scroll(sidebar, sidebar_canvas)
        self._bind_vertical_scroll(sidebar_canvas, sidebar_canvas)
        self._bind_vertical_scroll(sidebar_body, sidebar_canvas)

        row = 0
        row = self._build_profile_card(sidebar_body, row)
        row = self._build_background_card(sidebar_body, row)
        row = self._build_transform_card(sidebar_body, row)
        row = self._build_calibration_card(sidebar_body, row)
        row = self._build_recording_card(sidebar_body, row)
        row = self._build_processing_card(sidebar_body, row)
        row = self._build_upload_card(sidebar_body, row)
        sidebar_body.columnconfigure(0, weight=1)
        self._bind_vertical_scroll_recursive(sidebar_body, sidebar_canvas)

        # viewer side
        topbar = ttk.Frame(viewer, style="Card.TFrame")
        topbar.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(topbar, text="Preview", style="Title.TLabel").pack(side="left")
        for value, label in [
            ("calibration", "Calibration"),
            ("background", "Background"),
            ("transform", "Transform"),
            ("annotated", "Annotated"),
            ("mask", "Mask"),
            ("raw", "Raw"),
        ]:
            ttk.Radiobutton(topbar, text=label, value=value, variable=self.assay_preview_mode_var, command=self._refresh_assay_canvas).pack(side="left", padx=6)

        self.assay_canvas = CalibrationCanvas(viewer, background="#0f172a")
        self.assay_canvas.pack(fill="both", expand=True, padx=10, pady=(6, 4))
        self.assay_canvas.set_empty_text("Capture or load a background, then calibrate the assay tubes.")
        self.assay_canvas.set_callbacks(on_change=self._on_canvas_regions_changed, on_select=self._on_canvas_region_selected, on_status=self._set_footer)

        info_bar = ttk.Frame(viewer, style="Card.TFrame")
        info_bar.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(info_bar, textvariable=self.assay_preview_info_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(info_bar, textvariable=self.playback_status_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))

        playback_bar = ttk.Frame(viewer, style="Card.TFrame")
        playback_bar.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(playback_bar, text="Play processed", command=lambda: self.play_assay_video("annotated")).pack(side="left", padx=(0, 4))
        ttk.Button(playback_bar, text="Play raw", command=lambda: self.play_assay_video("raw")).pack(side="left", padx=4)
        ttk.Button(playback_bar, text="Play mask", command=lambda: self.play_assay_video("mask")).pack(side="left", padx=4)
        ttk.Button(playback_bar, text="Pause", command=self.pause_assay_video_playback).pack(side="left", padx=4)
        ttk.Button(playback_bar, text="Stop", command=self.stop_assay_video_playback).pack(side="left", padx=4)

        lower = ttk.Panedwindow(viewer, orient="horizontal")
        lower.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        region_card = ttk.Frame(lower, style="Card.TFrame")
        log_card = ttk.Frame(lower, style="Card.TFrame")
        lower.add(region_card, weight=2)
        lower.add(log_card, weight=3)

        region_canvas = tk.Canvas(region_card, background=CARD_BG, highlightthickness=0, borderwidth=0)
        region_scrollbar = ttk.Scrollbar(region_card, orient="vertical", command=region_canvas.yview)
        region_body = ttk.Frame(region_canvas, style="Card.TFrame")
        region_window = region_canvas.create_window((0, 0), window=region_body, anchor="nw")
        region_body.bind("<Configure>", lambda _e: region_canvas.configure(scrollregion=region_canvas.bbox("all")))
        region_canvas.bind("<Configure>", lambda event: region_canvas.itemconfigure(region_window, width=event.width))
        region_canvas.configure(yscrollcommand=region_scrollbar.set)
        region_canvas.pack(side="left", fill="both", expand=True)
        region_scrollbar.pack(side="right", fill="y")
        self._bind_vertical_scroll(region_card, region_canvas)
        self._bind_vertical_scroll(region_canvas, region_canvas)
        self._bind_vertical_scroll(region_body, region_canvas)

        ttk.Label(region_body, text="Calibrated vials", style="Title.TLabel").pack(anchor="w", padx=6, pady=(4, 2))
        ttk.Label(region_body, text="Select a vial, then drag the pink line or use the quick-adjust buttons below. Delete controls are kept visible here.", style="Muted.TLabel", wraplength=340, justify="left").pack(anchor="w", padx=6, pady=(0, 4))
        self.region_tree = ttk.Treeview(region_body, columns=("idx", "label", "state", "thr"), show="headings", height=6, selectmode="browse")
        for col, text, width in [("idx", "#", 42), ("label", "Label", 140), ("state", "State", 78), ("thr", "Thr y", 68)]:
            self.region_tree.heading(col, text=text)
            self.region_tree.column(col, width=width, anchor="center")
        self.region_tree.pack(fill="x", padx=6, pady=(0, 4))
        self.region_tree.bind("<<TreeviewSelect>>", self._on_region_tree_selected)
        self.region_tree.bind("<Delete>", lambda _e: self.assay_canvas.delete_selected() or "break")
        self.region_tree.bind("<BackSpace>", lambda _e: self.assay_canvas.delete_selected() or "break")

        form = ttk.Frame(region_body, style="Card.TFrame")
        form.pack(fill="x", padx=6, pady=(0, 6))
        for col in range(4):
            form.columnconfigure(col, weight=1)
        self._grid_labeled_entry(form, 0, 0, "Label", self.region_label_var, width=18)
        ttk.Checkbutton(form, text="Enabled", variable=self.region_enabled_var).grid(row=0, column=2, columnspan=2, sticky="w", padx=6, pady=3)
        self._grid_labeled_entry(form, 1, 0, "x", self.region_x_var, width=8)
        self._grid_labeled_entry(form, 1, 2, "y", self.region_y_var, width=8)
        self._grid_labeled_entry(form, 2, 0, "w", self.region_w_var, width=8)
        self._grid_labeled_entry(form, 2, 2, "h", self.region_h_var, width=8)
        self._grid_labeled_entry(form, 3, 0, "Top y", self.region_top_var, width=8)
        self._grid_labeled_entry(form, 3, 2, "Thr y", self.region_threshold_var, width=8)
        self._grid_labeled_entry(form, 4, 0, "Base y", self.region_baseline_var, width=8)

        quick_row = ttk.Frame(region_body, style="Card.TFrame")
        quick_row.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Button(quick_row, text="Thr -5", command=lambda: self.assay_canvas.nudge_selected_reference("threshold", -5)).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(quick_row, text="Thr -1", command=lambda: self.assay_canvas.nudge_selected_reference("threshold", -1)).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(quick_row, text="Thr +1", command=lambda: self.assay_canvas.nudge_selected_reference("threshold", 1)).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(quick_row, text="Thr +5", command=lambda: self.assay_canvas.nudge_selected_reference("threshold", 5)).pack(side="left", fill="x", expand=True, padx=(2, 0))

        apply_row = ttk.Frame(region_body, style="Card.TFrame")
        apply_row.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Button(apply_row, text="Apply selected vial", command=self.apply_selected_region_form).pack(fill="x", pady=2)
        ttk.Button(apply_row, text="Delete selected vial", command=self.assay_canvas.delete_selected).pack(fill="x", pady=2)
        ttk.Button(apply_row, text="Delete last vial", command=self.assay_canvas.delete_last).pack(fill="x", pady=2)

        extra_row = ttk.Frame(region_body, style="Card.TFrame")
        extra_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(extra_row, text="Duplicate selected", command=self.assay_canvas.duplicate_selected).pack(fill="x", pady=2)
        ttk.Button(extra_row, text="Enable / Ignore", command=self.assay_canvas.toggle_selected_enabled).pack(fill="x", pady=2)
        ttk.Button(extra_row, text="Undo", command=self.assay_canvas.undo).pack(fill="x", pady=2)
        ttk.Button(extra_row, text="Redo", command=self.assay_canvas.redo).pack(fill="x", pady=2)

        self._bind_vertical_scroll_recursive(region_body, region_canvas)

        ttk.Label(log_card, text="Assay log", style="Title.TLabel").pack(anchor="w", padx=6, pady=(4, 2))
        self.assay_log_widget = tk.Text(log_card, height=14, wrap="word", background="#0f172a", foreground="#dce8f7", insertbackground="#dce8f7", relief="flat")
        self.assay_log_widget.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # ------------------------------------------------------------------
    # card builders
    # ------------------------------------------------------------------
    def _build_profile_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Profile", "Reusable assay configuration for this physical setup.")
        combo = ttk.Combobox(card, textvariable=self.profile_combo_var, state="readonly")
        combo.grid(row=1, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 4))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.load_selected_profile())
        self.profile_combo = combo
        self._grid_labeled_entry(card, 2, 0, "Profile name", self.profile_name_var)
        self._grid_labeled_entry(card, 2, 2, "Description", self.profile_desc_var)
        self._grid_labeled_entry(card, 3, 0, "Assay device", self.assay_device_var, state="readonly")
        self._grid_labeled_entry(card, 3, 2, "Assay hint", self.assay_preferred_hint_var)
        self._grid_labeled_entry(card, 4, 0, "Profile path", self.profile_path_var, state="readonly", colspan=4)
        ttk.Label(card, textvariable=self.assay_camera_status_var, style="Muted.TLabel", wraplength=520, justify="left").grid(
            row=5, column=0, columnspan=4, sticky="w", padx=10, pady=(2, 0)
        )
        ttk.Label(
            card,
            text="Assay capture is locked to the HD Webcam eMeet C960 on usb-xhci-hcd.1-2.",
            style="Muted.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 4))
        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.grid(row=7, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 10))
        ttk.Button(buttons, text="New", command=self.new_profile).pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="Save", command=self.save_profile).pack(side="left", padx=4)
        ttk.Button(buttons, text="Duplicate", command=self.duplicate_profile).pack(side="left", padx=4)
        ttk.Button(buttons, text="Reload", command=self.load_selected_profile).pack(side="left", padx=4)
        ttk.Button(buttons, text="Refresh cameras", command=self._refresh_device_labels).pack(side="left", padx=4)
        return row + 1

    def _build_background_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Background", "Current, previous, and archived backgrounds are tracked per profile.")
        ttk.Label(card, textvariable=self.assay_background_info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 2))
        ttk.Label(card, textvariable=self.assay_previous_background_info_var, style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 4))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=3, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 10))
        ttk.Button(actions, text="Capture background", command=self.capture_assay_background).pack(fill="x", pady=2)
        ttk.Button(actions, text="Import background", command=self.import_assay_background).pack(fill="x", pady=2)
        ttk.Button(actions, text="Restore previous", command=self.restore_assay_background).pack(fill="x", pady=2)
        ttk.Button(actions, text="Rebuild with transform", command=self.rebuild_background_transform).pack(fill="x", pady=2)
        return row + 1

    def _build_transform_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Transform", "Apply rotation, flips, and crop before calibration and processing.")
        self._grid_labeled_entry(card, 1, 0, "Rotation deg", self.transform_rotation_var)
        ttk.Checkbutton(card, text="Flip horizontal", variable=self.transform_flip_h_var, command=self._on_transform_changed).grid(row=1, column=2, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Flip vertical", variable=self.transform_flip_v_var, command=self._on_transform_changed).grid(row=1, column=3, sticky="w", padx=10, pady=3)
        self._grid_labeled_entry(card, 2, 0, "Crop x", self.transform_crop_x_var, width=8)
        self._grid_labeled_entry(card, 2, 1, "Crop y", self.transform_crop_y_var, width=8)
        self._grid_labeled_entry(card, 2, 2, "Crop w", self.transform_crop_w_var, width=8)
        self._grid_labeled_entry(card, 2, 3, "Crop h", self.transform_crop_h_var, width=8)
        ttk.Label(card, textvariable=self.transform_status_var, style="Muted.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", padx=10, pady=(2, 2))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 10))
        ttk.Button(actions, text="Capture test frame", command=self.capture_transform_test_frame).pack(fill="x", pady=2)
        ttk.Button(actions, text="Test transform", command=self.test_transform).pack(fill="x", pady=2)
        ttk.Button(actions, text="Select crop", command=self.select_transform_crop).pack(fill="x", pady=2)
        ttk.Button(actions, text="Clear crop", command=self.clear_transform_crop).pack(fill="x", pady=2)
        ttk.Button(actions, text="Crop to current vials", command=self.crop_transform_to_regions).pack(fill="x", pady=2)
        return row + 1

    def _build_calibration_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Calibration", "Editable vial bounds, top reference, threshold line, and baseline.")
        self._labeled_file_row(card, "Calibration JSON", self.assay_calibration_path_var, mode="save", filetypes=[("JSON", "*.json"), ("All files", "*.*")], row=1)
        self._grid_labeled_entry(card, 2, 0, "Expected vials", self.assay_expected_vials_var)
        self._grid_labeled_entry(card, 2, 2, "Tube height mm", self.assay_tube_height_mm_var)
        self._grid_labeled_entry(card, 3, 0, "Tube width mm", self.assay_tube_width_mm_var)
        tool_row = ttk.Frame(card, style="Card.TFrame")
        tool_row.grid(row=4, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 4))
        ttk.Button(tool_row, text="Select tool", command=lambda: self.assay_canvas.set_tool("select")).pack(side="left", padx=(0, 4))
        ttk.Button(tool_row, text="Draw vial", command=lambda: self.assay_canvas.set_tool("draw")).pack(side="left", padx=4)
        ttk.Button(tool_row, text="Split x4", command=self.split_four_vials_mode).pack(side="left", padx=4)
        ttk.Button(tool_row, text="Guided quad/rect", command=self.guided_assay_calibration).pack(side="left", padx=4)
        action_row = ttk.Frame(card, style="Card.TFrame")
        action_row.grid(row=5, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 10))
        ttk.Button(action_row, text="Load", command=self.load_assay_calibration_into_editor).pack(side="left", padx=(0, 4))
        ttk.Button(action_row, text="Save", command=self.save_assay_calibration_from_editor).pack(side="left", padx=4)
        ttk.Button(action_row, text="Test", command=self.test_assay_calibration).pack(side="left", padx=4)
        ttk.Button(action_row, text="Clear", command=lambda: self.assay_canvas.clear_regions()).pack(side="left", padx=4)
        ttk.Button(action_row, text="Sort left-right", command=lambda: self.assay_canvas.sort_left_to_right()).pack(side="left", padx=4)
        ttk.Button(action_row, text="Copy refs to all", command=lambda: self.assay_canvas.apply_reference_style_from_selected()).pack(side="left", padx=4)
        return row + 1

    def _build_recording_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Recording", "One-click 10 s assay recording with optional motor pulse.")
        self._grid_labeled_entry(card, 1, 0, "Width", self.assay_width_var)
        self._grid_labeled_entry(card, 1, 1, "Height", self.assay_height_var)
        self._grid_labeled_entry(card, 1, 2, "Record FPS", self.assay_record_fps_var)
        self._grid_labeled_entry(card, 1, 3, "Duration s", self.assay_duration_var)
        self._grid_labeled_entry(card, 2, 0, "Pre-roll s", self.assay_preroll_var)
        ttk.Checkbutton(card, text="Auto-process after record", variable=self.assay_auto_process_var).grid(row=2, column=1, columnspan=2, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Motor enabled", variable=self.motor_enabled_var).grid(row=3, column=0, sticky="w", padx=10, pady=3)
        self._grid_labeled_entry(card, 3, 1, "GPIO pin", self.motor_pin_var)
        self._grid_labeled_entry(card, 3, 2, "Pulse ms", self.motor_pulse_ms_var)
        self._grid_labeled_entry(card, 3, 3, "Settle ms", self.motor_settle_ms_var)
        ttk.Checkbutton(card, text="Motor active high", variable=self.motor_active_high_var).grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=3)
        ttk.Label(
            card,
            text="Using restored legacy vibration backend (BCM12 PWM + BCM24 DIR). GPIO pin is kept for compatibility only.",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 4))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=6, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 10))
        ttk.Button(actions, text="Test motor", command=self.test_motor).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="Run 10 s Assay", command=self.run_assay_recording).pack(side="left", padx=4)
        return row + 1

    def _build_processing_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Processing", "Offline processing now supports snapshot-first stitching, direct mask-blob overlays, and multiple processing passes per recording.")
        self._grid_labeled_entry(card, 1, 0, "Analysis FPS", self.analysis_fps_var)
        self._grid_labeled_entry(card, 1, 1, "Frame average", self.assay_frame_average_var)
        self._grid_labeled_entry(card, 1, 2, "Smoothing", self.assay_smoothing_window_var)
        self._grid_labeled_entry(card, 2, 0, "Min area", self.detector_min_area_var)
        self._grid_labeled_entry(card, 2, 1, "Max area", self.detector_max_area_var)
        self._grid_labeled_entry(card, 2, 2, "Min threshold", self.detector_min_threshold_var)
        self._grid_labeled_entry(card, 3, 0, "Inner margin", self.detector_inner_margin_var)
        self._grid_labeled_entry(card, 3, 1, "Max flies/vial", self.detector_max_flies_var)
        self._grid_labeled_entry(card, 3, 2, "Thr hysteresis", self.detector_threshold_hysteresis_var)
        self._grid_labeled_entry(card, 4, 0, "Tracking mode", self.assay_tracking_mode_var)
        self._grid_labeled_entry(card, 4, 1, "Snapshot interval", self.assay_snapshot_interval_var)
        ttk.Checkbutton(card, text="Alignment enabled", variable=self.assay_alignment_var).grid(row=5, column=0, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Save mask video", variable=self.assay_save_mask_video_var).grid(row=5, column=1, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Show positions", variable=self.assay_show_positions_var).grid(row=5, column=2, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Use mask blobs as flies", variable=self.assay_mask_blobs_var).grid(row=6, column=0, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Save processed snapshots", variable=self.assay_save_snapshots_var).grid(row=6, column=1, sticky="w", padx=10, pady=3)
        ttk.Label(card, textvariable=self.profile_last_run_var, style="Muted.TLabel").grid(row=7, column=0, columnspan=4, sticky="w", padx=10, pady=(2, 4))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=8, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 10))
        ttk.Button(actions, text="Process Last Assay", command=self.process_last_assay_run).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="Process Selected Assay", command=self.process_selected_assay_run).pack(side="left", padx=4)
        ttk.Button(actions, text="Batch Process Folder", command=self.batch_process_assay_runs).pack(side="left", padx=4)
        return row + 1

    def _build_upload_card(self, parent, row: int) -> int:
        card = self._card(parent, row, "Export / Upload", "Optional Box upload. Artifact mode examples: summaries, summaries+videos, raw+annotated+pdf, or full.")
        self._grid_labeled_entry(card, 1, 0, "Output root", self.assay_output_root_var, width=36, colspan=4)
        ttk.Checkbutton(card, text="Box enabled", variable=self.box_enabled_var).grid(row=2, column=0, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Upload after processing", variable=self.box_upload_after_processing_var).grid(row=2, column=1, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(card, text="Upload after recording", variable=self.box_upload_after_recording_var).grid(row=2, column=2, sticky="w", padx=10, pady=3)
        self._grid_labeled_entry(card, 3, 0, "Parent folder", self.box_parent_folder_var, width=18)
        self._grid_labeled_entry(card, 3, 2, "Folder prefix", self.box_folder_prefix_var, width=16)
        self._grid_labeled_entry(card, 4, 0, "Artifact mode", self.box_artifact_mode_var, width=18)
        self._grid_labeled_entry(card, 4, 2, "Tokens file", self.box_tokens_file_var, width=18)
        self._grid_labeled_entry(card, 5, 0, "Config file", self.box_config_file_var, width=18)
        ttk.Checkbutton(card, text="Upload backgrounds", variable=self.box_upload_backgrounds_var).grid(row=5, column=2, sticky="w", padx=10, pady=3)
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=6, column=0, columnspan=4, sticky="ew", padx=10, pady=(4, 10))
        ttk.Button(actions, text="Upload last run", command=self.upload_last_run).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="Write Box templates", command=self.write_box_templates).pack(side="left", padx=4)
        ttk.Button(actions, text="Save profile", command=self.save_profile).pack(side="left", padx=4)
        ttk.Button(actions, text="List cameras", command=self.show_camera_inventory).pack(side="left", padx=4)
        return row + 1

    # ------------------------------------------------------------------
    # small UI helpers
    # ------------------------------------------------------------------
    def _card(self, parent, row: int, title: str, subtitle: str) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame")
        card.grid(row=row, column=0, sticky="ew", padx=10, pady=(10 if row == 0 else 0, 10))
        header = ttk.Frame(card, style="Card.TFrame")
        header.grid(row=0, column=0, columnspan=4, sticky="ew", padx=10, pady=(8, 0))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=title, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=subtitle, style="Muted.TLabel", wraplength=480, justify="left").grid(row=1, column=0, sticky="w", pady=(2, 0))
        for col in range(4):
            card.columnconfigure(col, weight=1)
        return card

    def _card_title(self, parent, title: str, subtitle: str) -> None:
        ttk.Label(parent, text=title, style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 0))
        ttk.Label(parent, text=subtitle, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 6))

    def _grid_labeled_entry(self, parent, row: int, col: int, label: str, variable: tk.Variable, width: int = 14, state: str = "normal", colspan: int = 1):
        field = ttk.Frame(parent, style="Card.TFrame")
        field.grid(row=row, column=col, columnspan=max(1, int(colspan)), sticky="ew", padx=10, pady=3)
        field.columnconfigure(0, weight=1)
        ttk.Label(field, text=label).grid(row=0, column=0, sticky="w", pady=(0, 1))
        entry = ttk.Entry(field, textvariable=variable, width=width, state=state)
        entry.grid(row=1, column=0, sticky="ew")
        return entry

    def _labeled_file_row(self, parent, label: str, variable: tk.Variable, mode: str = "open", filetypes=None, row: int = 2):
        if filetypes is None:
            filetypes = [("All files", "*.*")]
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=3)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=3)
        ttk.Button(parent, text="Browse", style="Small.TButton", command=lambda: self._browse_for_var(variable, mode, filetypes)).grid(row=row, column=3, sticky="e", padx=(0, 10), pady=3)
        return entry

    def _browse_for_var(self, variable: tk.Variable, mode: str, filetypes) -> None:
        current = str(variable.get() or self.project_root)
        initialdir = str(Path(current).expanduser().parent if current else self.project_root)
        if mode == "save":
            path = filedialog.asksaveasfilename(initialdir=initialdir, filetypes=filetypes)
        else:
            path = filedialog.askopenfilename(initialdir=initialdir, filetypes=filetypes)
        if path:
            variable.set(path)

    # ------------------------------------------------------------------
    # settings/profile loading
    # ------------------------------------------------------------------
    def _default_box_paths(self) -> tuple[Path, Path]:
        return Path(DEFAULT_BOX_CONFIG_PATH), Path(DEFAULT_BOX_TOKENS_PATH)

    def write_box_templates(self) -> None:
        box_config_path, _box_tokens_path = self._default_box_paths()
        target_dir = box_config_path.parent
        try:
            result = write_box_templates(target_dir, overwrite=False)
        except FileExistsError as exc:
            prompt = f"{exc}\n\nOverwrite the existing template files in {target_dir}?"
            if not messagebox.askyesno("Overwrite Box templates?", prompt):
                return
            result = write_box_templates(target_dir, overwrite=True)
        self.box_config_file_var.set(result["config_file"])
        self.box_tokens_file_var.set(result["tokens_file"])
        seeded_from = str(result.get("seeded_from", "") or "")
        tokens_write_mode = str(result.get("tokens_write_mode", "") or "")
        footer = "Box template files are ready."
        if seeded_from:
            footer = f"Box config seeded from {seeded_from}."
        self._set_footer(footer)
        details = [
            "Box config files are ready.",
            "",
            f"Config JSON: {result['config_file']}",
            f"Tokens JSON: {result['tokens_file']}",
        ]
        if seeded_from:
            details.extend([
                "",
                f"Seeded from: {seeded_from}",
                f"Tokens setup: {tokens_write_mode}",
                "The config already reuses the existing Box uploader credentials from this repository.",
            ])
        else:
            details.extend([
                "",
                "Paste your Box app values into those files, then enable Box upload in the profile.",
            ])
        messagebox.showinfo(
            "Box template files",
            "\n".join(details),
        )

    def _default_profile(self, name: str = "default") -> AssayProfile:
        profile = self.profile_store.create_profile(name)
        profile.calibration_path = str((self.project_root / "calibrations" / f"{profile.slug}_calibration.json").resolve())
        profile.outputs.output_root = str((self.project_root / "outputs" / "assay").resolve())
        box_config_path, box_tokens_path = self._default_box_paths()
        profile.box_upload.config_file = ""
        profile.box_upload.tokens_file = str(box_tokens_path)
        profile.box_upload.upload_after_processing = True
        profile.motor.backend = "module"
        profile.motor.module_name = "vibration"
        return profile

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            data = load_json(self.settings_path)
        except Exception:
            return
        geometry = data.get("window_geometry")
        if geometry:
            try:
                self.geometry(str(geometry))
            except Exception:
                pass
        for var, key in [
            (self.channel_background_var, "channel_background"),
            (self.channel_calibration_var, "channel_calibration"),
            (self.assay_preview_mode_var, "assay_preview_mode"),
            (self.channel_mm_var, "channel_mm"),
            (self.channel_score_thresh_var, "channel_score_thresh"),
            (self.channel_band_half_width_var, "channel_band_half_width"),
        ]:
            if key in data:
                var.set(data[key])

    def _save_settings(self) -> None:
        payload = {
            "window_geometry": self.geometry(),
            "channel_background": self.channel_background_var.get(),
            "channel_calibration": self.channel_calibration_var.get(),
            "assay_preview_mode": self.assay_preview_mode_var.get(),
            "channel_mm": self.channel_mm_var.get(),
            "channel_score_thresh": self.channel_score_thresh_var.get(),
            "channel_band_half_width": self.channel_band_half_width_var.get(),
        }
        save_json(self.settings_path, payload)

    def _load_startup_profile(self) -> None:
        profile = self.profile_store.load_last_used()
        if profile is None:
            profile = self._default_profile()
            self.profile_store.save_profile(profile)
        self.load_profile(profile)

    def _refresh_profile_combo(self) -> None:
        names = self.profile_store.list_profile_names()
        self.profile_combo["values"] = names
        if self.current_profile is not None:
            self.profile_combo_var.set(self.current_profile.name)

    def load_profile(self, profile: AssayProfile) -> None:
        self.current_profile = profile
        self.current_profile_path = self.profile_store.profile_path(profile.name)
        self.profile_name_var.set(profile.name)
        self.profile_desc_var.set(profile.description)
        self.profile_combo_var.set(profile.name)
        self.profile_path_var.set(str(self.current_profile_path.resolve()))
        self.profile_last_run_var.set(f"Last run: {profile.last_run_dir or 'none'}")

        self.assay_backend_var.set(profile.assay_camera.backend)
        self.assay_device_var.set("auto:assay")
        self.assay_preferred_hint_var.set(profile.assay_camera.preferred_hint)
        self.assay_width_var.set(str(profile.assay_camera.width))
        self.assay_height_var.set(str(profile.assay_camera.height))
        self.assay_record_fps_var.set(str(profile.assay_camera.fps))
        self.analysis_fps_var.set(str(profile.analysis.analysis_fps))
        self.assay_duration_var.set(str(profile.assay_duration_s))
        self.assay_preroll_var.set(str(profile.record_preroll_s))
        self.assay_alignment_var.set(bool(profile.analysis.alignment_enabled))
        self.assay_auto_process_var.set(bool(profile.analysis.auto_process_after_recording))
        self.assay_save_mask_video_var.set(bool(profile.analysis.save_mask_video))
        self.assay_show_positions_var.set(bool(profile.analysis.show_positions))
        self.assay_tracking_mode_var.set(str(getattr(profile.analysis, "tracking_mode", "stitch") or "stitch"))
        self.assay_mask_blobs_var.set(bool(getattr(profile.analysis, "use_mask_blobs_as_flies", True)))
        self.assay_save_snapshots_var.set(bool(profile.outputs.save_preview_snapshots))
        self.assay_frame_average_var.set(str(profile.analysis.frame_average_count))
        self.assay_smoothing_window_var.set(str(profile.analysis.smoothing_window))
        self.assay_snapshot_interval_var.set(str(profile.outputs.snapshot_interval_s))
        self.assay_output_root_var.set(str(profile.outputs.output_root))
        self.assay_calibration_path_var.set(str(profile.calibration_path))

        self.transform_rotation_var.set(str(profile.transform.rotation_deg))
        self.transform_flip_h_var.set(bool(profile.transform.flip_horizontal))
        self.transform_flip_v_var.set(bool(profile.transform.flip_vertical))
        crop = profile.transform.crop_xywh or ["", "", "", ""]
        self.transform_crop_x_var.set(str(crop[0]) if len(crop) == 4 else "")
        self.transform_crop_y_var.set(str(crop[1]) if len(crop) == 4 else "")
        self.transform_crop_w_var.set(str(crop[2]) if len(crop) == 4 else "")
        self.transform_crop_h_var.set(str(crop[3]) if len(crop) == 4 else "")

        self.detector_min_area_var.set(str(profile.detector.min_area))
        self.detector_max_area_var.set(str(profile.detector.max_area))
        self.detector_min_threshold_var.set(str(profile.detector.min_threshold))
        self.detector_inner_margin_var.set(str(profile.detector.inner_margin_px))
        self.detector_max_flies_var.set(str(profile.detector.max_flies_per_vial))
        self.detector_threshold_hysteresis_var.set(str(profile.detector.threshold_hysteresis_px))

        self.motor_enabled_var.set(bool(profile.motor.enabled))
        self.motor_pin_var.set(str(profile.motor.gpio_pin))
        self.motor_pulse_ms_var.set(str(profile.motor.pulse_ms))
        self.motor_settle_ms_var.set(str(profile.motor.settle_delay_ms))
        self.motor_active_high_var.set(bool(profile.motor.active_high))

        box_config_path, box_tokens_path = self._default_box_paths()
        self.box_enabled_var.set(bool(profile.box_upload.enabled))
        self.box_parent_folder_var.set(profile.box_upload.parent_folder_id)
        self.box_tokens_file_var.set(profile.box_upload.tokens_file or str(box_tokens_path))
        self.box_config_file_var.set(profile.box_upload.config_file or "")
        self.box_upload_after_processing_var.set(bool(profile.box_upload.upload_after_processing))
        self.box_upload_after_recording_var.set(bool(profile.box_upload.upload_after_recording))
        self.box_upload_backgrounds_var.set(bool(profile.box_upload.upload_backgrounds))
        self.box_artifact_mode_var.set(profile.box_upload.artifact_mode)
        self.box_folder_prefix_var.set(profile.box_upload.folder_prefix)

        self.profile_store.set_last_used(self.profile_store.profile_path(profile.name))
        self._refresh_profile_combo()
        self._refresh_device_labels()
        self._refresh_assay_background_info()
        self._maybe_load_profile_calibration()
        self._on_transform_changed()
        self._set_footer(f"Loaded profile: {profile.name}")
        self._log_assay(f"Loaded assay profile: {profile.name}")

    def _maybe_load_profile_calibration(self) -> None:
        cal_path = Path(self.assay_calibration_path_var.get())
        if cal_path.exists():
            try:
                self.load_assay_calibration_into_editor(silent=True)
            except Exception as exc:
                self._log_assay(f"Calibration load warning: {exc}")

    def _build_profile_from_vars(self) -> AssayProfile:
        profile = self.current_profile.copy() if self.current_profile is not None else self._default_profile(self.profile_name_var.get() or "default")
        profile.name = self.profile_name_var.get().strip() or "default"
        profile.description = self.profile_desc_var.get().strip()
        profile.assay_camera.backend = self.assay_backend_var.get().strip() or "opencv"
        profile.assay_camera.device = "auto:assay"
        self.assay_device_var.set("auto:assay")
        profile.assay_camera.preferred_hint = self.assay_preferred_hint_var.get().strip()
        profile.assay_camera.width = self._int_var(self.assay_width_var, 1536)
        profile.assay_camera.height = self._int_var(self.assay_height_var, 864)
        profile.assay_camera.fps = self._float_var(self.assay_record_fps_var, 30.0)
        profile.assay_duration_s = self._float_var(self.assay_duration_var, 10.0)
        profile.record_preroll_s = self._float_var(self.assay_preroll_var, 0.0)
        profile.analysis.analysis_fps = self._float_var(self.analysis_fps_var, 5.0)
        profile.analysis.frame_average_count = self._int_var(self.assay_frame_average_var, 1)
        profile.analysis.smoothing_window = self._int_var(self.assay_smoothing_window_var, 3)
        profile.analysis.auto_process_after_recording = bool(self.assay_auto_process_var.get())
        profile.analysis.alignment_enabled = bool(self.assay_alignment_var.get())
        profile.analysis.save_mask_video = bool(self.assay_save_mask_video_var.get())
        profile.analysis.show_positions = bool(self.assay_show_positions_var.get())
        profile.analysis.tracking_mode = (self.assay_tracking_mode_var.get().strip().lower() or "stitch")
        profile.analysis.use_mask_blobs_as_flies = bool(self.assay_mask_blobs_var.get())
        profile.outputs.save_preview_snapshots = bool(self.assay_save_snapshots_var.get())
        profile.outputs.snapshot_interval_s = self._float_var(self.assay_snapshot_interval_var, 1.0)
        profile.detector.min_area = self._int_var(self.detector_min_area_var, 10)
        profile.detector.max_area = self._int_var(self.detector_max_area_var, 250)
        profile.detector.min_threshold = self._float_var(self.detector_min_threshold_var, 12.0)
        profile.detector.inner_margin_px = self._int_var(self.detector_inner_margin_var, 8)
        profile.detector.max_flies_per_vial = self._int_var(self.detector_max_flies_var, 10)
        profile.detector.threshold_hysteresis_px = self._float_var(self.detector_threshold_hysteresis_var, 1.5)
        profile.motor.enabled = bool(self.motor_enabled_var.get())
        profile.motor.gpio_pin = self._int_var(self.motor_pin_var, 18)
        profile.motor.pulse_ms = self._int_var(self.motor_pulse_ms_var, 400)
        profile.motor.settle_delay_ms = self._int_var(self.motor_settle_ms_var, 150)
        profile.motor.active_high = bool(self.motor_active_high_var.get())
        profile.motor.backend = "module"
        profile.motor.module_name = "vibration"
        profile.outputs.output_root = self.assay_output_root_var.get().strip() or str((self.project_root / "outputs" / "assay").resolve())
        profile.calibration_path = self.assay_calibration_path_var.get().strip() or str((self.project_root / "calibrations" / f"{profile.slug}_calibration.json").resolve())
        profile.transform = self._build_transform_from_vars()
        box_config_path, box_tokens_path = self._default_box_paths()
        profile.box_upload.enabled = bool(self.box_enabled_var.get())
        profile.box_upload.parent_folder_id = self.box_parent_folder_var.get().strip()
        profile.box_upload.tokens_file = self.box_tokens_file_var.get().strip() or str(box_tokens_path)
        profile.box_upload.config_file = self.box_config_file_var.get().strip()
        profile.box_upload.upload_after_processing = bool(self.box_upload_after_processing_var.get())
        profile.box_upload.upload_after_recording = bool(self.box_upload_after_recording_var.get())
        profile.box_upload.upload_backgrounds = bool(self.box_upload_backgrounds_var.get())
        profile.box_upload.artifact_mode = self.box_artifact_mode_var.get().strip() or "summaries"
        profile.box_upload.folder_prefix = self.box_folder_prefix_var.get().strip() or "fly_assay"
        if self.current_profile is not None:
            profile.current_background_path = self.current_profile.current_background_path
            profile.previous_background_path = self.current_profile.previous_background_path
            profile.background_meta_path = self.current_profile.background_meta_path
            profile.last_run_dir = self.current_profile.last_run_dir
        return profile

    def _build_transform_from_vars(self) -> TransformSettings:
        crop_vals = [self.transform_crop_x_var.get(), self.transform_crop_y_var.get(), self.transform_crop_w_var.get(), self.transform_crop_h_var.get()]
        crop = None
        if all(str(v).strip() != "" for v in crop_vals):
            crop = [self._int_value(v, 0) for v in crop_vals]
        return TransformSettings(
            rotation_deg=self._float_var(self.transform_rotation_var, 0.0),
            flip_horizontal=bool(self.transform_flip_h_var.get()),
            flip_vertical=bool(self.transform_flip_v_var.get()),
            crop_xywh=crop,
        )

    def save_profile(self) -> None:
        try:
            profile = self._build_profile_from_vars()
            path = self.profile_store.save_profile(profile)
            self.load_profile(profile)
            self.current_profile_path = path
            self.profile_path_var.set(str(path.resolve()))
            self._log_assay(f"Saved profile: {path}")
        except Exception as exc:
            messagebox.showerror("Profile save error", str(exc))

    def new_profile(self) -> None:
        name = simpledialog.askstring("New profile", "Profile name:", initialvalue="assay_profile")
        if not name:
            return
        profile = self._default_profile(name)
        self.load_profile(profile)

    def duplicate_profile(self) -> None:
        if self.current_profile is None:
            return
        name = simpledialog.askstring("Duplicate profile", "New profile name:", initialvalue=f"{self.current_profile.name}_copy")
        if not name:
            return
        profile = self.current_profile.copy(new_name=name)
        profile.calibration_path = str((self.project_root / "calibrations" / f"{profile.slug}_calibration.json").resolve())
        self.profile_store.save_profile(profile)
        self.load_profile(profile)

    def load_selected_profile(self) -> None:
        name = self.profile_combo_var.get().strip()
        if not name:
            return
        try:
            profile = self.profile_store.load_profile(name)
            self.load_profile(profile)
        except Exception as exc:
            messagebox.showerror("Profile load error", str(exc))

    # ------------------------------------------------------------------
    # logging / queue helpers
    # ------------------------------------------------------------------
    def _set_footer(self, text: str) -> None:
        self.footer_var.set(text)

    def _update_status_labels(self) -> None:
        self.transform_status_var.set(f"Transform: {describe_transform(self._build_transform_from_vars())}")
        if self.current_profile is not None:
            self.profile_last_run_var.set(f"Last run: {self.current_profile.last_run_dir or 'none'}")

    def _thread_logger(self, channel: str) -> Callable[[str], None]:
        def logger(msg: str) -> None:
            self.ui_queue.put(("log", {"channel": channel, "text": msg}))
        return logger

    def _log_assay(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}\n"
        self.assay_log_widget.insert("end", line)
        self.assay_log_widget.see("end")

    def _log_channel(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}\n"
        self.channel_log_widget.insert("end", line)
        self.channel_log_widget.see("end")

    def _start_worker(self, success_kind: str, func: Callable[..., Any], *args: Any, error_title: str = "Task error", **kwargs: Any) -> None:
        if self.assay_worker_thread is not None and self.assay_worker_thread.is_alive():
            messagebox.showwarning("Busy", "Another assay task is already running.")
            return

        def worker() -> None:
            try:
                result = func(*args, **kwargs)
                self.ui_queue.put((success_kind, result))
            except Exception as exc:
                self.ui_queue.put(("task_error", {"title": error_title, "error": str(exc), "trace": traceback.format_exc()}))

        self.assay_worker_thread = threading.Thread(target=worker, daemon=True)
        self.assay_worker_thread.start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    channel = payload.get("channel")
                    text = payload.get("text", "")
                    if channel == "channel":
                        self._log_channel(text)
                    else:
                        self._log_assay(text)
                    self.app_status_var.set(text)
                    self._set_footer(text)
                elif kind == "task_error":
                    self.app_status_var.set(payload.get("error", "Task failed"))
                    self._set_footer(payload.get("error", "Task failed"))
                    self._log_assay(payload.get("error", "Task failed"))
                    messagebox.showerror(payload.get("title", "Task error"), payload.get("error", "Task failed"))
                elif kind == "assay_background_done":
                    self._on_assay_background_done(payload)
                elif kind == "assay_background_restored":
                    self._on_assay_background_done(payload)
                elif kind == "assay_preview":
                    self._on_assay_preview(payload)
                elif kind == "assay_transform_frame":
                    self._on_assay_transform_frame(payload)
                elif kind == "assay_calibration_done":
                    self._on_guided_calibration_done(payload)
                elif kind == "assay_record_done":
                    self._on_assay_record_done(payload)
                elif kind == "assay_process_done":
                    self._on_assay_process_done(payload)
                elif kind == "assay_batch_done":
                    self._on_assay_batch_done(payload)
                elif kind == "motor_test_done":
                    backend = ""
                    if isinstance(payload, dict):
                        backend = str(payload.get("backend_name", "") or "")
                    text = f"Motor test complete ({backend})" if backend else "Motor test complete"
                    self.app_status_var.set(text)
                    self._log_assay(text)
                elif kind == "upload_done":
                    self._log_assay("Upload complete")
                    messagebox.showinfo("Upload complete", json.dumps(payload, indent=2))
                elif kind == "channel_preview":
                    self._on_channel_preview(payload)
                elif kind == "channel_background_done":
                    self._log_channel(f"Saved channel background: {payload}")
                    self.channel_background_var.set(str(payload))
                    self.channel_canvas.set_image_bgr(read_bgr(payload))
                    self.channel_preview_info_var.set(f"Background saved to {payload}")
                elif kind == "channel_calibration_done":
                    self._log_channel(f"Saved channel calibration: {payload}")
                    self.channel_calibration_var.set(str(payload))
                elif kind == "channel_live_stopped":
                    self.channel_live_btn.configure(text="Start live")
        except queue.Empty:
            pass
        finally:
            self._update_status_labels()
            self.after(80, self._poll_queue)

    # ------------------------------------------------------------------
    # utility conversion helpers
    # ------------------------------------------------------------------
    def _int_value(self, raw: Any, default: int) -> int:
        try:
            return int(float(str(raw).strip()))
        except Exception:
            return int(default)

    def _float_value(self, raw: Any, default: float) -> float:
        try:
            return float(str(raw).strip())
        except Exception:
            return float(default)

    def _int_var(self, variable: tk.Variable, default: int) -> int:
        return self._int_value(variable.get(), default)

    def _float_var(self, variable: tk.Variable, default: float) -> float:
        return self._float_value(variable.get(), default)

    def _refresh_device_labels(self) -> None:
        profile = self._build_profile_from_vars() if self.current_profile is not None else self._default_profile()
        assay_desc = describe_camera_selection(profile.assay_camera.device, role="assay", preferred_hint=profile.assay_camera.preferred_hint)
        if assay_desc is None:
            self.assay_camera_status_var.set(
                "Assay camera: HD Webcam eMeet C960 on usb-xhci-hcd.1-2 not resolved"
            )
        else:
            self.assay_camera_status_var.set(f"Assay camera: {assay_desc.card_name} -> {assay_desc.stable_path}")

    def show_camera_inventory(self) -> None:
        devices = list_video_devices(prefer_index_zero=True)
        if not devices:
            messagebox.showinfo("Camera inventory", "No /dev/video capture devices were found.")
            return
        lines = []
        for dev in devices:
            lines.append(f"{dev.card_name}\n  stable={dev.stable_path}\n  device={dev.device_path}\n  brio={dev.is_brio}")
        messagebox.showinfo("Camera inventory", "\n\n".join(lines))

    # ------------------------------------------------------------------
    # assay background / transform helpers
    # ------------------------------------------------------------------
    def _current_background_store(self):
        profile = self._build_profile_from_vars()
        return get_background_store(profile, self.project_root)

    def _refresh_assay_background_info(self) -> None:
        try:
            profile = self._build_profile_from_vars()
        except Exception:
            profile = self.current_profile or self._default_profile()
        store = get_background_store(profile, self.project_root)
        current = store.load_current()
        previous = store.load_previous()
        self.assay_background_info_var.set(
            "Current background: none" if current is None else f"Current background: {Path(current.transformed_path).name}  ({current.captured_at})"
        )
        self.assay_previous_background_info_var.set(
            "Previous background: none" if previous is None else f"Previous background: {Path(previous.transformed_path).name}  ({previous.captured_at})"
        )
        if current is not None:
            self.current_profile.current_background_path = current.transformed_path
            self.current_profile.previous_background_path = previous.transformed_path if previous is not None else ""
            self.current_profile.background_meta_path = str(store.current_meta_path.resolve())
        preview_path = current_background_preview_path(profile, self.project_root)
        if preview_path is not None and preview_path.exists() and self.assay_preview_mode_var.get() in {"background", "calibration"}:
            try:
                image = read_bgr(preview_path)
                self.assay_preview_images["background"] = image
                if self.assay_preview_mode_var.get() == "background":
                    self.assay_canvas.set_image_bgr(image)
            except Exception:
                pass

    def _current_background_bgr(self):
        preview_path = current_background_preview_path(self._build_profile_from_vars(), self.project_root)
        if preview_path is None or not preview_path.exists():
            return None
        return read_bgr(preview_path)

    def _current_raw_background_bgr(self):
        store = self._current_background_store()
        if store.current_raw_path.exists():
            return read_bgr(store.current_raw_path)
        return None

    def _on_transform_changed(self) -> None:
        self.transform_status_var.set(f"Transform: {describe_transform(self._build_transform_from_vars())}")
        self._refresh_assay_canvas()

    def capture_assay_background(self) -> None:
        profile = self._build_profile_from_vars()
        self.save_profile()
        self._start_worker(
            "assay_background_done",
            capture_profile_background,
            profile,
            self.project_root,
            logger=self._thread_logger("assay"),
            error_title="Background capture error",
        )

    def import_assay_background(self) -> None:
        image_path = filedialog.askopenfilename(initialdir=str(self.project_root), filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not image_path:
            return
        profile = self._build_profile_from_vars()
        self.save_profile()
        self._start_worker(
            "assay_background_done",
            import_profile_background,
            profile,
            self.project_root,
            image_path,
            logger=self._thread_logger("assay"),
            error_title="Background import error",
        )

    def restore_assay_background(self) -> None:
        profile = self._build_profile_from_vars()
        self._start_worker(
            "assay_background_restored",
            restore_previous_background,
            profile,
            self.project_root,
            error_title="Background restore error",
        )

    def rebuild_background_transform(self) -> None:
        profile = self._build_profile_from_vars()

        def task() -> Any:
            store = get_background_store(profile, self.project_root)
            result = store.rebuild_current_transform(profile.transform)
            if result is None:
                raise BackgroundError("No current background is available to rebuild.")
            return result

        self._start_worker("assay_background_done", task, error_title="Background rebuild error")

    def _on_assay_background_done(self, record: Any) -> None:
        self._refresh_assay_background_info()
        if self.current_profile is not None:
            store = self._current_background_store()
            self.current_profile.current_background_path = str(store.current_transformed_path.resolve())
            self.current_profile.previous_background_path = str(store.previous_transformed_path.resolve()) if store.previous_transformed_path.exists() else ""
            self.current_profile.background_meta_path = str(store.current_meta_path.resolve())
            self.save_profile()
        current_bgr = self._current_background_bgr()
        if current_bgr is not None:
            self.assay_preview_images["background"] = current_bgr
            self.assay_preview_images["calibration"] = current_bgr
            if self.assay_preview_mode_var.get() in {"background", "calibration"}:
                self._refresh_assay_canvas()
        self._log_assay(f"Background ready: {getattr(record, 'transformed_path', record)}")
        self._set_footer("Background updated.")

    def capture_transform_test_frame(self) -> None:
        profile = self._build_profile_from_vars()

        def task() -> Dict[str, Any]:
            from assay_tracking import capture_assay_frame
            raw_frame = capture_assay_frame(
                width=int(profile.assay_camera.width),
                height=int(profile.assay_camera.height),
                fps=float(profile.assay_camera.fps),
                camera_backend=profile.assay_camera.backend,
                camera_device=profile.assay_camera.device,
                camera_index=int(profile.assay_camera.camera_index),
            )
            transformed = apply_image_transform(raw_frame, profile.transform)
            return {"raw_bgr": raw_frame, "transformed_bgr": transformed}

        self._start_worker("assay_transform_frame", task, error_title="Transform test frame error")

    def _on_assay_transform_frame(self, payload: Dict[str, Any]) -> None:
        self.latest_assay_raw_frame = payload.get("raw_bgr")
        self.assay_preview_images["raw"] = payload.get("raw_bgr")
        self.assay_preview_images["transform"] = payload.get("transformed_bgr")
        self.assay_preview_images["annotated"] = payload.get("transformed_bgr")
        self.assay_preview_info_var.set("Captured a fresh assay frame for transform preview.")
        self.assay_preview_mode_var.set("transform")
        self._refresh_assay_canvas()

    def test_transform(self) -> None:
        raw_frame = self.latest_assay_raw_frame
        if raw_frame is None:
            raw_frame = self._current_raw_background_bgr()
        if raw_frame is None:
            messagebox.showinfo("Transform preview", "Capture a test frame or background first.")
            return
        transformed = apply_image_transform(raw_frame, self._build_transform_from_vars())
        self.assay_preview_images["raw"] = raw_frame
        self.assay_preview_images["transform"] = transformed
        self.assay_preview_mode_var.set("transform")
        self.assay_preview_info_var.set("Transform preview updated.")
        self._refresh_assay_canvas()

    def select_transform_crop(self) -> None:
        raw_frame = self.latest_assay_raw_frame
        if raw_frame is None:
            raw_frame = self._current_raw_background_bgr()
        if raw_frame is None:
            messagebox.showinfo("Select crop", "Capture a test frame or background first.")
            return
        transform = self._build_transform_from_vars()
        transform.crop_xywh = None
        preview = apply_image_transform(raw_frame, transform)
        rect = cv2.selectROI("Select crop", preview, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow("Select crop")
        if rect is None or len(rect) != 4:
            return
        x, y, w, h = [int(v) for v in rect]
        if w <= 1 or h <= 1:
            return
        self.transform_crop_x_var.set(str(x))
        self.transform_crop_y_var.set(str(y))
        self.transform_crop_w_var.set(str(w))
        self.transform_crop_h_var.set(str(h))
        self._on_transform_changed()
        self.test_transform()

    def clear_transform_crop(self) -> None:
        for var in [self.transform_crop_x_var, self.transform_crop_y_var, self.transform_crop_w_var, self.transform_crop_h_var]:
            var.set("")
        self._on_transform_changed()
        self.test_transform()

    def crop_transform_to_regions(self) -> None:
        regions = self.assay_canvas.get_regions()
        if not regions:
            messagebox.showinfo("Crop to vials", "Draw or load at least one calibrated vial first.")
            return
        crop = merge_crop_from_regions([(r.x, r.y, r.w, r.h) for r in regions], padding_px=12)
        if crop is None:
            return
        self.transform_crop_x_var.set(str(crop[0]))
        self.transform_crop_y_var.set(str(crop[1]))
        self.transform_crop_w_var.set(str(crop[2]))
        self.transform_crop_h_var.set(str(crop[3]))
        self._on_transform_changed()
        self._refresh_assay_canvas()

    # ------------------------------------------------------------------
    # calibration helpers
    # ------------------------------------------------------------------
    def split_four_vials_mode(self) -> None:
        self.assay_canvas.set_split_count(max(2, self._int_var(self.assay_expected_vials_var, 4)))
        self.assay_canvas.set_tool("split")

    def _background_for_calibration(self):
        background = self._current_background_bgr()
        if background is None:
            raise BackgroundError("Capture or import a background before calibrating.")
        return background

    def _build_current_assay_calibration(self) -> AssayCalibration:
        background_bgr = self._background_for_calibration()
        regions = self.assay_canvas.get_regions()
        if not regions:
            raise ValueError("Define at least one vial region before saving or testing calibration.")
        tube_height_mm = None if not self.assay_tube_height_mm_var.get().strip() else self._float_var(self.assay_tube_height_mm_var, 0.0)
        tube_width_mm = None if not self.assay_tube_width_mm_var.get().strip() else self._float_var(self.assay_tube_width_mm_var, 0.0)
        vials = [
            region.to_vial(idx + 1, tube_height_mm=tube_height_mm, tube_width_mm=tube_width_mm)
            for idx, region in enumerate(sorted(regions, key=lambda r: (r.x, r.y)))
        ]
        preview_path = current_background_preview_path(self._build_profile_from_vars(), self.project_root)
        calibration = build_assay_calibration(
            background_bgr=background_bgr,
            vials=vials,
            background_path=None if preview_path is None else str(preview_path.resolve()),
        )
        return calibration

    def save_assay_calibration_from_editor(self, silent: bool = False) -> bool:
        try:
            profile = self._build_profile_from_vars()
            calibration = self._build_current_assay_calibration()
            path = Path(profile.calibration_path).expanduser()
            if not path.is_absolute():
                path = (self.project_root / path).resolve()
            if path.exists() and not silent:
                if not messagebox.askyesno("Overwrite calibration?", f"Overwrite existing calibration?\n\n{path}"):
                    return False
            save_assay_calibration(path, calibration)
            self.assay_calibration_path_var.set(str(path.resolve()))
            self._log_assay(f"Saved calibration: {path}")
            self.assay_preview_images["calibration"] = render_assay_calibration_overlay(background_bgr := self._background_for_calibration(), calibration)
            self._refresh_assay_canvas()
            return True
        except Exception as exc:
            if not silent:
                messagebox.showerror("Calibration save error", str(exc))
            return False

    def load_assay_calibration_into_editor(self, silent: bool = False) -> bool:
        try:
            path = Path(self.assay_calibration_path_var.get()).expanduser()
            if not path.is_absolute():
                path = (self.project_root / path).resolve()
            calibration = load_assay_calibration(path)
            background_bgr = self._background_for_calibration()
            expected_hw = [background_bgr.shape[0], background_bgr.shape[1]]
            if list(calibration.image_shape_hw) != expected_hw:
                raise ValueError(
                    f"Calibration expects HxW={calibration.image_shape_hw}, but the current transformed background is HxW={expected_hw}."
                )
            regions = [EditorRegion.from_vial(vial) for vial in calibration.vials]
            self.assay_canvas.set_regions(regions, clear_history=True)
            self.assay_canvas.set_tool("select")
            if calibration.vials:
                h_mm = next((v.tube_height_mm for v in calibration.vials if v.tube_height_mm is not None), None)
                w_mm = next((v.tube_width_mm for v in calibration.vials if v.tube_width_mm is not None), None)
                self.assay_tube_height_mm_var.set("" if h_mm is None else str(h_mm))
                self.assay_tube_width_mm_var.set("" if w_mm is None else str(w_mm))
                self.assay_expected_vials_var.set(str(len(calibration.vials)))
            self.assay_preview_mode_var.set("calibration")
            self._refresh_assay_canvas()
            self._log_assay(f"Loaded calibration: {path}")
            return True
        except Exception as exc:
            if not silent:
                messagebox.showerror("Calibration load error", str(exc))
            return False

    def guided_assay_calibration(self) -> None:
        try:
            background_path = current_background_preview_path(self._build_profile_from_vars(), self.project_root)
            if background_path is None:
                raise BackgroundError("Capture or import a background before running guided calibration.")
        except Exception as exc:
            messagebox.showerror("Guided calibration", str(exc))
            return

        total_vials = self._int_var(self.assay_expected_vials_var, 4)
        tube_height_mm = None if not self.assay_tube_height_mm_var.get().strip() else self._float_var(self.assay_tube_height_mm_var, 0.0)
        tube_width_mm = None if not self.assay_tube_width_mm_var.get().strip() else self._float_var(self.assay_tube_width_mm_var, 0.0)

        def task() -> AssayCalibration:
            return calibrate_assay_interactive(
                background_path=background_path,
                output_json=self.assay_calibration_path_var.get(),
                total_vials=total_vials,
                ignored_physical_indices=[],
                tube_height_mm=tube_height_mm,
                tube_width_mm=tube_width_mm,
            )

        self._start_worker("assay_calibration_done", task, error_title="Guided calibration error")

    def _on_guided_calibration_done(self, calibration: AssayCalibration) -> None:
        regions = [EditorRegion.from_vial(vial) for vial in calibration.vials]
        self.assay_canvas.set_regions(regions, clear_history=True)
        self.assay_preview_mode_var.set("calibration")
        self._refresh_assay_canvas()
        self._log_assay("Guided calibration complete. Review, edit if needed, then save.")

    def test_assay_calibration(self) -> None:
        try:
            profile = self._build_profile_from_vars()
            calibration = self._build_current_assay_calibration()
            background_bgr = self._background_for_calibration()
        except Exception as exc:
            messagebox.showerror("Calibration test error", str(exc))
            return

        def task() -> Dict[str, Any]:
            from assay_tracking import capture_assay_frame
            raw_frame = capture_assay_frame(
                width=int(profile.assay_camera.width),
                height=int(profile.assay_camera.height),
                fps=float(profile.assay_camera.fps),
                camera_backend=profile.assay_camera.backend,
                camera_device=profile.assay_camera.device,
                camera_index=int(profile.assay_camera.camera_index),
            )
            transformed_frame = apply_image_transform(raw_frame, profile.transform)
            preview_images, rows, meta = preview_assay_frame(
                background_bgr=background_bgr,
                frame_bgr=transformed_frame,
                calibration=calibration,
                min_area=int(profile.detector.min_area),
                max_area=int(profile.detector.max_area),
                min_threshold=float(profile.detector.min_threshold),
                inner_margin_px=int(profile.detector.inner_margin_px),
                no_align=not bool(profile.analysis.alignment_enabled),
                max_flies_per_vial=int(profile.detector.max_flies_per_vial),
                show_positions=bool(profile.analysis.show_positions),
            )
            return {"preview_images": preview_images, "rows": rows, "meta": meta}

        self._start_worker("assay_preview", task, error_title="Calibration test error")

    def _on_canvas_regions_changed(self) -> None:
        self._update_region_tree()
        self._update_region_editor()
        if self.assay_preview_mode_var.get() == "calibration":
            self._refresh_assay_canvas()

    def _on_canvas_region_selected(self, _index: Optional[int]) -> None:
        self._update_region_tree(select_canvas=True)
        self._update_region_editor()

    def _update_region_tree(self, select_canvas: bool = False) -> None:
        if not hasattr(self, "region_tree"):
            return
        tree = self.region_tree
        tree.delete(*tree.get_children())
        for idx, region in enumerate(self.assay_canvas.get_regions(), start=1):
            tree.insert("", "end", iid=str(idx - 1), values=(idx, region.label or f"Vial {idx}", "active" if region.enabled else "ignored", int(region.threshold_y)))
        if select_canvas and self.assay_canvas.selected_index is not None and tree.exists(str(self.assay_canvas.selected_index)):
            selected_iid = str(self.assay_canvas.selected_index)
            tree.selection_set(selected_iid)
            tree.see(selected_iid)
        elif not select_canvas:
            tree.selection_remove(*tree.selection())

    def _on_region_tree_selected(self, _event) -> None:
        sel = self.region_tree.selection()
        if not sel:
            return
        try:
            self.assay_canvas.set_selected_index(int(sel[0]))
        except Exception:
            return

    def _update_region_editor(self) -> None:
        region = self.assay_canvas.current_region()
        if region is None:
            for var in [
                self.region_label_var, self.region_x_var, self.region_y_var, self.region_w_var,
                self.region_h_var, self.region_top_var, self.region_threshold_var, self.region_baseline_var,
            ]:
                var.set("")
            self.region_enabled_var.set(True)
            return
        self.region_label_var.set(region.label)
        self.region_enabled_var.set(bool(region.enabled))
        self.region_x_var.set(str(region.x))
        self.region_y_var.set(str(region.y))
        self.region_w_var.set(str(region.w))
        self.region_h_var.set(str(region.h))
        self.region_top_var.set(str(region.top_y))
        self.region_threshold_var.set(str(region.threshold_y))
        self.region_baseline_var.set(str(region.baseline_y))

    def apply_selected_region_form(self) -> None:
        region = self.assay_canvas.current_region()
        if region is None:
            return
        self.assay_canvas.update_selected_region(
            label=self.region_label_var.get().strip(),
            enabled=bool(self.region_enabled_var.get()),
            x=self._int_var(self.region_x_var, region.x),
            y=self._int_var(self.region_y_var, region.y),
            w=self._int_var(self.region_w_var, region.w),
            h=self._int_var(self.region_h_var, region.h),
            top_y=self._int_var(self.region_top_var, region.top_y),
            threshold_y=self._int_var(self.region_threshold_var, region.threshold_y),
            baseline_y=self._int_var(self.region_baseline_var, region.baseline_y),
        )

    def _refresh_assay_canvas(self) -> None:
        self._release_assay_video_playback(status_text=None)
        mode = self.assay_preview_mode_var.get()
        simple_canvas = getattr(self, "assay_simple_canvas", None)
        if mode == "calibration":
            background_bgr = self._current_background_bgr()
            if background_bgr is None:
                self.assay_canvas.set_image_bgr(None)
                self.assay_canvas.set_empty_text("Capture or import a background to begin calibration.")
                if simple_canvas is not None:
                    simple_canvas.set_empty_text("Record an assay or process a run to review it here.")
                    simple_canvas.set_image_bgr(None)
                return
            try:
                calibration = self._build_current_assay_calibration() if self.assay_canvas.get_regions() else None
            except Exception:
                calibration = None
            image = background_bgr
            if calibration is not None:
                image = render_assay_calibration_overlay(background_bgr, calibration)
            self.assay_canvas.set_show_regions(True)
            self.assay_canvas.set_editor_enabled(True)
            self.assay_canvas.set_image_bgr(image)
            if simple_canvas is not None:
                simple_canvas.set_image_bgr(image)
            return
        image = self.assay_preview_images.get(mode)
        if image is None and mode == "background":
            image = self._current_background_bgr()
        if image is None and mode == "transform":
            image = self.assay_preview_images.get("transform") or self._current_background_bgr()
        if image is None and mode == "raw":
            image = self.assay_preview_images.get("raw") or self._current_background_bgr()
        self.assay_canvas.set_show_regions(False)
        self.assay_canvas.set_editor_enabled(False)
        self.assay_canvas.set_image_bgr(image)
        if simple_canvas is not None:
            simple_canvas.set_image_bgr(image)

    def _on_assay_preview(self, payload: Dict[str, Any]) -> None:
        if "preview_images" in payload:
            preview_images = payload.get("preview_images", {})
            for key, value in preview_images.items():
                self.assay_preview_images[key] = value
            meta = payload.get("meta", {})
            detection_count = int(meta.get("detection_count", 0))
            active_track_count = int(meta.get("active_track_count", 0))
            self.assay_preview_info_var.set(f"detections={detection_count}  active_tracks={active_track_count}")
            self.assay_preview_mode_var.set("annotated")
            self._refresh_assay_canvas()
            return
        preview_bgr = payload.get("preview_bgr")
        if preview_bgr is not None:
            self.assay_preview_images["annotated"] = preview_bgr
            if payload.get("mask_bgr") is not None:
                self.assay_preview_images["mask"] = payload.get("mask_bgr")
            if payload.get("raw_frame_bgr") is not None:
                self.assay_preview_images["raw"] = payload.get("raw_frame_bgr")
            frame_index = payload.get("frame_index")
            time_s = payload.get("time_s")
            self.assay_preview_info_var.set(f"frame={frame_index}  t={time_s:0.2f}s")
            if self.assay_preview_mode_var.get() != "calibration":
                self.assay_canvas.set_show_regions(False)
                self.assay_canvas.set_editor_enabled(False)
                self.assay_canvas.set_image_bgr(preview_bgr)
                simple_canvas = getattr(self, "assay_simple_canvas", None)
                if simple_canvas is not None:
                    simple_canvas.set_image_bgr(preview_bgr)

    def _resolve_last_run_dir(self) -> Optional[Path]:
        candidates = [
            self.assay_preview_paths.get("run_dir", ""),
            "" if self.current_profile is None else str(self.current_profile.last_run_dir or ""),
            str(self._build_profile_from_vars().last_run_dir or ""),
        ]
        for raw in candidates:
            raw = str(raw or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.exists():
                return path.resolve()
        return None

    def _resolve_playback_source(self, kind: str) -> tuple[Path, bool]:
        kind = str(kind or "annotated").strip().lower()
        direct_raw = str(self.assay_preview_paths.get(kind, "") or "").strip()
        if direct_raw:
            direct_path = Path(direct_raw).expanduser()
            if direct_path.exists():
                return direct_path.resolve(), kind == "raw"

        run_dir = self._resolve_last_run_dir()
        if run_dir is None:
            raise FileNotFoundError("No recorded assay run is available for playback yet.")

        if kind == "raw":
            manifest_path = run_dir / "run_manifest.json"
            manifest = load_json(manifest_path) if manifest_path.exists() else {}
            candidates = []
            raw_video_path = str(manifest.get("raw_video_path", "") or "").strip()
            if raw_video_path:
                candidates.append(Path(raw_video_path).expanduser())
            candidates.extend([run_dir / "raw_video.mp4", run_dir / "raw_video.avi"])
        else:
            manifest_path = run_dir / "run_manifest.json"
            manifest = load_json(manifest_path) if manifest_path.exists() else {}
            processed_root = run_dir / "processed"
            latest_processing_path = processed_root / "latest_processing.json"
            latest_processing = load_json(latest_processing_path) if latest_processing_path.exists() else {}
            processing_dir_raw = str(latest_processing.get("processing_dir", manifest.get("processing_dir", "")) or "").strip()
            processing_dir = Path(processing_dir_raw).expanduser() if processing_dir_raw else None
            processing_json = (processing_dir / "processing_session.json") if processing_dir is not None else None
            if processing_json is None or not processing_json.exists():
                proc_dirs = sorted([p for p in processed_root.glob("proc_*") if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
                if proc_dirs:
                    processing_dir = proc_dirs[0]
                    processing_json = processing_dir / "processing_session.json"
            processing = load_json(processing_json) if processing_json is not None and processing_json.exists() else {}
            key = "annotated_video_path" if kind == "annotated" else "mask_video_path"
            candidates = []
            explicit = str(processing.get(key, latest_processing.get(key, manifest.get(key, ""))) or "").strip()
            if explicit:
                candidates.append(Path(explicit).expanduser())
            filename = "annotated_video.mp4" if kind == "annotated" else "mask_video.mp4"
            if processing_dir is not None:
                candidates.append(processing_dir / filename)
            candidates.append(run_dir / "processed" / filename)

        for candidate in candidates:
            candidate = candidate if candidate.is_absolute() else (run_dir / candidate)
            if candidate.exists():
                return candidate.resolve(), kind == "raw"
        raise FileNotFoundError(f"No {kind} playback video was found for run {run_dir.name}.")

    def _release_assay_video_playback(self, status_text: Optional[str] = None) -> None:
        if self.assay_playback_after_id is not None:
            try:
                self.after_cancel(self.assay_playback_after_id)
            except Exception:
                pass
            self.assay_playback_after_id = None
        if self.assay_playback_cap is not None:
            try:
                self.assay_playback_cap.release()
            except Exception:
                pass
        self.assay_playback_cap = None
        self.assay_playback_fps = 0.0
        self.assay_playback_frame_index = 0
        self.assay_playback_kind = ""
        self.assay_playback_video_path = None
        self.assay_playback_transform_raw = False
        if status_text is not None:
            self.playback_status_var.set(status_text)

    def play_assay_video(self, kind: str) -> None:
        try:
            video_path, transform_raw = self._resolve_playback_source(kind)
        except Exception as exc:
            messagebox.showerror("Playback", str(exc))
            return

        self._release_assay_video_playback(status_text=None)
        cap = cv2.VideoCapture(str(video_path))
        if cap is None or not cap.isOpened():
            messagebox.showerror("Playback", f"Could not open video:\n\n{video_path}")
            return

        fallback_fps = self._float_var(self.assay_record_fps_var, 30.0) if kind == "raw" else self._float_var(self.analysis_fps_var, 5.0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or fallback_fps or 5.0)
        if fps <= 0:
            fps = fallback_fps if fallback_fps > 0 else 5.0

        self.assay_playback_cap = cap
        self.assay_playback_fps = fps
        self.assay_playback_frame_index = 0
        self.assay_playback_kind = str(kind)
        self.assay_playback_video_path = video_path
        self.assay_playback_transform_raw = bool(transform_raw)
        self.assay_preview_mode_var.set("raw" if kind == "raw" else ("mask" if kind == "mask" else "annotated"))
        self.playback_status_var.set(f"Playing {kind}: {video_path.name}")
        self._step_assay_video_playback()

    def _step_assay_video_playback(self) -> None:
        if self.assay_playback_cap is None:
            return

        ok, frame_bgr = self.assay_playback_cap.read()
        if not ok or frame_bgr is None:
            self._release_assay_video_playback(status_text="Playback finished. Use Play to replay.")
            return

        if self.assay_playback_transform_raw:
            try:
                frame_bgr = apply_image_transform(frame_bgr, self._build_transform_from_vars())
            except Exception:
                pass

        frame_index = int(self.assay_playback_frame_index)
        time_s = float(frame_index) / max(1e-6, float(self.assay_playback_fps or 5.0))
        header = f"Playback {self.assay_playback_kind}  frame={frame_index}  t={time_s:0.2f}s"
        preview = frame_bgr.copy()
        cv2.putText(preview, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(preview, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (18, 18, 18), 1, cv2.LINE_AA)
        self.assay_canvas.set_show_regions(False)
        self.assay_canvas.set_editor_enabled(False)
        self.assay_canvas.set_image_bgr(preview)
        simple_canvas = getattr(self, "assay_simple_canvas", None)
        if simple_canvas is not None:
            simple_canvas.set_image_bgr(preview)
        self.assay_preview_info_var.set(header)
        self.assay_playback_frame_index += 1
        delay_ms = max(15, int(round(1000.0 / max(1e-6, float(self.assay_playback_fps or 5.0)))))
        self.assay_playback_after_id = self.after(delay_ms, self._step_assay_video_playback)

    def pause_assay_video_playback(self) -> None:
        if self.assay_playback_cap is None:
            self.playback_status_var.set("Playback is not running.")
            return
        self._release_assay_video_playback(status_text="Playback paused. Press Play to restart from the beginning.")

    def stop_assay_video_playback(self) -> None:
        self._release_assay_video_playback(status_text="Playback stopped.")

    # ------------------------------------------------------------------
    # assay recording / processing / upload
    # ------------------------------------------------------------------
    def process_assay_action(self) -> None:
        self.process_last_assay_run()

    def export_assay_action(self) -> None:
        self.upload_last_run(auto=False)

    def _ensure_profile_ready_for_recording(self) -> AssayProfile:
        profile = self._build_profile_from_vars()
        if self.assay_canvas.get_regions():
            if not self.save_assay_calibration_from_editor(silent=True):
                raise RecordingError("Could not save the current calibration before recording.")
        self.profile_store.save_profile(profile)
        self.current_profile = profile
        return profile

    def run_assay_recording(self) -> None:
        try:
            profile = self._ensure_profile_ready_for_recording()
        except Exception as exc:
            messagebox.showerror("Recording error", str(exc))
            return

        callback = lambda payload: self.ui_queue.put(("assay_preview", {**payload, "stage": "record"}))
        self._start_worker(
            "assay_record_done",
            record_assay_run,
            profile,
            self.project_root,
            preview_callback=callback,
            logger=self._thread_logger("assay"),
            error_title="Recording error",
        )

    def _on_assay_record_done(self, result: Dict[str, Any]) -> None:
        run_dir = str(result.get("run_dir", ""))
        self.assay_preview_paths["run_dir"] = run_dir
        if result.get("raw_video_path"):
            self.assay_preview_paths["raw"] = str(result.get("raw_video_path"))
        self.assay_preview_mode_var.set("annotated")
        self.assay_preview_info_var.set(f"Recorded run: {Path(run_dir).name}")
        self.playback_status_var.set("Raw video ready. Press Play raw to review the recording inside the GUI.")
        self._log_assay(f"Recording complete: {run_dir}")
        if self.current_profile is not None:
            self.current_profile.last_run_dir = run_dir
            self.profile_last_run_var.set(f"Last run: {run_dir}")
            self.profile_store.save_profile(self.current_profile)
        if should_auto_upload(self._build_profile_from_vars().box_upload, "recording"):
            self.upload_last_run(auto=True)
        if self.assay_auto_process_var.get():
            self.process_last_assay_run()
        else:
            messagebox.showinfo("Recording complete", f"Assay saved to:\n\n{run_dir}")

    def process_last_assay_run(self) -> None:
        try:
            profile = self._ensure_profile_ready_for_recording()
        except Exception as exc:
            messagebox.showerror("Process last assay", str(exc))
            return
        callback = lambda payload: self.ui_queue.put(("assay_preview", {**payload, "stage": "process"}))
        self._start_worker(
            "assay_process_done",
            process_last_assay,
            profile,
            self.project_root,
            logger=self._thread_logger("assay"),
            progress_callback=callback,
            error_title="Processing error",
        )

    def process_selected_assay_run(self) -> None:
        run_dir = filedialog.askdirectory(initialdir=self.assay_output_root_var.get() or str(self.project_root))
        if not run_dir:
            return
        profile = self._build_profile_from_vars()
        callback = lambda payload: self.ui_queue.put(("assay_preview", {**payload, "stage": "process"}))
        self._start_worker(
            "assay_process_done",
            process_assay_run,
            run_dir,
            profile_override=profile,
            logger=self._thread_logger("assay"),
            progress_callback=callback,
            error_title="Processing error",
        )

    def batch_process_assay_runs(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.assay_output_root_var.get() or str(self.project_root))
        if not folder:
            return
        profile = self._build_profile_from_vars()
        self._start_worker(
            "assay_batch_done",
            batch_process_folder,
            folder,
            profile_override=profile,
            logger=self._thread_logger("assay"),
            error_title="Batch processing error",
        )

    def _on_assay_process_done(self, result: Dict[str, Any]) -> None:
        run_dir = str(result.get("run_dir", ""))
        self.assay_preview_paths["run_dir"] = run_dir
        if result.get("annotated_video_path"):
            self.assay_preview_paths["annotated"] = str(result.get("annotated_video_path"))
        if result.get("mask_video_path"):
            self.assay_preview_paths["mask"] = str(result.get("mask_video_path"))
        if result.get("raw_video_path"):
            self.assay_preview_paths["raw"] = str(result.get("raw_video_path"))
        self._log_assay(f"Processing complete: {run_dir}")
        self.assay_preview_mode_var.set("annotated")
        if result.get("annotated_video_path"):
            self.assay_preview_info_var.set(f"Processed run: {Path(run_dir).name}")
            self.playback_status_var.set("Processed video ready. Press Play processed or Play mask to review it inside the GUI.")
        if self.current_profile is not None and run_dir:
            self.current_profile.last_run_dir = run_dir
            self.profile_store.save_profile(self.current_profile)
        messagebox.showinfo(
            "Processing complete",
            f"Processed run:\n\n{run_dir}\n\nPer-vial summary:\n{result.get('per_vial_summary_csv', '')}\n\nReport PDF:\n{result.get('report_pdf', '')}",
        )

    def _on_assay_batch_done(self, results: List[Dict[str, Any]]) -> None:
        okay = sum(1 for item in results if "error" not in item)
        failed = len(results) - okay
        self._log_assay(f"Batch processing finished. ok={okay} failed={failed}")
        messagebox.showinfo("Batch processing complete", f"Processed {len(results)} runs.\nSuccess: {okay}\nFailed: {failed}")

    def test_motor(self) -> None:
        profile = self._build_profile_from_vars()
        self._start_worker(
            "motor_test_done",
            pulse_vibration_motor,
            profile.motor,
            error_title="Motor error",
        )

    def upload_last_run(self, auto: bool = False) -> None:
        profile = self._build_profile_from_vars()
        run_dir = profile.last_run_dir or (self.current_profile.last_run_dir if self.current_profile is not None else "")
        if not run_dir:
            if not auto:
                messagebox.showinfo("Upload", "Record or process a run first.")
            return
        self._start_worker(
            "upload_done",
            manual_upload_run,
            run_dir,
            profile.box_upload,
            artifact_mode=None,
            logger=self._thread_logger("assay"),
            error_title="Upload error",
        )

    # ------------------------------------------------------------------
    # channel mode actions
    # ------------------------------------------------------------------
    def _channel_brio_config(self) -> BrioConfig:
        return BrioConfig(
            device="auto:channel",
            width=self._int_var(self.channel_width_var, 1920),
            height=self._int_var(self.channel_height_var, 1080),
            fps=self._int_var(self.channel_fps_var, 30),
        )

    def capture_channel_background(self) -> None:
        config = self._channel_brio_config()
        output_path = self.channel_background_var.get().strip() or str((self.project_root / "backgrounds" / "channel_bg.png").resolve())
        self._start_worker(
            "channel_background_done",
            capture_brio_background,
            output_path,
            device=config.device,
            width=config.width,
            height=config.height,
            fps=config.fps,
            error_title="Channel background error",
        )

    def run_channel_calibration(self) -> None:
        background_path = self.channel_background_var.get().strip()
        if not background_path or not Path(background_path).exists():
            messagebox.showerror("Channel calibration", "Capture or choose a Brio background first.")
            return
        output_path = self.channel_calibration_var.get().strip() or str((self.project_root / "calibrations" / "channel_calibration.json").resolve())
        self._start_worker(
            "channel_calibration_done",
            calibrate_channel,
            background_path,
            output_path,
            channel_mm=self._float_var(self.channel_mm_var, 111.0),
            error_title="Channel calibration error",
        )

    def detect_channel_once(self) -> None:
        background = self.channel_background_var.get().strip()
        calibration = self.channel_calibration_var.get().strip()
        if not background or not Path(background).exists():
            messagebox.showerror("Channel detect", "Capture or choose a Brio background first.")
            return
        if not calibration or not Path(calibration).exists():
            messagebox.showerror("Channel detect", "Calibrate the Brio channel first.")
            return
        config = self._channel_brio_config()
        score_thresh = self._int_var(self.channel_score_thresh_var, 20)
        band_half_width = self._int_var(self.channel_band_half_width_var, 35)
        no_align = bool(self.channel_no_align_var.get())

        def task() -> Dict[str, Any]:
            with BrioCamera(config) as camera:
                frame_bgr = camera.read()
            result, annotated, mask = process_fly_detection(
                background=background,
                frame=frame_bgr,
                calibration_path=calibration,
                score_thresh=score_thresh,
                band_half_width=band_half_width,
                no_align=no_align,
            )
            return {"result": result, "annotated": annotated, "mask": mask}

        self._start_worker("channel_preview", task, error_title="Channel detect error")

    def toggle_channel_live(self) -> None:
        if self.channel_live_thread is not None and self.channel_live_thread.is_alive():
            if self.channel_live_stop_event is not None:
                self.channel_live_stop_event.set()
            self.channel_live_btn.configure(text="Start live")
            return
        background = self.channel_background_var.get().strip()
        calibration = self.channel_calibration_var.get().strip()
        if not background or not Path(background).exists():
            messagebox.showerror("Channel live", "Capture or choose a Brio background first.")
            return
        if not calibration or not Path(calibration).exists():
            messagebox.showerror("Channel live", "Calibrate the Brio channel first.")
            return
        config = self._channel_brio_config()
        score_thresh = self._int_var(self.channel_score_thresh_var, 20)
        band_half_width = self._int_var(self.channel_band_half_width_var, 35)
        no_align = bool(self.channel_no_align_var.get())
        stop_event = threading.Event()
        self.channel_live_stop_event = stop_event
        self.channel_live_btn.configure(text="Stop live")

        def worker() -> None:
            try:
                with BrioCamera(config) as camera:
                    while not stop_event.is_set():
                        frame_bgr = camera.read()
                        result, annotated, mask = process_fly_detection(
                            background=background,
                            frame=frame_bgr,
                            calibration_path=calibration,
                            score_thresh=score_thresh,
                            band_half_width=band_half_width,
                            no_align=no_align,
                        )
                        self.ui_queue.put(("channel_preview", {"result": result, "annotated": annotated, "mask": mask}))
                        time.sleep(0.02)
            except Exception as exc:
                self.ui_queue.put(("task_error", {"title": "Channel live error", "error": str(exc)}))
            finally:
                self.ui_queue.put(("channel_live_stopped", None))

        self.channel_live_thread = threading.Thread(target=worker, daemon=True)
        self.channel_live_thread.start()

    def _on_channel_preview(self, payload: Dict[str, Any]) -> None:
        annotated = payload.get("annotated")
        if annotated is not None:
            self.channel_canvas.set_image_bgr(annotated)
        result = payload.get("result", {})
        count = int(result.get("count", 0))
        positions = result.get("x_positions_mm", [])
        self.channel_preview_info_var.set(f"count={count}  positions_mm={positions}")

    # ------------------------------------------------------------------
    # close / misc
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self.channel_live_stop_event is not None:
            self.channel_live_stop_event.set()
        self._release_assay_video_playback(status_text=None)
        self._save_settings()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
