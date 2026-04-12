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
from pathlib import Path
import tkinter as tk
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
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self.toggle)
        self.draw()

    def toggle(self, _event=None):
        if not self.enabled:
            return
        self.value = not self.value
        self.draw()
        if callable(self.command):
            self.command(self.value)

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


class DrosophilaGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._configure_windows_app_id()
        self.root.title("Drosophila Genetics Control Panel")
        self.root.geometry("1100x760")
        self.root.minsize(960, 680)
        self.root.state("zoomed")
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
        self.operations_logo_image = None
        self.footer_banner_images = []
        self.window_icon_images = []
        self.entry_fly_display_image = None
        self.entry_fly_frames = []
        self.entry_fly_job: str | None = None
        self.entry_fly_frame_index = 0
        self.entry_fly_source_image = None
        self.last_preview_mtime: float | None = None
        self.last_result_mtime: float | None = None
        self.last_used_detection_mtime: float | None = None
        self._last_output_dir_text = ""
        self.remote_settings = load_remote_connection_settings(self.repo_root)
        self.remote_controller: RemoteController | None = None
        self.remote_sync: RemoteSyncManager | None = None
        self._local_runtime_cache: dict[str, object] | None = None
        self._local_runtime_error: str | None = None
        self.entry_page_scale = 1.54

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
        self.device_state_labels: dict[str, tk.Label] = {}
        self.device_state_text: dict[str, tk.StringVar] = {}
        self.device_detail_text: dict[str, tk.StringVar] = {}

        self.control_widgets = []
        self.toggle_widgets = []
        self.motion_widgets = []
        self.remote_unsupported_widgets = []
        self.manual_move_entry: ttk.Entry | None = None

        self.create_widgets()
        self._set_window_icon()
        self.set_status("idle", "Ready")
        self.log_message(f"Channel output directory: {self.output_dir_var.get()}")
        self.update_position()
        self.update_channel_preview()
        self.process_queue()
        self._apply_connection_state(ConnectionState.LOCAL, "Local controller active.")

    def _load_local_runtime(self) -> dict[str, object]:
        if self._local_runtime_cache is not None:
            return self._local_runtime_cache

        try:
            motion = importlib.import_module("motion")
            vacuum = importlib.import_module("vacuum")
            vibration = importlib.import_module("vibration")
            assay_module = importlib.import_module("assay")
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
            "assay": getattr(assay_module, "assay"),
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

    def _entry_scale(self, value: int) -> int:
        return max(1, math.ceil(value * self.entry_page_scale))

    def _entry_button_scale(self, value: int) -> int:
        return max(1, round(self._entry_scale(value) * 0.75))

    def _blend_hex(self, start_hex: str, end_hex: str, ratio: float) -> str:
        ratio = max(0.0, min(1.0, ratio))
        start = tuple(int(start_hex[index : index + 2], 16) for index in (1, 3, 5))
        end = tuple(int(end_hex[index : index + 2], 16) for index in (1, 3, 5))
        blended = tuple(
            round(start[channel] + ((end[channel] - start[channel]) * ratio))
            for channel in range(3)
        )
        return f"#{blended[0]:02X}{blended[1]:02X}{blended[2]:02X}"

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
        entry_card.columnconfigure(0, weight=0)
        entry_card.columnconfigure(1, weight=0)

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

        enter_button = tk.Button(
            button_row,
            text="Enter Control Panel",
            bg="#8E2D2B",
            fg="#FFFFFF",
            activebackground="#732220",
            activeforeground="#FFFFFF",
            font=("Arial", self._entry_button_scale(21), "bold"),
            padx=self._entry_button_scale(30),
            pady=self._entry_button_scale(17),
            relief="raised",
            bd=0,
            command=self.show_control_panel,
        )
        enter_button.grid(row=0, column=0, sticky=tk.W, padx=(0, button_gap))

        update_button = tk.Button(
            button_row,
            text="Check for Updates",
            bg="#8E2D2B",
            fg="#FFFFFF",
            activebackground="#732220",
            activeforeground="#FFFFFF",
            font=("Arial", self._entry_button_scale(21), "bold"),
            padx=self._entry_button_scale(30),
            pady=self._entry_button_scale(17),
            relief="raised",
            bd=0,
            command=self.check_for_updates,
        )
        update_button.grid(row=0, column=1, sticky=tk.W)

        fly_panel = tk.Frame(
            entry_card,
            bg="#686766",
            padx=self._entry_scale(10),
            pady=self._entry_scale(7),
        )
        fly_panel.grid(row=0, column=1, sticky=(tk.N, tk.E))

        self.entry_fly_label = tk.Label(
            fly_panel,
            bg="#686766",
            bd=0,
            highlightthickness=0,
        )
        self.entry_fly_label.grid(row=0, column=0)

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
        elif state in {ConnectionState.CLIENT_CONNECTED, ConnectionState.CLIENT_RECONNECTED}:
            label_text = "Remote Mode (Degraded)" if self.remote_backend_degraded else "Remote Mode"
            label_color = "#FF9800" if self.remote_backend_degraded else "#4CAF50"
            connection_color = "#4CAF50"
            self.remote_connected = True
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

        self.mode_var.set(label_text)
        self.mode_label.config(bg=label_color)
        if getattr(self, "connection_label", None) is not None:
            self.connection_label.config(bg=connection_color)
        self._update_control_interactivity()

    def _schedule_remote_home_prompt(self) -> None:
        if (
            not self.is_remote_mode()
            or not self.remote_connected
            or self.remote_home_prompt_scheduled
            or self.remote_controller is None
        ):
            return

        self.remote_home_calibration_required = True
        self.remote_home_prompt_scheduled = True
        self.connection_var.set("Connected. Waiting for home calibration approval.")
        self.set_status("waiting", "Remote calibration must be sent Home before controls unlock.")
        self._update_control_interactivity()
        self.root.after(50, self._prompt_remote_home_calibration)

    def _prompt_remote_home_calibration(self) -> None:
        self.remote_home_prompt_scheduled = False

        if not self.is_remote_mode() or not self.remote_connected or self.remote_controller is None:
            return

        approved = messagebox.askyesno(
            "Remote Calibration",
            "The remote calibration should be sent to Home before use.\n\nSend Home now?",
        )

        if not approved:
            self.remote_home_calibration_required = True
            self.connection_var.set("Connected. Waiting for home calibration approval.")
            self.set_status("waiting", "Waiting for home calibration approval.")
            self._update_control_interactivity()
            return

        self.connection_var.set("Connected. Sending remote home calibration.")
        self.set_status("moving", "Sending remote home calibration.")
        self._start_remote_command(
            "home calibration",
            "moving",
            "Sending remote home calibration.",
            self.remote_controller.home,
            allow_calibration_bypass=True,
        )

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
        self.detection_var.set(self._format_remote_detection(status.get("detection_summary", {}) or {}))

        detection_summary = status.get("detection_summary", {}) or {}
        source_path = detection_summary.get("source_path")
        if source_path:
            self.output_dir_var.set(str(source_path))
        self._request_remote_preview_if_needed(detection_summary)

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
        normalized_result = {
            "class": result.get("result_class", "UNCERTAIN"),
            "confidence": float(result.get("confidence", 0.0)),
            "errors": list(result.get("errors", [])),
        }
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

    def _update_control_interactivity(self) -> None:
        busy = self.local_task_busy or self.remote_request_in_flight or (self.is_remote_mode() and self.remote_backend_busy)
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
            if widget in self.remote_unsupported_widgets and self.is_remote_mode():
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
            stop_enabled = self.remote_connected and self.remote_stop_allowed
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
        source_image = self._load_entry_fly_source_image()
        if source_image is None:
            self.entry_fly_display_image = None
            self.entry_fly_label.config(image="", text="Fly preview unavailable")
            return

        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.entry_fly_display_image = None
            self.entry_fly_label.config(image="", text="Fly preview unavailable")
            return

        image = self._build_entry_photo_stage(source_image)
        max_bound = self._entry_scale(372)
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

    def create_main_content(self, parent):
        content_frame = ttk.Frame(parent)
        content_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.N, tk.S, tk.W, tk.E), pady=(0, 10))
        content_frame.columnconfigure(0, weight=0)
        content_frame.columnconfigure(1, weight=1)
        content_frame.columnconfigure(2, weight=0)
        content_frame.rowconfigure(0, weight=1)

        self.create_motion_control(content_frame)
        self.create_channel_preview(content_frame)
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

    def create_channel_preview(self, parent):
        preview_frame = ttk.LabelFrame(parent, text="Channel Detection Preview", style="Log.TLabelframe", padding="10")
        preview_frame.grid(row=0, column=1, sticky=(tk.N, tk.S, tk.W, tk.E), padx=8)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

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

    def create_device_operations(self, parent):
        controls_frame = ttk.Frame(parent)
        controls_frame.grid(row=0, column=2, sticky=(tk.N, tk.S))
        controls_frame.columnconfigure(0, weight=1)

        device_frame = ttk.LabelFrame(controls_frame, text="Device Control", style="Device.TLabelframe", padding="10")
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

        ops_frame = ttk.LabelFrame(controls_frame, text="Operations", style="Ops.TLabelframe", padding=(10, 6, 10, 8))
        ops_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(10, 0))
        ops_frame.columnconfigure(0, weight=1)
        ops_frame.rowconfigure(0, weight=0)

        operations_content = tk.Frame(ops_frame, bg="#F8E8F0")
        operations_content.grid(row=0, column=0, sticky=(tk.N, tk.W, tk.E))
        operations_content.columnconfigure(0, minsize=126)
        operations_content.columnconfigure(1, weight=1)
        operations_content.rowconfigure(0, weight=0)

        panel_height = 170
        button_gap = 12
        button_margin = 20

        action_frame = tk.Frame(operations_content, bg="#F8E8F0", width=126, height=panel_height)
        action_frame.grid(row=0, column=0, sticky=(tk.N, tk.W), padx=(0, 12))
        action_frame.grid_propagate(False)
        action_frame.columnconfigure(0, weight=1)
        top_button_spacer = tk.Frame(action_frame, bg="#F8E8F0", height=button_margin)
        top_button_spacer.grid(row=0, column=0, sticky=(tk.W, tk.E))

        self.run_button = self.make_button(action_frame, "Run Automated", "#9C27B0", self.run_automated)
        self.run_button.grid(row=1, column=0, sticky=(tk.W, tk.E))
        self.remote_unsupported_widgets.append(self.run_button)

        self.assay_button = self.make_button(action_frame, "Run Assay", "#9C27B0", self.run_assay)
        self.assay_button.grid(row=3, column=0, sticky=(tk.W, tk.E))

        self.classify_button = self.make_button(action_frame, "Classify Fly", "#9C27B0", self.classify_fly_gui)
        self.classify_button.grid(row=5, column=0, sticky=(tk.W, tk.E))

        tk.Frame(action_frame, bg="#F8E8F0", height=button_gap).grid(row=2, column=0, sticky=(tk.W, tk.E))
        tk.Frame(action_frame, bg="#F8E8F0", height=button_gap).grid(row=4, column=0, sticky=(tk.W, tk.E))
        tk.Frame(action_frame, bg="#F8E8F0", height=button_margin).grid(row=6, column=0, sticky=(tk.W, tk.E))

        self.create_operations_logo(operations_content, panel_height)

    def create_system_controls(self, parent):
        system_frame = ttk.LabelFrame(parent, text="System Control", padding="10")
        system_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))

        self.start_button = self.make_button(system_frame, "START", "#4CAF50", self.system_start)
        self.start_button.grid(row=0, column=0, pady=3, padx=5, sticky=(tk.W, tk.E))
        self.remote_unsupported_widgets.append(self.start_button)

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

        banner_box_width = 312
        banner_box_height = 88
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
                banner_label.config(text="")

    def make_button(self, parent, text: str, color: str, command):
        button = tk.Button(
            parent,
            text=text,
            bg=color,
            fg="white",
            font=("Arial", 10, "bold"),
            relief="raised",
            command=command,
        )
        self.register_control(button)
        return button

    def create_device_card(self, parent, row: int, actuator: str, title: str, description: str, command):
        card = tk.Frame(
            parent,
            bg="#FFFFFF",
            highlightbackground="#D4DCE5",
            highlightthickness=1,
            bd=0,
            padx=10,
            pady=8,
        )
        card.grid(row=row, column=0, pady=(0, 10 if row == 0 else 0), sticky=(tk.W, tk.E))
        card.columnconfigure(0, weight=1)

        header = tk.Frame(card, bg="#FFFFFF")
        header.grid(row=0, column=0, sticky=(tk.W, tk.E))
        header.columnconfigure(0, weight=1)

        tk.Label(
            header,
            text=title,
            bg="#FFFFFF",
            fg="#1F2933",
            font=("Arial", 11, "bold"),
        ).grid(row=0, column=0, sticky=tk.W)

        state_var = tk.StringVar(value="OFF")
        detail_var = tk.StringVar(value="Currently off")
        state_label = tk.Label(
            header,
            textvariable=state_var,
            bg="#EEF2F6",
            fg="#52606D",
            font=("Arial", 8, "bold"),
            padx=10,
            pady=4,
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
            font=("Arial", 8),
            justify="left",
            wraplength=220,
        ).grid(row=1, column=0, pady=(4, 6), sticky=tk.W)

        control_row = tk.Frame(card, bg="#FFFFFF")
        control_row.grid(row=2, column=0, sticky=(tk.W, tk.E))
        control_row.columnconfigure(1, weight=1)

        switch = SliderSwitch(control_row, command=command, initial=False, bg="#FFFFFF")
        switch.grid(row=0, column=0, sticky=tk.W)

        tk.Label(
            control_row,
            textvariable=detail_var,
            bg="#FFFFFF",
            fg="#1F2933",
            font=("Arial", 10, "bold"),
            anchor="w",
        ).grid(row=0, column=1, padx=(10, 0), sticky=(tk.W, tk.E))

        self.update_device_card_state(actuator, False)
        return switch

    def create_operations_logo(self, parent, panel_height: int):
        logo_area = tk.Frame(parent, bg="#F8E8F0", height=panel_height)
        logo_area.grid(row=0, column=1, sticky=(tk.N, tk.W, tk.E))
        logo_area.grid_propagate(False)
        logo_area.columnconfigure(0, weight=1)
        logo_margin = 10
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
        self.operations_logo_label.grid(row=1, column=0)
        tk.Frame(logo_area, bg="#F8E8F0", height=logo_margin).grid(row=2, column=0, sticky=(tk.W, tk.E))
        self.load_operations_logo()

    def load_operations_logo(self):
        logo_path = self._resolve_asset_path(
            "drosophilafly.png",
            "drosophila.png",
            "drosphoila.png",
        )
        if logo_path is None:
            self.operations_logo_image = None
            self.operations_logo_label.config(
                image="",
                text="Team fly logo unavailable",
                bg="#F8E8F0",
            )
            return
        try:
            from PIL import Image, ImageTk

            image = Image.open(logo_path)
            image = image.convert("RGBA")
            resample = getattr(Image, "Resampling", Image)
            image.thumbnail((152, 152), resample.LANCZOS)
            self.operations_logo_image = ImageTk.PhotoImage(image)
            self.operations_logo_label.config(
                image=self.operations_logo_image,
                text="",
                bg="#F8E8F0",
                compound="center",
                anchor="center",
            )
        except Exception:
            self.operations_logo_image = None
            self.operations_logo_label.config(
                image="",
                text="Team fly logo unavailable",
                bg="#F8E8F0",
            )

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
                elif kind == "actuator":
                    self.update_actuator_state(item[1], item[2])
                elif kind == "classification_result":
                    self.show_classification_result(item[1])
                elif kind == "clear_stop":
                    self.stop_requested.clear()
                elif kind == "remote_connection":
                    connection_state = ConnectionState(item[1])
                    self._apply_connection_state(connection_state, item[2])
                    if connection_state in {ConnectionState.CLIENT_CONNECTED, ConnectionState.CLIENT_RECONNECTED}:
                        self._schedule_remote_home_prompt()
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
        message = f"Class: {class_name}\nConfidence: {confidence:.4f}"
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

    def update_channel_preview(self):
        if self.is_remote_mode():
            if not self.remote_connected:
                self.set_preview_placeholder("Remote mode preview unavailable while disconnected.")
            elif self.preview_image is None and not self._remote_preview_fetch_in_flight:
                self.set_preview_placeholder("Waiting for remote channel detection image...")
            self.root.after(1000, self.update_channel_preview)
            return

        output_dir = self._current_channel_output_dir()
        output_text = str(output_dir)
        if output_text != self._last_output_dir_text:
            self._last_output_dir_text = output_text
            self.output_dir_var.set(output_text)

        preview_path = self._resolve_channel_file("last_channel_annotated.png")
        result_path = self._resolve_channel_file("last_channel_result.json")

        result = self._read_detection_result(result_path) if result_path.exists() else None
        if result:
            positions = result.get("x_positions_mm", [])
            count = len(positions) if isinstance(positions, list) else 0
            remaining = bool(result.get("fly_remaining", False))
            modified = time.strftime("%H:%M:%S", time.localtime(result_path.stat().st_mtime))
            self.detection_var.set(f"remaining={remaining} count={count} last_update={modified}")
            self.last_result_mtime = result_path.stat().st_mtime
        else:
            self.detection_var.set("Waiting for channel detection output.")

        if preview_path.exists():
            preview_mtime = preview_path.stat().st_mtime
            if preview_mtime != self.last_preview_mtime:
                self.load_channel_preview(preview_path)
                self.last_preview_mtime = preview_mtime
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

    def set_preview_placeholder(self, message: str):
        self.preview_source_image = None
        self.preview_label.config(image="", text=message, bg="black", fg="white")

    def _refresh_preview_image(self, _event=None):
        if self.preview_source_image is not None:
            self._render_preview_image()

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

    def _load_positions_from_json(self, path: Path):
        result = self._read_detection_result(path)
        if result is None:
            self.worker_log(f"Invalid JSON format in: {path}")
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
        runtime = self._load_local_runtime()
        assay_callable = runtime["assay"]
        self.worker_status("assaying", "Running assay.")
        self.ui_queue.put(("actuator", "vibration", True))
        try:
            assay_callable()
        finally:
            self.ui_queue.put(("actuator", "vibration", False))

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

    def _run_automated_worker(self):
        runtime = self._load_local_runtime()
        motion = runtime["motion"]
        assay_callable = runtime["assay"]
        chamber_drop_s = 2.0
        chamber_identify_s = 6.0
        chamber_pickup_s = 2.0
        tube_drop_s = 2.0
        camera_photo_position = self._clamp_operational(config.CHANNEL_LOCATION_END + 15.0)

        cycle_index = 0
        last_detection_mtime = self.last_used_detection_mtime

        try:
            while True:
                self._check_stop()
                cycle_index += 1

                tube_label = "Tube 1" if (cycle_index - 1) % 2 == 0 else "Tube 2"
                tube_position = config.TUBE_1_CENTER if (cycle_index - 1) % 2 == 0 else config.TUBE_2_CENTER

                self.worker_status("running", f"Cycle {cycle_index}: homing gantry.")
                self._set_vacuum(False)
                motion.home_to_zero()

                self.worker_status("moving", f"Cycle {cycle_index}: moving to channel photo position.")
                motion.move_to_absolute(camera_photo_position)

                last_detection_mtime, positions = self._wait_for_detection_result(last_detection_mtime)
                self.last_used_detection_mtime = last_detection_mtime

                if positions == "done":
                    self.worker_log("No more flies remaining. Ending automated run.")
                    break

                pickup_position = positions[0]
                self.worker_log(f"Selected pickup position: {pickup_position:.2f} mm")

                self.worker_status("running", f"Cycle {cycle_index}: accuracy reset home before pickup.")
                self._set_vacuum(False)
                motion.home_to_zero()

                self.worker_status("moving", f"Cycle {cycle_index}: moving to pickup position.")
                motion.move_to_absolute(pickup_position)

                self.worker_status("picking", f"Cycle {cycle_index}: picking fly.")
                self._set_vacuum(True)
                self._sleep_with_stop(2.0)

                self.worker_status("moving", "Moving to chamber center.")
                motion.move_to_absolute(config.CHAMBER_CENTER)

                self.worker_status("running", "Dropping fly in chamber.")
                self._set_vacuum(False)
                self._sleep_with_stop(chamber_drop_s)

                self.worker_status("running", "Identification window active.")
                self._sleep_with_stop(chamber_identify_s)

                self.worker_status("picking", "Picking fly from chamber.")
                self._set_vacuum(True)
                self._sleep_with_stop(chamber_pickup_s)

                self.worker_status("moving", f"Moving to {tube_label}.")
                motion.move_to_absolute(tube_position)

                self.worker_status("running", f"Dropping fly into {tube_label}.")
                self._set_vacuum(False)
                self._sleep_with_stop(tube_drop_s)

                self.worker_status("running", f"Cycle {cycle_index}: returning home.")
                motion.home_to_zero()

            self.worker_status("assaying", "Sorting complete. Assay starts in 10 seconds.")
            for seconds_left in range(10, 0, -1):
                self._check_stop()
                self.worker_log(f"Assay starts in {seconds_left}...")
                self._sleep_with_stop(1.0)

            self.ui_queue.put(("actuator", "vibration", True))
            try:
                assay_callable()
            finally:
                self.ui_queue.put(("actuator", "vibration", False))
        finally:
            self._set_vacuum(False)
            self.ui_queue.put(("actuator", "vibration", False))

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

    def run_automated(self):
        if self.is_remote_mode():
            messagebox.showinfo("Remote Mode", "Automated run is not wired for remote mode in this phase.")
            return
        runtime = self._ensure_local_runtime_or_warn("Run Automated")
        if runtime is None:
            return
        if messagebox.askyesno(
            "Confirm",
            "Start automated operation?\n\nThe GUI will follow the existing gantry sequence and wait for channel detection JSON updates.",
        ):
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
        runtime = self._ensure_local_runtime_or_warn("Run Assay")
        if runtime is None:
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
            if not self.remote_stop_allowed:
                messagebox.showinfo("Stop", "Stop is only available while the remote backend is busy.")
                return
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
            self._apply_connection_state(self.connection_state, self.connection_var.get())
            self.set_status("idle", "Remote UI reset.")
            self.remote_seen_log_keys.clear()
            return
        runtime = self._ensure_local_runtime_or_warn("Reset")
        if runtime is None:
            return
        self.start_task("reset", "running", "Resetting system.", self._reset_worker)

    def on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Quit", "A task is still running. Close the GUI anyway?"):
                return
        self.stop_entry_animation()
        self._stop_remote_sync()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()
