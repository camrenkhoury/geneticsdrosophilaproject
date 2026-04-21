from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import contextlib
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk
from typing import Any, Callable


class RemoteAssayWorkspace(ttk.Frame):
    PREVIEW_MODES = ("calibration", "background", "transform", "annotated", "mask", "raw")

    def __init__(
        self,
        master: tk.Misc,
        *,
        get_controller: Callable[[], Any | None],
        on_back: Callable[[], None],
        can_exit_callback: Callable[[], bool] | None = None,
        status_callback: Callable[[str, str], None],
        log_callback: Callable[[str], None],
        open_setup_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, padding=6)
        self.get_controller = get_controller
        self.on_back = on_back
        self.can_exit_callback = can_exit_callback
        self.status_callback = status_callback
        self.log_callback = log_callback
        self.open_setup_callback = open_setup_callback

        self.cache_dir = Path(tempfile.gettempdir()) / "drosophila_remote_assay_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.connected = False
        self.remote_busy = False
        self.assay_available = False
        self.backend_status_text = "Disconnected"
        self._preview_source_image = None
        self._preview_photo = None
        self._worker_count = 0
        self._last_status_payload: dict[str, Any] | None = None
        self._last_manifest_payload: dict[str, Any] | None = None
        self.results_visible = False
        self.debug_window: tk.Toplevel | None = None
        self.calibration_window: tk.Toplevel | None = None
        self._setup_prompt_shown = False
        self.profile_combo = None
        self.calibration_text = None
        self.workflow_buttons: dict[str, ttk.Button] = {}
        self.calibration_buttons: dict[str, ttk.Button] = {}
        self.calibration_canvas: tk.Canvas | None = None
        self.calibration_step_var = tk.StringVar(value="Step 1: preparing assay background.")
        self.calibration_instruction_var = tk.StringVar(
            value="Open Calibration / Config to load or capture a clean assay background."
        )
        self.calibration_region_summary_var = tk.StringVar(value="No tube regions defined yet.")
        self.calibration_ready_var = tk.StringVar(value="Calibration not ready.")
        self.calibration_gate_var = tk.StringVar(value="Gate: background=no regions=0 saved=no tested=no continue=no")
        self._calibration_feedback_override: str | None = None
        self._calibration_image = None
        self._calibration_photo = None
        self._calibration_display_box: tuple[int, int, int, int, float] | None = None
        self._calibration_regions: list[dict[str, int]] = []
        self._calibration_draft_region: dict[str, int] | None = None
        self._calibration_drag_start: tuple[int, int] | None = None
        self._guided_calibration_saved = False
        self._guided_calibration_tested = False
        self._guided_calibration_busy = False
        self._guided_calibration_busy_message = ""
        self._video_capture = None
        self._video_after_id: str | None = None
        self._video_paused = False

        self.connection_var = tk.StringVar(value="Disconnected")
        self.workspace_status_var = tk.StringVar(value="Open the assay workspace to begin.")
        self.active_profile_var = tk.StringVar(value="")
        self.profile_combo_var = tk.StringVar(value="")
        self.profile_path_var = tk.StringVar(value="")
        self.background_var = tk.StringVar(value="")
        self.previous_background_var = tk.StringVar(value="")
        self.calibration_path_var = tk.StringVar(value="")
        self.last_run_var = tk.StringVar(value="")
        self.preview_mode_var = tk.StringVar(value="calibration")
        self.preview_info_var = tk.StringVar(value="No assay preview loaded.")
        self.results_status_var = tk.StringVar(value="No assay run loaded.")
        self.workflow_guidance_var = tk.StringVar(
            value=(
                "Workflow: 1) Calibration / Config: capture or import a clean background, load or edit calibration, "
                "then save and test. 2) Run assay recording. 3) Process assay. 4) Export to Box."
            )
        )

        self._build_layout()

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        topbar = ttk.Frame(self)
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        topbar.columnconfigure(1, weight=1)

        ttk.Button(topbar, text="Exit Assay", command=self._request_exit).grid(row=0, column=0, sticky="w")
        ttk.Label(topbar, text="Assay Workspace", font=("Arial", 11, "bold")).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Button(topbar, text="Debug", command=self.open_debug_menu).grid(row=0, column=2, sticky="e", padx=(0, 6))
        ttk.Button(topbar, text="Results", command=self.toggle_results).grid(row=0, column=3, sticky="e", padx=(0, 6))

        self.connection_label = tk.Label(
            topbar,
            textvariable=self.connection_var,
            bg="#607D8B",
            fg="white",
            padx=8,
            pady=2,
            relief="ridge",
            anchor="center",
        )
        self.connection_label.grid(row=0, column=4, sticky="e")

        self.status_label = tk.Label(
            self,
            textvariable=self.workspace_status_var,
            bg="#F3F4F6",
            fg="#111827",
            anchor="w",
            justify="left",
            padx=6,
            pady=2,
        )
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        self.status_label.bind(
            "<Configure>",
            lambda event: self.status_label.configure(wraplength=max(300, event.width - 12)),
        )

        self.body = ttk.Frame(self)
        self.body.grid(row=2, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        self.body.columnconfigure(1, weight=0, minsize=300)
        self.body.rowconfigure(0, weight=1)

        center_panel = ttk.Frame(self.body)
        center_panel.grid(row=0, column=0, sticky="nsew")
        center_panel.columnconfigure(0, weight=1)
        center_panel.rowconfigure(1, weight=5)
        center_panel.rowconfigure(2, weight=1)

        self.right_panel = ttk.Frame(self.body, width=300)
        self.right_panel.grid(row=0, column=1, sticky="nse", padx=(6, 0))
        self.right_panel.grid_propagate(False)
        self.right_panel.columnconfigure(0, weight=1)
        self.right_panel.rowconfigure(1, weight=1)

        self._build_preview_panel(center_panel)
        self._build_results_panel(self.right_panel)
        self._apply_panel_visibility()
        self._build_action_bar(self)

    def _build_action_bar(self, parent: ttk.Frame) -> None:
        action_bar = ttk.Frame(parent)
        action_bar.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        style = ttk.Style(self)
        style.configure("AssayAction.TButton", padding=(8, 7))
        for column in range(6):
            action_bar.columnconfigure(column, weight=1)

        workflow_actions = (
            ("calibration", "1) Calibration / Config", self.open_calibration_window),
            ("run", "2) Run Assay Recording", self.run_assay),
            ("process", "3) Process Assay", self.process_last),
            ("export", "4) Export to Box", self.upload_last),
            ("capture", "Capture Image", self.capture_preview),
        )
        optional_actions = (
            ("Play Raw", lambda: self.play_video_artifact("raw_video")),
            ("Play Processed", lambda: self.play_video_artifact("annotated_video")),
            ("Play Mask", lambda: self.play_video_artifact("mask_video")),
            ("Pause", self.pause_playback),
            ("Stop", self.stop_playback),
            ("Report / CSV", self.toggle_results),
        )
        self.workflow_buttons = {}
        for index, (key, label, command) in enumerate(workflow_actions):
            column_count = len(workflow_actions)
            padx = (0, 4) if index == 0 else ((4, 0) if index == column_count - 1 else 4)
            button = ttk.Button(action_bar, text=label, command=command, style="AssayAction.TButton")
            button.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=padx,
                pady=(0, 4),
            )
            self.workflow_buttons[key] = button
        for index, (label, command) in enumerate(optional_actions):
            column_count = len(optional_actions)
            column = index
            padx = (0, 4) if column == 0 else ((4, 0) if column == column_count - 1 else 4)
            ttk.Button(action_bar, text=label, command=command, style="AssayAction.TButton").grid(
                row=1,
                column=column,
                sticky="ew",
                padx=padx,
                pady=(4, 0),
            )
        self._update_workflow_button_states()

    def _build_scrollable_controls(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, highlightthickness=0, bd=0, background="#f3f4f6")
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        body = ttk.Frame(canvas, padding=2)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(_event=None) -> None:
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        body.bind("<Configure>", _sync_region)
        canvas.bind("<Configure>", _sync_width)

        row = 0
        row = self._build_profile_card(body, row)
        row = self._build_preview_card(body, row)
        row = self._build_recording_card(body, row)
        row = self._build_processing_card(body, row)
        row = self._build_upload_card(body, row)
        row = self._build_artifact_card(body, row)
        body.rowconfigure(row, weight=1)

    def _card(self, parent: ttk.Frame, row: int, title: str, subtitle: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=6)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 5))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=subtitle, foreground="#4b5563", wraplength=220, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 5)
        )
        return frame

    def _build_profile_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Profile / Debug", "Active Integrated3 assay profile on the Pi.")
        ttk.Label(frame, text="Active").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Label(frame, textvariable=self.active_profile_var).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(frame, text="Profile path").grid(row=2, column=0, sticky="nw", pady=2)
        ttk.Label(frame, textvariable=self.profile_path_var, wraplength=220, justify="left").grid(row=2, column=1, sticky="w", pady=2)

        self.profile_combo = ttk.Combobox(frame, textvariable=self.profile_combo_var, state="readonly")
        self.profile_combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        button_row = ttk.Frame(frame)
        button_row.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(button_row, text="Refresh", command=self.refresh_workspace).pack(side="left")
        ttk.Button(button_row, text="Activate", command=self.activate_selected_profile).pack(side="left", padx=(6, 0))
        if self.open_setup_callback is not None:
            ttk.Button(button_row, text="Open Pi Setup", command=self.open_pi_setup).pack(side="left", padx=(6, 0))
        return row + 1

    def _build_preview_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Preview", "Remote assay preview modes from the Pi.")
        ttk.Label(frame, text="Mode").grid(row=1, column=0, sticky="w", pady=2)
        preview_combo = ttk.Combobox(frame, textvariable=self.preview_mode_var, values=self.PREVIEW_MODES, state="readonly")
        preview_combo.grid(row=1, column=1, sticky="ew", pady=2)
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(actions, text="Capture Preview", command=self.capture_preview).pack(side="left")
        ttk.Button(actions, text="Show Image", command=self.load_preview_image_for_current_mode).pack(side="left", padx=(6, 0))
        return row + 1

    def _build_background_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Background", "Capture, import, restore, and rebuild assay background.")
        ttk.Label(frame, text="Current").grid(row=1, column=0, sticky="nw", pady=2)
        ttk.Label(frame, textvariable=self.background_var, wraplength=220, justify="left").grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(frame, text="Previous").grid(row=2, column=0, sticky="nw", pady=2)
        ttk.Label(frame, textvariable=self.previous_background_var, wraplength=220, justify="left").grid(row=2, column=1, sticky="w", pady=2)
        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(actions, text="Capture", command=self.capture_background).pack(side="left")
        ttk.Button(actions, text="Import", command=self.import_background).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Restore", command=self.restore_background).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Rebuild", command=self.rebuild_background).pack(side="left", padx=(6, 0))
        return row + 1

    def _build_calibration_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Calibration", "Load, edit, save, and test assay calibration JSON.")
        ttk.Label(frame, text="Calibration").grid(row=1, column=0, sticky="nw", pady=2)
        ttk.Label(frame, textvariable=self.calibration_path_var, wraplength=220, justify="left").grid(row=1, column=1, sticky="w", pady=2)
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        ttk.Button(actions, text="Load", command=self.load_calibration).pack(side="left")
        ttk.Button(actions, text="Save", command=self.save_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Test", command=self.test_calibration).pack(side="left", padx=(6, 0))

        self.calibration_text = scrolledtext.ScrolledText(frame, height=7, wrap=tk.WORD, font=("Consolas", 8))
        self.calibration_text.grid(row=3, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(3, weight=1)
        return row + 1

    def _build_recording_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Run", "Run the Integrated3 assay workflow remotely.")
        ttk.Button(frame, text="Run Assay", command=self.run_assay).grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Label(frame, textvariable=self.last_run_var, wraplength=300, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        return row + 1

    def _build_processing_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Processing", "Process the latest run, a selected run, or a folder.")
        actions = ttk.Frame(frame)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="Process Last", command=self.process_last).pack(side="left")
        ttk.Button(actions, text="Process Selected", command=self.process_selected).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Batch Process", command=self.batch_process).pack(side="left", padx=(6, 0))
        return row + 1

    def _build_upload_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Upload / Export", "Upload the latest run and seed Box template files.")
        actions = ttk.Frame(frame)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="Upload Last", command=self.upload_last).pack(side="left")
        ttk.Button(actions, text="Write Box Templates", command=self.write_box_templates).pack(side="left", padx=(6, 0))
        return row + 1

    def _build_artifact_card(self, parent: ttk.Frame, row: int) -> int:
        frame = self._card(parent, row, "Artifacts", "Fetch the latest manifest, videos, CSVs, PDF, and processing JSON.")
        actions = ttk.Frame(frame)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="Manifest", command=self.load_manifest).pack(side="left")
        ttk.Button(actions, text="Annotated Video", command=lambda: self.fetch_artifact("annotated_video")).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Raw Video", command=lambda: self.fetch_artifact("raw_video")).pack(side="left", padx=(6, 0))
        second_row = ttk.Frame(frame)
        second_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(second_row, text="Mask Video", command=lambda: self.fetch_artifact("mask_video")).pack(side="left")
        ttk.Button(second_row, text="Per-Vial CSV", command=lambda: self.fetch_artifact("per_vial_summary_csv")).pack(side="left", padx=(6, 0))
        ttk.Button(second_row, text="Per-Fly CSV", command=lambda: self.fetch_artifact("per_fly_summary_csv")).pack(side="left", padx=(6, 0))
        third_row = ttk.Frame(frame)
        third_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(third_row, text="Report PDF", command=lambda: self.fetch_artifact("report_pdf")).pack(side="left")
        ttk.Button(third_row, text="Processing JSON", command=lambda: self.fetch_artifact("processing_json")).pack(side="left", padx=(6, 0))
        return row + 1

    def _build_preview_panel(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Preview / Work Area", font=("Arial", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.preview_info_var, foreground="#4b5563").grid(row=1, column=0, sticky="w")
        ttk.Label(header, textvariable=self.workflow_guidance_var, foreground="#374151", wraplength=1100, justify="left").grid(
            row=2, column=0, sticky="ew", pady=(3, 0)
        )

        preview_frame = ttk.Frame(parent, relief="sunken")
        preview_frame.grid(row=1, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview_label = tk.Label(
            preview_frame,
            text="No assay preview loaded.",
            bg="#111827",
            fg="white",
            anchor="center",
            justify="center",
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew")
        self.preview_label.bind("<Configure>", self._refresh_preview_image)

        log_frame = ttk.LabelFrame(parent, text="Action Log / Debug", padding=4)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.workspace_log = scrolledtext.ScrolledText(log_frame, height=7, wrap=tk.WORD, font=("Consolas", 8))
        self.workspace_log.grid(row=0, column=0, sticky="nsew")

    def _build_results_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Results / Status", font=("Arial", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(parent, textvariable=self.results_status_var, foreground="#4b5563", wraplength=340, justify="left").grid(
            row=1, column=0, sticky="ew", pady=(0, 6)
        )

        self.results_text = scrolledtext.ScrolledText(parent, height=18, wrap=tk.WORD, font=("Consolas", 8))
        self.results_text.grid(row=2, column=0, sticky="nsew")

        parent.rowconfigure(2, weight=1)

    def enter_workspace(self, *, setup_required: bool = False) -> None:
        self.preview_mode_var.set("raw")
        self.preview_info_var.set("Opening Pi-side assay camera preview...")
        if not setup_required:
            self._setup_prompt_shown = False
        self.refresh_workspace()
        if setup_required:
            self.after(250, self.enter_setup_flow)
        elif self.connected:
            self.after(500, self.capture_preview)

    def enter_setup_flow(self) -> None:
        self.preview_mode_var.set("calibration")
        self.preview_info_var.set("Assay setup required before running assays.")
        self._set_workspace_status("Assay Setup is not complete. Opening calibration/config controls.")
        if not self._setup_prompt_shown:
            self._setup_prompt_shown = True
            messagebox.showinfo(
                "Assay Setup Required",
                "No saved assay setup was found. Press OK to open Calibration / Config.\n\n"
                "Complete the assay background and calibration before running the assay.",
                parent=self.winfo_toplevel(),
            )
        self.open_calibration_window()

    def _request_exit(self) -> None:
        if self.can_exit_callback is not None and not self.can_exit_callback():
            messagebox.showwarning(
                "Assay Locked",
                "Assay is locked during the active automated flow. Use Emergency Stop if motion must be stopped.",
                parent=self.winfo_toplevel(),
            )
            return
        self.stop_playback()
        self.on_back()

    def _focus_toplevel(self, window: tk.Toplevel) -> None:
        try:
            window.update_idletasks()
            window.deiconify()
            window.lift()
            window.focus_force()
            window.attributes("-topmost", True)
            window.after(350, lambda: window.attributes("-topmost", False))
        except tk.TclError:
            pass

    def open_calibration_window(self) -> None:
        if self.calibration_window is not None and self.calibration_window.winfo_exists():
            self._focus_toplevel(self.calibration_window)
            return

        self.calibration_window = tk.Toplevel(self.winfo_toplevel())
        self.calibration_window.title("Assay Calibration / Config")
        self.calibration_window.geometry("1180x760")
        self.calibration_window.minsize(900, 620)
        self.calibration_window.columnconfigure(0, weight=1)
        self.calibration_window.rowconfigure(1, weight=1)
        self.calibration_window.protocol("WM_DELETE_WINDOW", self._close_calibration_window)

        guidance = ttk.Label(
            self.calibration_window,
            textvariable=self.calibration_instruction_var,
            wraplength=1120,
            justify="left",
            foreground="#374151",
            padding=(10, 8),
        )
        guidance.grid(row=0, column=0, sticky="ew")

        shell = ttk.Frame(self.calibration_window, padding=8)
        shell.grid(row=1, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)
        self._build_calibration_window_controls(shell)
        self._focus_toplevel(self.calibration_window)
        self.refresh_workspace()
        self.after(300, self._enter_guided_calibration_flow)

    def _build_calibration_window_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=320)
        parent.rowconfigure(0, weight=1)

        image_frame = ttk.LabelFrame(parent, text="Assay background and tube regions", padding=6)
        image_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)

        self.calibration_canvas = tk.Canvas(
            image_frame,
            background="#111827",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.calibration_canvas.grid(row=0, column=0, sticky="nsew")
        self.calibration_canvas.bind("<Configure>", self._refresh_calibration_canvas)
        self.calibration_canvas.bind("<ButtonPress-1>", self._on_calibration_drag_start)
        self.calibration_canvas.bind("<B1-Motion>", self._on_calibration_drag_motion)
        self.calibration_canvas.bind("<ButtonRelease-1>", self._on_calibration_drag_end)

        step_panel = ttk.LabelFrame(parent, text="Guided setup", padding=10)
        step_panel.grid(row=0, column=1, sticky="nsew")
        step_panel.columnconfigure(0, weight=1)

        ttk.Label(step_panel, textvariable=self.calibration_step_var, font=("Arial", 11, "bold")).grid(
            row=0, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Label(
            step_panel,
            textvariable=self.calibration_region_summary_var,
            foreground="#374151",
            wraplength=290,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            step_panel,
            textvariable=self.calibration_ready_var,
            foreground="#4b5563",
            wraplength=290,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(
            step_panel,
            textvariable=self.calibration_gate_var,
            foreground="#7c2d12",
            wraplength=290,
            justify="left",
        ).grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.calibration_buttons = {}
        ttk.Label(
            step_panel,
            text="1) Draw tube boxes on the image",
            font=("Arial", 10, "bold"),
        ).grid(row=4, column=0, sticky="ew", pady=(2, 3))
        for row, (key, label, command) in enumerate(
            (
                ("retake", "(Optional) Retake Clean Background", self.capture_guided_background),
                ("undo", "Undo last box", self.undo_calibration_region),
                ("clear", "Clear boxes", self.clear_calibration_regions),
            ),
            start=5,
        ):
            button = ttk.Button(step_panel, text=label, command=command)
            button.grid(row=row, column=0, sticky="ew", pady=3)
            self.calibration_buttons[key] = button

        ttk.Separator(step_panel).grid(row=8, column=0, sticky="ew", pady=(8, 8))
        for row, (key, label, command) in enumerate(
            (
                ("save", "2) Save Calibration", self.save_guided_calibration),
                ("test", "3) Test Calibration", self.test_guided_calibration),
            ),
            start=9,
        ):
            button = ttk.Button(step_panel, text=label, command=command)
            button.grid(row=row, column=0, sticky="ew", pady=3)
            self.calibration_buttons[key] = button

        ttk.Label(step_panel, text="Secondary shortcut", foreground="#6b7280").grid(
            row=11, column=0, sticky="w", pady=(10, 2)
        )
        load_button = ttk.Button(step_panel, text="Reuse old calibration", command=self.load_calibration)
        load_button.grid(row=12, column=0, sticky="ew", pady=3)
        self.calibration_buttons["load"] = load_button

        ttk.Separator(step_panel).grid(row=13, column=0, sticky="ew", pady=(8, 8))
        continue_button = ttk.Button(step_panel, text="4) Continue to Assay", command=self.continue_from_calibration)
        continue_button.grid(row=14, column=0, sticky="ew", pady=3)
        self.calibration_buttons["continue"] = continue_button

        help_text = (
            "How to calibrate:\n"
            "1) A clean assay background loads or captures automatically.\n"
            "2) Draw one box around each visible assay tube. After the first box, later boxes reuse the first box's top/bottom and only take the width you drag.\n"
            "3) Save Calibration.\n"
            "4) Test Calibration and confirm the overlay looks right.\n"
            "5) Continue to the assay workspace.\n\n"
            "Debug-only controls like preview modes and Pi setup launch are in the Debug window."
        )
        ttk.Label(step_panel, text=help_text, foreground="#374151", wraplength=290, justify="left").grid(
            row=15, column=0, sticky="ew", pady=(14, 0)
        )
        step_panel.rowconfigure(16, weight=1)
        self._update_calibration_workflow_state()

    def _close_calibration_window(self) -> None:
        if self.calibration_window is not None and self.calibration_window.winfo_exists():
            self.calibration_window.destroy()
        self.calibration_window = None
        self.calibration_text = None
        self.calibration_canvas = None

    def _assay_setup_ready(self) -> bool:
        return bool(self.background_var.get().strip()) and bool(self.calibration_path_var.get().strip())

    def _update_workflow_button_states(self) -> None:
        if not self.workflow_buttons:
            return
        setup_ready = self._assay_setup_ready()
        has_run = bool(self.last_run_var.get().strip())
        state_by_key = {
            "calibration": tk.NORMAL,
            "capture": tk.NORMAL if self.connected else tk.DISABLED,
            "run": tk.NORMAL if self.connected and setup_ready else tk.DISABLED,
            "process": tk.NORMAL if self.connected and has_run else tk.DISABLED,
            "export": tk.NORMAL if self.connected and has_run else tk.DISABLED,
        }
        for key, button in self.workflow_buttons.items():
            if button.winfo_exists():
                button.configure(state=state_by_key.get(key, tk.NORMAL))

    def _enter_guided_calibration_flow(self) -> None:
        if self.calibration_window is None or not self.calibration_window.winfo_exists():
            return
        if self.background_var.get().strip():
            self.load_guided_background()
            return
        self.capture_guided_background(auto=True)

    def capture_guided_background(self, *, auto: bool = False) -> None:
        if self._guided_calibration_busy:
            return
        label = "Auto-capturing assay background" if auto else "Retaking assay background"
        self._guided_calibration_busy = True
        self._guided_calibration_busy_message = f"{label}..."
        self._calibration_feedback_override = self._guided_calibration_busy_message
        self._update_calibration_workflow_state()

        def on_success(_payload: dict[str, Any]) -> None:
            self._guided_calibration_saved = False
            self._guided_calibration_tested = False
            self._guided_calibration_busy_message = "Background captured. Loading image..."
            self._calibration_feedback_override = self._guided_calibration_busy_message
            self.refresh_workspace()
            self.load_guided_background()

        self._run_async(label, lambda: self._require_controller().capture_assay_background(), on_success)

    def load_guided_background(self) -> None:
        self._guided_calibration_busy = True
        self._guided_calibration_busy_message = "Loading assay background image..."
        self._calibration_feedback_override = self._guided_calibration_busy_message
        self._update_calibration_workflow_state()

        def on_success(image_bytes: bytes | None) -> None:
            if not image_bytes:
                self._guided_calibration_busy = False
                self._guided_calibration_busy_message = ""
                self._calibration_feedback_override = "Background load failed: no image bytes returned."
                self.calibration_instruction_var.set(
                    "No assay background image is available. Use Retake clean background."
                )
                self._update_calibration_workflow_state()
                return
            self._set_calibration_image_from_bytes(image_bytes)
            self._guided_calibration_busy = False
            self._guided_calibration_busy_message = ""
            self.preview_info_var.set("Showing assay background for calibration.")
            self._calibration_feedback_override = "Background loaded. Drag boxes around the tubes on the image."
            self._update_calibration_workflow_state()

        self._run_async(
            "Loading assay background for calibration",
            lambda: self._require_controller().get_assay_background_image("current"),
            on_success,
        )

    def _set_calibration_image_from_bytes(self, image_bytes: bytes) -> None:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required to display assay calibration images.") from exc
        import io

        self._calibration_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        self._set_preview_from_bytes(image_bytes)
        self._refresh_calibration_canvas()

    def _refresh_calibration_canvas(self, _event=None) -> None:
        canvas = self.calibration_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 640)
        height = max(canvas.winfo_height(), 420)
        if self._calibration_image is None:
            canvas.create_text(
                width // 2,
                height // 2,
                text="Assay background will appear here.\nA clean background is captured automatically when setup starts.",
                fill="white",
                justify="center",
                font=("Arial", 12, "bold"),
            )
            self._calibration_display_box = None
            return
        try:
            from PIL import ImageTk
        except ImportError:
            return

        source_w, source_h = self._calibration_image.size
        scale = min(max(1, width - 20) / source_w, max(1, height - 20) / source_h)
        draw_w = max(1, int(source_w * scale))
        draw_h = max(1, int(source_h * scale))
        offset_x = (width - draw_w) // 2
        offset_y = (height - draw_h) // 2
        display = self._calibration_image.resize((draw_w, draw_h))
        self._calibration_photo = ImageTk.PhotoImage(display)
        canvas.create_image(offset_x, offset_y, image=self._calibration_photo, anchor="nw")
        self._calibration_display_box = (offset_x, offset_y, draw_w, draw_h, scale)

        for index, region in enumerate(self._calibration_regions, start=1):
            self._draw_calibration_region(canvas, region, index, "#22c55e")
        if self._calibration_draft_region is not None:
            self._draw_calibration_region(canvas, self._calibration_draft_region, None, "#f59e0b")
        if not self._calibration_regions:
            canvas.create_rectangle(offset_x + 12, offset_y + 12, offset_x + draw_w - 12, offset_y + 68, fill="#111827", outline="#f59e0b", stipple="gray25")
            canvas.create_text(
                offset_x + 24,
                offset_y + 24,
                text="Step 1: drag boxes around each tube directly on this image.\nSave Calibration unlocks after the first box is drawn.",
                fill="white",
                anchor="nw",
                justify="left",
                font=("Arial", 11, "bold"),
            )

    def _draw_calibration_region(
        self,
        canvas: tk.Canvas,
        region: dict[str, int],
        index: int | None,
        color: str,
    ) -> None:
        if self._calibration_display_box is None:
            return
        offset_x, offset_y, _draw_w, _draw_h, scale = self._calibration_display_box
        x1 = offset_x + int(region["x"] * scale)
        y1 = offset_y + int(region["y"] * scale)
        x2 = offset_x + int((region["x"] + region["w"]) * scale)
        y2 = offset_y + int((region["y"] + region["h"]) * scale)
        canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=3)
        canvas.create_line((x1 + x2) // 2, y1, (x1 + x2) // 2, y2, fill=color, width=1, dash=(4, 3))
        if index is not None:
            canvas.create_text(
                x1 + 8,
                y1 + 8,
                text=str(index),
                fill="white",
                anchor="nw",
                font=("Arial", 11, "bold"),
            )

    def _canvas_to_calibration_point(self, event: tk.Event) -> tuple[int, int] | None:
        if self._calibration_image is None or self._calibration_display_box is None:
            return None
        offset_x, offset_y, draw_w, draw_h, scale = self._calibration_display_box
        x = int(getattr(event, "x", 0))
        y = int(getattr(event, "y", 0))
        if x < offset_x or y < offset_y or x > offset_x + draw_w or y > offset_y + draw_h:
            return None
        source_w, source_h = self._calibration_image.size
        image_x = min(source_w - 1, max(0, int((x - offset_x) / scale)))
        image_y = min(source_h - 1, max(0, int((y - offset_y) / scale)))
        return image_x, image_y

    def _on_calibration_drag_start(self, event: tk.Event) -> None:
        point = self._canvas_to_calibration_point(event)
        if point is None:
            return
        self._calibration_drag_start = point
        self._calibration_draft_region = None

    def _on_calibration_drag_motion(self, event: tk.Event) -> None:
        if self._calibration_drag_start is None:
            return
        point = self._canvas_to_calibration_point(event)
        if point is None:
            return
            self._calibration_draft_region = self._region_from_points(self._calibration_drag_start, point)
        self._refresh_calibration_canvas()

    def _on_calibration_drag_end(self, event: tk.Event) -> None:
        if self._calibration_drag_start is None:
            return
        point = self._canvas_to_calibration_point(event)
        start = self._calibration_drag_start
        self._calibration_drag_start = None
        self._calibration_draft_region = None
        if point is None:
            self._refresh_calibration_canvas()
            return
        region = self._region_from_points(start, point)
        if region["w"] < 10 or region["h"] < 10:
            self._refresh_calibration_canvas()
            return
        self._calibration_regions.append(region)
        self._calibration_regions.sort(key=lambda item: (item["x"], item["y"]))
        self._guided_calibration_saved = False
        self._guided_calibration_tested = False
        self._calibration_feedback_override = None
        self._refresh_calibration_canvas()
        self._update_calibration_workflow_state()

    def _region_from_points(self, start: tuple[int, int], end: tuple[int, int]) -> dict[str, int]:
        x1, y1 = start
        x2, y2 = end
        x = min(x1, x2)
        w = abs(x2 - x1)
        if self._calibration_regions:
            first = self._calibration_regions[0]
            return {"x": x, "y": int(first["y"]), "w": w, "h": int(first["h"])}
        y = min(y1, y2)
        return {"x": x, "y": y, "w": w, "h": abs(y2 - y1)}

    def undo_calibration_region(self) -> None:
        if self._calibration_regions:
            self._calibration_regions.pop()
            self._guided_calibration_saved = False
            self._guided_calibration_tested = False
            self._calibration_feedback_override = None
            self._refresh_calibration_canvas()
            self._update_calibration_workflow_state()

    def clear_calibration_regions(self) -> None:
        self._calibration_regions.clear()
        self._guided_calibration_saved = False
        self._guided_calibration_tested = False
        self._calibration_feedback_override = None
        self._refresh_calibration_canvas()
        self._update_calibration_workflow_state()

    def _build_guided_calibration_payload(self) -> dict[str, Any]:
        if self._calibration_image is None:
            raise ValueError("Load or capture an assay background before saving calibration.")
        if not self._calibration_regions:
            raise ValueError("Draw at least one tube box before saving calibration.")
        width, height = self._calibration_image.size
        vials: list[dict[str, Any]] = []
        for index, region in enumerate(sorted(self._calibration_regions, key=lambda item: (item["x"], item["y"])), start=1):
            center_x = int(region["x"] + region["w"] // 2)
            vials.append(
                {
                    "physical_index": index,
                    "assay_index": index,
                    "enabled": True,
                    "roi_xywh": [int(region["x"]), int(region["y"]), int(region["w"]), int(region["h"])],
                    "top_point_px": [center_x, int(region["y"])],
                    "baseline_point_px": [center_x, int(region["y"] + region["h"] - 1)],
                    "quad_points_px": None,
                    "tube_height_mm": None,
                    "tube_width_mm": None,
                    "label": f"Vial {index}",
                    "group_id": None,
                }
            )
        return {
            "schema_version": 4,
            "background_path": self.background_var.get().strip() or None,
            "image_shape_hw": [int(height), int(width)],
            "ignored_physical_indices": [],
            "editor_mode": "host_remote_rectangles",
            "editor_meta": {"source": "remote_assay_workspace", "tool": "guided_rectangles"},
            "vials": vials,
        }

    def save_guided_calibration(self) -> None:
        calibration = self._build_guided_calibration_payload()
        self._guided_calibration_saved = False
        self._guided_calibration_tested = False
        self._guided_calibration_busy = True
        self._guided_calibration_busy_message = "Saving calibration..."
        self._calibration_feedback_override = "Saving calibration..."
        self._update_calibration_workflow_state()

        def on_success(payload: dict[str, Any]) -> None:
            self.calibration_path_var.set(str(payload.get("calibration_path", "") or self.calibration_path_var.get()))
            self._guided_calibration_saved = True
            self._guided_calibration_tested = False
            self._guided_calibration_busy = False
            self._guided_calibration_busy_message = ""
            self._calibration_feedback_override = "Calibration saved. Next: Test Calibration."
            self.refresh_workspace()
            self._update_calibration_workflow_state()

        self._run_async(
            "Saving assay calibration",
            lambda: self._require_controller().save_assay_calibration(calibration),
            on_success,
        )

    def test_guided_calibration(self) -> None:
        if not self._guided_calibration_saved:
            messagebox.showwarning(
                "Save Required",
                "Save Calibration before testing.",
                parent=self.calibration_window or self.winfo_toplevel(),
            )
            return
        calibration = self._build_guided_calibration_payload()
        self._guided_calibration_tested = False
        self._guided_calibration_busy = True
        self._guided_calibration_busy_message = "Testing calibration overlay..."
        self._calibration_feedback_override = "Testing calibration..."
        self._update_calibration_workflow_state()

        def worker() -> dict[str, Any]:
            controller = self._require_controller()
            payload = controller.test_assay_calibration(calibration)
            mode = str(payload.get("mode", "") or "calibration").strip().lower()
            image_bytes = controller.get_assay_preview_image(mode)
            if not image_bytes and mode != "calibration":
                mode = "calibration"
                image_bytes = controller.get_assay_preview_image(mode)
            if not image_bytes:
                raise RuntimeError("Calibration test finished, but no tested preview image was returned by the Pi.")
            return {"payload": payload, "mode": mode, "image_bytes": image_bytes}

        def on_success(result: dict[str, Any]) -> None:
            self._guided_calibration_tested = True
            self._guided_calibration_busy = False
            self._guided_calibration_busy_message = ""
            mode = str(result.get("mode", "") or "calibration")
            image_bytes = result.get("image_bytes")
            if not isinstance(image_bytes, (bytes, bytearray)):
                raise RuntimeError("Calibration test did not return displayable preview bytes.")
            self.preview_mode_var.set(mode)
            self._set_calibration_image_from_bytes(bytes(image_bytes))
            self.preview_info_var.set(f"Showing tested assay calibration preview ({mode}).")
            payload = result.get("payload", {}) or {}
            self._calibration_feedback_override = (
                f"Calibration test passed. Mode={mode}. "
                f"Backend message={payload.get('message', 'ok')}. Continue to Assay is now enabled."
            )
            self._update_calibration_workflow_state()

        self._run_async(
            "Testing assay calibration",
            worker,
            on_success,
        )

    def continue_from_calibration(self) -> None:
        if not self._guided_calibration_saved or not self._guided_calibration_tested:
            messagebox.showwarning(
                "Calibration Not Ready",
                "Save Calibration and Test Calibration must both complete before continuing to the assay workspace.",
                parent=self.calibration_window or self.winfo_toplevel(),
            )
            return
        self._close_calibration_window()
        self._set_workspace_status("Assay calibration complete. Run assay recording is now available.")
        self._update_workflow_button_states()

    def _load_regions_from_calibration_payload(self, calibration: dict[str, Any]) -> None:
        regions: list[dict[str, int]] = []
        for vial in calibration.get("vials", []) or []:
            roi = vial.get("roi_xywh")
            if isinstance(roi, list) and len(roi) >= 4:
                try:
                    x, y, w, h = (int(round(float(value))) for value in roi[:4])
                except (TypeError, ValueError):
                    continue
                if w > 0 and h > 0:
                    regions.append({"x": x, "y": y, "w": w, "h": h})
        self._calibration_regions = sorted(regions, key=lambda item: (item["x"], item["y"]))
        self._guided_calibration_saved = bool(regions)
        self._guided_calibration_tested = False
        self._calibration_feedback_override = (
            f"Loaded existing calibration with {len(regions)} region{'s' if len(regions) != 1 else ''}. "
            "Test Calibration is required before continuing."
            if regions
            else "Loaded calibration, but no tube regions were found."
        )
        self._refresh_calibration_canvas()
        self._update_calibration_workflow_state()

    def _update_calibration_workflow_state(self) -> None:
        has_image = self._calibration_image is not None
        region_count = len(self._calibration_regions)
        ready_to_continue = bool(
            has_image
            and region_count
            and self._guided_calibration_saved
            and self._guided_calibration_tested
        )
        self.calibration_region_summary_var.set(
            f"{region_count} tube region{'s' if region_count != 1 else ''} defined."
            if region_count
            else "No tube regions defined yet."
        )
        if self._guided_calibration_busy:
            self.calibration_step_var.set("Working...")
            self.calibration_instruction_var.set(self._guided_calibration_busy_message or "Working on assay calibration.")
            self.calibration_ready_var.set(self._guided_calibration_busy_message or "Working...")
        elif not has_image:
            self.calibration_step_var.set("Step 1: capture clean assay background")
            self.calibration_instruction_var.set(
                "Assay setup starts by loading or capturing a clean background image. "
                "If none exists, it is captured automatically."
            )
            self.calibration_ready_var.set("Waiting for assay background.")
        elif region_count == 0:
            self.calibration_step_var.set("Step 1: draw tube boxes")
            self.calibration_instruction_var.set(
                "The background is loaded. Drag the first box around a full tube. Later boxes reuse that first "
                "box's top/bottom and only use the horizontal width you drag. Save/Test stay disabled until at "
                "least one box is drawn."
            )
            self.calibration_ready_var.set("Blocked: draw at least one tube box on the image before saving.")
        elif not self._guided_calibration_saved:
            self.calibration_step_var.set("Step 2: save calibration")
            self.calibration_instruction_var.set(
                "Review the green tube boxes. Use Undo/Clear if needed, then Save Calibration."
            )
            self.calibration_ready_var.set("Blocked: calibration regions are drawn but not saved.")
        elif not self._guided_calibration_tested:
            self.calibration_step_var.set("Step 3: test calibration")
            self.calibration_instruction_var.set(
                "Calibration saved. Use Test Calibration to render and verify the overlay before continuing."
            )
            self.calibration_ready_var.set("Blocked: calibration saved but not tested.")
        else:
            self.calibration_step_var.set("Step 4: continue to assay")
            self.calibration_instruction_var.set(
                "Calibration was saved and tested successfully. Continue to the main assay workflow."
            )
            self.calibration_ready_var.set("Ready: Continue to Assay is enabled.")

        idle = not self._guided_calibration_busy
        state_by_key = {
            "retake": tk.NORMAL if self.connected and idle else tk.DISABLED,
            "load": tk.NORMAL if self.connected and idle else tk.DISABLED,
            "undo": tk.NORMAL if region_count and idle else tk.DISABLED,
            "clear": tk.NORMAL if region_count and idle else tk.DISABLED,
            "save": tk.NORMAL if self.connected and has_image and region_count and idle else tk.DISABLED,
            "test": tk.NORMAL if self.connected and has_image and region_count and self._guided_calibration_saved and idle else tk.DISABLED,
            "continue": tk.NORMAL if ready_to_continue and idle else tk.DISABLED,
        }
        for key, button in self.calibration_buttons.items():
            if button.winfo_exists():
                button.configure(state=state_by_key.get(key, tk.NORMAL))
        if self._calibration_feedback_override:
            self.calibration_ready_var.set(self._calibration_feedback_override)
        self.calibration_gate_var.set(
            "Gate: "
            f"background={'yes' if has_image else 'no'} "
            f"regions={region_count} "
            f"saved={'yes' if self._guided_calibration_saved else 'no'} "
            f"tested={'yes' if self._guided_calibration_tested else 'no'} "
            f"busy={'yes' if self._guided_calibration_busy else 'no'} "
            f"continue={'yes' if ready_to_continue and idle else 'no'}"
        )
        self._update_workflow_button_states()

    def open_pi_setup(self) -> None:
        self._append_log("Started: Opening Pi setup GUI.")
        if self.open_setup_callback is None:
            self._append_log("Opening Pi setup GUI failed: setup callback is unavailable.")
            return
        try:
            result = self.open_setup_callback()
        except Exception as exc:
            self._append_log(f"Opening Pi setup GUI failed: {exc}")
            messagebox.showerror("Assay Setup Error", str(exc), parent=self.winfo_toplevel())
            return
        self._append_log(f"Opening Pi setup GUI requested: {result}")

    def open_debug_menu(self) -> None:
        if self.debug_window is not None and self.debug_window.winfo_exists():
            self._focus_toplevel(self.debug_window)
            return
        self.debug_window = tk.Toplevel(self.winfo_toplevel())
        self.debug_window.title("Assay Debug")
        self.debug_window.geometry("460x760")
        self.debug_window.minsize(420, 520)
        self.debug_window.columnconfigure(0, weight=1)
        self.debug_window.rowconfigure(0, weight=1)
        self.debug_window.protocol("WM_DELETE_WINDOW", self._close_debug_menu)

        shell = ttk.Frame(self.debug_window, padding=8)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)
        self._build_scrollable_controls(shell)
        self._focus_toplevel(self.debug_window)
        self.refresh_workspace()

    def _close_debug_menu(self) -> None:
        if self.debug_window is not None and self.debug_window.winfo_exists():
            self.debug_window.destroy()
        self.debug_window = None

    def toggle_results(self) -> None:
        self.results_visible = not self.results_visible
        self._apply_panel_visibility()

    def _apply_panel_visibility(self) -> None:
        if self.results_visible:
            self.body.columnconfigure(1, weight=0, minsize=300)
            self.right_panel.grid()
        else:
            self.right_panel.grid_remove()
            self.body.columnconfigure(1, weight=0, minsize=0)

    def update_connection_state(self, *, connected: bool, message: str, remote_busy: bool, assay_available: bool) -> None:
        self.connected = bool(connected)
        self.remote_busy = bool(remote_busy)
        self.assay_available = bool(assay_available)
        state_text = str(message or "Disconnected")
        if self.connected and self.remote_busy:
            state_text = f"{state_text} | busy"
        elif self.connected:
            state_text = f"{state_text} | connected"
        color = "#4CAF50" if self.connected else "#FF9800"
        if self.connected and not self.assay_available:
            color = "#F44336"
            state_text = f"{state_text} | assay unavailable"
        self.connection_var.set(state_text)
        self.connection_label.configure(bg=color)
        self._update_workflow_button_states()
        self._update_calibration_workflow_state()

    def _append_log(self, message: str) -> None:
        if getattr(self, "workspace_log", None) is not None and self.workspace_log.winfo_exists():
            self.workspace_log.insert(tk.END, message + "\n")
            self.workspace_log.see(tk.END)
        self.log_callback(message)

    def _set_workspace_status(self, text: str) -> None:
        self.workspace_status_var.set(text)
        self.results_status_var.set(text)

    def _require_controller(self):
        controller = self.get_controller()
        if controller is None or not self.connected:
            raise RuntimeError("Connect to the Pi backend before using the assay workspace.")
        return controller

    def _run_async(self, label: str, worker: Callable[[], Any], on_success: Callable[[Any], None] | None = None) -> None:
        self._worker_count += 1
        self._set_workspace_status(label)
        self._append_log(f"Started: {label}")
        self.status_callback("assaying", label)

        def _task() -> None:
            try:
                result = worker()
            except Exception as exc:
                self.after(0, lambda: self._handle_error(label, exc))
                return
            self.after(0, lambda: self._handle_success(label, result, on_success))

        threading.Thread(target=_task, daemon=True).start()

    def _handle_error(self, label: str, exc: Exception) -> None:
        self._worker_count = max(0, self._worker_count - 1)
        self._guided_calibration_busy = False
        self._guided_calibration_busy_message = ""
        message = f"{label} failed: {exc}"
        if "calibration" in label.lower() or "background" in label.lower():
            self._calibration_feedback_override = message
        self._append_log(message)
        self.status_callback("error", message)
        self._set_workspace_status(message)
        self._update_calibration_workflow_state()
        messagebox.showerror("Assay Workspace Error", str(exc), parent=self.winfo_toplevel())

    def _handle_success(self, label: str, result: Any, on_success: Callable[[Any], None] | None) -> None:
        try:
            self._append_log(f"{label} completed.")
            if on_success is not None:
                on_success(result)
        except Exception as exc:
            self._handle_error(label, exc)
            return
        self._worker_count = max(0, self._worker_count - 1)
        self.status_callback("running", f"{label} completed.")

    def refresh_workspace(self) -> None:
        def worker() -> dict[str, Any]:
            controller = self._require_controller()
            manifest: dict[str, Any] = {}
            try:
                manifest = controller.get_latest_assay_manifest()
            except Exception:
                manifest = {}
            return {
                "status": controller.get_assay_status(),
                "summary": controller.get_assay_profile_summary(),
                "profiles": controller.get_assay_profiles(),
                "manifest": manifest,
            }

        def on_success(payload: dict[str, Any]) -> None:
            self._apply_status_payload(payload.get("status", {}))
            self._apply_profiles_payload(payload.get("profiles", {}))
            manifest = payload.get("manifest", {})
            if manifest.get("ok", True):
                self._apply_manifest_payload(manifest)
            self._apply_summary_payload(payload.get("summary", {}))
            if self.background_var.get() and self.preview_mode_var.get().strip().lower() != "raw":
                self.load_preview_image("background")

        self._run_async("Refreshing assay workspace", worker, on_success)

    def _apply_status_payload(self, payload: dict[str, Any]) -> None:
        self._last_status_payload = dict(payload or {})
        self.active_profile_var.set(str(payload.get("profile", "") or ""))
        self.profile_path_var.set(str(payload.get("profile_path", "") or ""))
        self.background_var.set(str(payload.get("background_preview", "") or ""))
        self.previous_background_var.set(str(payload.get("background_previous", "") or ""))
        self.calibration_path_var.set(str(payload.get("calibration_path", "") or ""))
        self.last_run_var.set(str(payload.get("last_run_dir", "") or ""))
        if getattr(self, "results_text", None) is not None and self.results_text.winfo_exists():
            self.results_text.delete("1.0", tk.END)
            self.results_text.insert(tk.END, json.dumps(payload, indent=2, sort_keys=True))
        self._update_workflow_button_states()
        self._update_calibration_workflow_state()

    def _apply_summary_payload(self, payload: dict[str, Any]) -> None:
        if payload:
            self._append_log(f"Profile summary loaded for {payload.get('name', '')}.")

    def _apply_profiles_payload(self, payload: dict[str, Any]) -> None:
        profiles = list(payload.get("profiles", []) or [])
        if self.profile_combo is not None and self.profile_combo.winfo_exists():
            self.profile_combo.configure(values=profiles)
        active = str(payload.get("active_profile", "") or "")
        self.profile_combo_var.set(active or (profiles[0] if profiles else ""))

    def _apply_manifest_payload(self, payload: dict[str, Any]) -> None:
        self._last_manifest_payload = dict(payload or {})
        run_dir = str(payload.get("run_dir", "") or "")
        if run_dir:
            self.last_run_var.set(run_dir)
        manifest = payload.get("manifest", {}) or {}
        if getattr(self, "results_text", None) is not None and self.results_text.winfo_exists():
            self.results_text.delete("1.0", tk.END)
            self.results_text.insert(tk.END, json.dumps(manifest, indent=2, sort_keys=True))
        self.results_status_var.set(f"Latest assay run: {Path(run_dir).name}" if run_dir else "No assay run loaded.")
        self._update_workflow_button_states()

    def activate_selected_profile(self) -> None:
        profile_name = self.profile_combo_var.get().strip()
        if not profile_name:
            return
        self._run_async(
            f"Activating assay profile {profile_name}",
            lambda: self._require_controller().activate_assay_profile(profile_name),
            lambda _payload: self.refresh_workspace(),
        )

    def capture_preview(self) -> None:
        mode = self.preview_mode_var.get().strip() or "calibration"
        self._run_async(
            f"Capturing assay preview ({mode})",
            lambda: self._require_controller().capture_assay_preview(mode=mode),
            lambda _payload: self.load_preview_image(mode),
        )

    def load_preview_image_for_current_mode(self) -> None:
        self.load_preview_image(self.preview_mode_var.get().strip() or "calibration")

    def load_preview_image(self, mode: str) -> None:
        mode_key = str(mode or "calibration").strip().lower()

        def worker() -> tuple[str, bytes | None]:
            controller = self._require_controller()
            if mode_key == "background":
                return mode_key, controller.get_assay_background_image("current")
            return mode_key, controller.get_assay_preview_image(mode_key)

        def on_success(result: tuple[str, bytes | None]) -> None:
            loaded_mode, image_bytes = result
            if not image_bytes:
                self.preview_info_var.set(f"No preview image is available for mode '{loaded_mode}'.")
                return
            self.preview_info_var.set(f"Showing {loaded_mode} preview.")
            self._set_preview_from_bytes(image_bytes)

        self._run_async(f"Loading assay {mode_key} image", worker, on_success)

    def capture_background(self) -> None:
        self._run_async(
            "Capturing assay background",
            lambda: self._require_controller().capture_assay_background(),
            lambda _payload: self._post_background_update("background"),
        )

    def import_background(self) -> None:
        source_path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not source_path:
            return
        source = Path(source_path)
        image_bytes = source.read_bytes()
        self._run_async(
            f"Importing assay background from {source.name}",
            lambda: self._require_controller().import_assay_background(
                image_bytes=image_bytes,
                filename=source.name,
            ),
            lambda _payload: self._post_background_update("background"),
        )

    def restore_background(self) -> None:
        self._run_async(
            "Restoring previous assay background",
            lambda: self._require_controller().restore_previous_assay_background(),
            lambda _payload: self._post_background_update("background"),
        )

    def rebuild_background(self) -> None:
        self._run_async(
            "Rebuilding assay background transform",
            lambda: self._require_controller().rebuild_assay_background(),
            lambda _payload: self._post_background_update("background"),
        )

    def _post_background_update(self, preview_mode: str) -> None:
        self.refresh_workspace()
        self.preview_mode_var.set(preview_mode)

    def load_calibration(self) -> None:
        def on_success(payload: dict[str, Any]) -> None:
            calibration = payload.get("calibration", {})
            if self.calibration_text is not None and self.calibration_text.winfo_exists():
                self.calibration_text.delete("1.0", tk.END)
                self.calibration_text.insert(tk.END, json.dumps(calibration, indent=2, sort_keys=True))
            self.calibration_path_var.set(str(payload.get("calibration_path", "") or ""))
            if isinstance(calibration, dict):
                self._load_regions_from_calibration_payload(calibration)
            self.preview_mode_var.set("calibration")
            self.load_preview_image("background")

        self._run_async("Loading assay calibration", lambda: self._require_controller().get_assay_calibration(), on_success)

    def _current_calibration_payload(self) -> dict[str, Any]:
        if self.calibration_text is None or not self.calibration_text.winfo_exists():
            raise ValueError("Open Calibration / Config before editing assay calibration JSON.")
        raw_text = self.calibration_text.get("1.0", tk.END).strip()
        if not raw_text:
            raise ValueError("Calibration editor is empty.")
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("Calibration JSON must be an object.")
        return payload

    def save_calibration(self) -> None:
        calibration = self._current_calibration_payload()

        def on_success(payload: dict[str, Any]) -> None:
            self.calibration_path_var.set(str(payload.get("calibration_path", "") or ""))
            self.preview_mode_var.set("calibration")
            self.load_preview_image("calibration")
            self.refresh_workspace()

        self._run_async(
            "Saving assay calibration",
            lambda: self._require_controller().save_assay_calibration(calibration),
            on_success,
        )

    def test_calibration(self) -> None:
        calibration = self._current_calibration_payload()
        self._run_async(
            "Testing assay calibration",
            lambda: self._require_controller().test_assay_calibration(calibration),
            lambda _payload: self.load_preview_image("annotated"),
        )

    def run_assay(self) -> None:
        def post_progress(message: str) -> None:
            self.after(
                0,
                lambda text=message: (
                    self._set_workspace_status(text),
                    self.preview_info_var.set(text),
                    self._append_log(text),
                ),
            )

        def worker() -> dict[str, Any]:
            controller = self._require_controller()
            accepted = controller.run_integrated3_assay()
            status = dict(accepted or {})
            if not status.get("accepted", False):
                post_progress(f"Integrated3 assay was not accepted: {status.get('message', 'Command rejected.')}")
                return status
            post_progress(str(status.get("message") or "Integrated3 assay accepted. Waiting for backend progress..."))
            deadline = threading.Event()
            started_at = time.monotonic()
            last_report_at = 0.0
            last_state = status
            while not deadline.wait(0.5):
                polled = controller.get_status_fresh()
                last_state = dict(polled or {})
                current_task = str(polled.get("current_task") or "")
                task_state = str(polled.get("task_state") or "")
                latest_message = str(polled.get("latest_message") or "")
                now = time.monotonic()
                if now - last_report_at >= 2.0:
                    last_report_at = now
                    post_progress(
                        "Integrated3 assay running: "
                        f"task={current_task or '--'} state={task_state or '--'} "
                        f"message={latest_message or '--'}"
                    )
                if current_task in {"assay", "integrated3_assay"} or task_state == "ASSAY_RUNNING":
                    if now - started_at > 240.0:
                        raise RuntimeError(
                            "Timed out waiting for Integrated3 assay to finish. "
                            f"Last state: task={current_task or '--'} state={task_state or '--'} "
                            f"message={latest_message or '--'}"
                        )
                    continue
                if task_state == "ASSAY_ERROR":
                    raise RuntimeError(latest_message or "Integrated3 assay failed.")
                if task_state == "ASSAY_COMPLETE" or current_task == "":
                    post_progress(
                        "Integrated3 assay completed: "
                        f"state={task_state or '--'} message={latest_message or '--'}"
                    )
                    return polled
                if now - started_at > 240.0:
                    raise RuntimeError(
                        "Timed out waiting for Integrated3 assay to finish. "
                        f"Last state: {last_state}"
                    )

        def on_success(_payload: dict[str, Any]) -> None:
            self._set_workspace_status("Integrated3 assay complete. Loading latest results.")
            self.refresh_workspace()
            self.load_manifest()

        self._run_async("Running Integrated3 assay", worker, on_success)

    def process_last(self) -> None:
        self._run_async("Processing latest assay run", lambda: self._require_controller().process_last_assay(), self._on_processing_complete)

    def process_selected(self) -> None:
        run_dir = filedialog.askdirectory(parent=self.winfo_toplevel())
        if not run_dir:
            return
        self._run_async(
            f"Processing selected assay run {Path(run_dir).name}",
            lambda: self._require_controller().process_selected_assay(run_dir),
            self._on_processing_complete,
        )

    def batch_process(self) -> None:
        folder = filedialog.askdirectory(parent=self.winfo_toplevel())
        if not folder:
            return
        self._run_async(
            f"Batch processing assay folder {Path(folder).name}",
            lambda: self._require_controller().batch_process_assay(folder),
            lambda payload: self._append_results_payload(payload),
        )

    def _on_processing_complete(self, payload: dict[str, Any]) -> None:
        if getattr(self, "results_text", None) is not None and self.results_text.winfo_exists():
            self.results_text.delete("1.0", tk.END)
            self.results_text.insert(tk.END, json.dumps(payload, indent=2, sort_keys=True))
        self.results_status_var.set("Assay processing complete.")
        self.load_manifest()

    def upload_last(self) -> None:
        self._run_async(
            "Uploading latest assay run",
            lambda: self._require_controller().upload_last_assay(),
            lambda payload: self._append_results_payload(payload),
        )

    def write_box_templates(self) -> None:
        self._run_async(
            "Writing assay Box templates",
            lambda: self._require_controller().seed_assay_box_templates(overwrite=True),
            lambda payload: self._append_results_payload(payload),
        )

    def _append_results_payload(self, payload: dict[str, Any]) -> None:
        if getattr(self, "results_text", None) is not None and self.results_text.winfo_exists():
            self.results_text.insert(tk.END, "\n\n" + json.dumps(payload, indent=2, sort_keys=True))
        self.results_visible = True
        self._apply_panel_visibility()

    def load_manifest(self) -> None:
        self._run_async("Loading latest assay manifest", lambda: self._require_controller().get_latest_assay_manifest(), self._apply_manifest_payload)

    def fetch_artifact(self, kind: str) -> None:
        kind_key = str(kind).strip().lower()

        def worker() -> tuple[Path, str]:
            return self._download_artifact(kind_key)

        def on_success(result: tuple[Path, str]) -> None:
            path, artifact_kind = result
            if artifact_kind.endswith("json") or artifact_kind.endswith("csv"):
                if getattr(self, "results_text", None) is not None and self.results_text.winfo_exists():
                    self.results_text.delete("1.0", tk.END)
                    self.results_text.insert(tk.END, path.read_text(encoding="utf-8", errors="replace"))
                self.results_status_var.set(f"Loaded {path.name} into results panel.")
                self.results_visible = True
                self._apply_panel_visibility()
            else:
                self._open_file(path)
                self.results_status_var.set(f"Opened {path.name}.")

        self._run_async(f"Fetching assay artifact {kind_key}", worker, on_success)

    def _download_artifact(self, kind_key: str) -> tuple[Path, str]:
        controller = self._require_controller()
        if kind_key == "annotated_video":
            data = controller.get_latest_assay_annotated_video()
            suffix = ".mp4"
        elif kind_key == "raw_video":
            data = controller.get_latest_assay_raw_video()
            suffix = ".mp4"
        elif kind_key == "mask_video":
            data = controller.get_latest_assay_mask_video()
            suffix = ".mp4"
        elif kind_key == "per_vial_summary_csv":
            data = controller.get_latest_assay_per_vial_summary_csv()
            suffix = ".csv"
        elif kind_key == "per_fly_summary_csv":
            data = controller.get_latest_assay_per_fly_summary_csv()
            suffix = ".csv"
        elif kind_key == "report_pdf":
            data = controller.get_latest_assay_report_pdf()
            suffix = ".pdf"
        elif kind_key == "processing_json":
            data = controller.get_latest_assay_processing_json()
            suffix = ".json"
        else:
            raise ValueError(f"Unsupported artifact kind: {kind_key}")
        if not data:
            raise RuntimeError(f"No assay artifact is available for {kind_key}.")
        target = self.cache_dir / f"latest_{kind_key}{suffix}"
        target.write_bytes(data)
        return target, kind_key

    def play_video_artifact(self, kind: str) -> None:
        kind_key = str(kind or "").strip().lower()

        def worker() -> tuple[Path, str]:
            return self._download_artifact(kind_key)

        def on_success(result: tuple[Path, str]) -> None:
            path, artifact_kind = result
            self._start_video_playback(path, artifact_kind)

        label = {
            "raw_video": "raw assay video",
            "annotated_video": "processed assay video",
            "mask_video": "mask assay video",
        }.get(kind_key, kind_key)
        self._run_async(f"Loading {label}", worker, on_success)

    def _start_video_playback(self, path: Path, label: str) -> None:
        self.stop_playback()
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is required to play assay videos in the workspace.") from exc
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise RuntimeError(f"Could not open assay video: {path}")
        self._video_capture = capture
        self._video_paused = False
        self.preview_info_var.set(f"Playing {label}.")
        self._show_next_video_frame()

    def pause_playback(self) -> None:
        if self._video_capture is None:
            return
        self._video_paused = not self._video_paused
        self.preview_info_var.set("Video paused." if self._video_paused else "Video playing.")
        if not self._video_paused:
            self._show_next_video_frame()

    def stop_playback(self) -> None:
        if self._video_after_id is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._video_after_id)
            self._video_after_id = None
        if self._video_capture is not None:
            with contextlib.suppress(Exception):
                self._video_capture.release()
            self._video_capture = None
        self._video_paused = False

    def _show_next_video_frame(self) -> None:
        if self._video_capture is None or self._video_paused:
            return
        ok, frame_bgr = self._video_capture.read()
        if not ok:
            self.stop_playback()
            self.preview_info_var.set("Video playback complete.")
            return
        try:
            import cv2
            from PIL import Image
        except ImportError as exc:
            self.stop_playback()
            raise RuntimeError("OpenCV and Pillow are required to play assay videos.") from exc
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        self._preview_source_image = Image.fromarray(frame_rgb)
        self._refresh_preview_image()
        fps = float(self._video_capture.get(cv2.CAP_PROP_FPS) or 30.0)
        delay_ms = max(15, int(1000.0 / max(1.0, fps)))
        self._video_after_id = self.after(delay_ms, self._show_next_video_frame)

    def _set_preview_from_bytes(self, image_bytes: bytes) -> None:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required to display assay preview images.") from exc
        import io

        self._preview_source_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        self._refresh_preview_image()

    def _refresh_preview_image(self, _event=None) -> None:
        if self._preview_source_image is None:
            return
        try:
            from PIL import ImageTk
        except ImportError:
            return
        width = max(self.preview_label.winfo_width(), 640)
        height = max(self.preview_label.winfo_height(), 480)
        image = self._preview_source_image.copy()
        image.thumbnail((width - 20, height - 20))
        self._preview_photo = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self._preview_photo, text="")

    @staticmethod
    def _open_file(path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        if os.name == "posix":
            subprocess.Popen(["xdg-open", str(path)])
            return
        raise RuntimeError(f"Do not know how to open files on this platform: {os.name}")
