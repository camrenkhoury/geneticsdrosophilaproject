#!/usr/bin/env python3
"""Tkinter control GUI for the Drosophila genetics system."""

from __future__ import annotations

import contextlib
import json
import math
import queue
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import config
from assay import assay
from fly_classifier import classify_fly
from motion import (
    GPIO_AVAILABLE,
    get_current_position,
    get_operational_max_mm,
    home_to_zero,
    move_relative,
    move_to_absolute,
)
from vacuum import vacuum_off, vacuum_on
from vibration import vibration_off, vibration_on


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
        mode = " (Simulation Mode)" if not GPIO_AVAILABLE else ""
        self.root.title(f"Drosophila Genetics Control Panel{mode}")
        self.root.geometry("1100x760")
        self.root.minsize(960, 680)
        self.root.state("zoomed")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.code_dir = Path(__file__).resolve().parent
        self.repo_root = self.code_dir.parent
        self.ui_queue: queue.Queue = queue.Queue()
        self.stop_requested = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.current_task_name: str | None = None
        self.current_task_cancellable = False
        self.preview_image = None
        self.operations_logo_image = None
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

        self.state_var = tk.StringVar(value="IDLE")
        self.position_var = tk.StringVar(value="0.00 mm")
        self.message_var = tk.StringVar(value="Ready")
        self.mode_var = tk.StringVar(value="Hardware Mode" if GPIO_AVAILABLE else "Simulation Mode")
        self.detection_var = tk.StringVar(value="Waiting for channel detection output.")
        self.output_dir_var = tk.StringVar(value=str(self._default_channel_output_dir()))
        self.device_state_labels: dict[str, tk.Label] = {}
        self.device_state_text: dict[str, tk.StringVar] = {}
        self.device_detail_text: dict[str, tk.StringVar] = {}

        self.control_widgets = []
        self.toggle_widgets = []
        self.manual_move_entry: ttk.Entry | None = None

        self.create_widgets()
        self._set_window_icon()
        self.set_status("idle", "Ready")
        self.log_message(f"Channel output directory: {self.output_dir_var.get()}")
        self.update_position()
        self.update_channel_preview()
        self.process_queue()

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
        icon_path = self._resolve_asset_path(
            "drosophila.png",
            "drosophilafly.png",
            "drosphoila.png",
        )
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

        self.entry_frame = tk.Frame(self.page_container, bg="#FFFFFF", padx=40, pady=40)
        self.entry_frame.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))
        self.entry_frame.columnconfigure(0, weight=1)
        self.entry_frame.rowconfigure(0, weight=1)
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
        entry_card = tk.Frame(
            parent,
            bg="#686766",
            highlightbackground="#686766",
            highlightthickness=1,
            bd=0,
            padx=61,
            pady=61,
        )
        entry_card.grid(row=0, column=0)
        entry_card.columnconfigure(0, weight=0)
        entry_card.columnconfigure(1, weight=0)

        left_panel = tk.Frame(entry_card, bg="#686766")
        left_panel.grid(row=0, column=0, sticky=(tk.N, tk.W), padx=(0, 47))

        tk.Label(
            left_panel,
            text="Drosophila Genetics GUI",
            bg="#686766",
            fg="#F3F4F6",
            font=("Arial", 38, "bold"),
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 17))

        tk.Label(
            left_panel,
            text="Open the control panel when you are ready.",
            bg="#686766",
            fg="#E5E7EA",
            font=("Arial", 18),
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky=tk.W, pady=(0, 38))

        enter_button = tk.Button(
            left_panel,
            text="Enter Control Panel",
            bg="#8E2D2B",
            fg="#FFFFFF",
            activebackground="#732220",
            activeforeground="#FFFFFF",
            font=("Arial", 21, "bold"),
            padx=30,
            pady=17,
            relief="raised",
            bd=0,
            command=self.show_control_panel,
        )
        enter_button.grid(row=2, column=0, sticky=tk.W)

        fly_panel = tk.Frame(entry_card, bg="#686766", padx=10, pady=7)
        fly_panel.grid(row=0, column=1, sticky=(tk.N, tk.E))

        self.entry_fly_label = tk.Label(
            fly_panel,
            bg="#686766",
            bd=0,
            highlightthickness=0,
        )
        self.entry_fly_label.grid(row=0, column=0)

    def show_entry_page(self):
        self.main_frame.grid_remove()
        self.entry_frame.grid()
        self._display_entry_photo()

    def show_control_panel(self):
        self.entry_frame.grid_remove()
        self.main_frame.grid()

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

        margin_x = 51
        margin_y = 44
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
        max_bounds = (372, 372)
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
        canvas_size = 240
        center_x = canvas_size // 2
        center_y = 104

        built_frames = []
        for frame_index in range(frame_count):
            orbit_angle = (2.0 * math.pi * frame_index) / frame_count
            image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image, "RGBA")

            glow_center_y = 194
            draw.ellipse((56, glow_center_y - 11, 184, glow_center_y + 11), fill=(88, 237, 255, 28))
            draw.ellipse((68, glow_center_y - 7, 172, glow_center_y + 7), fill=(88, 237, 255, 54))
            draw.ellipse((78, glow_center_y - 3, 162, glow_center_y + 3), outline=(159, 247, 255, 180), width=2)
            draw.arc((54, glow_center_y - 14, 186, glow_center_y + 14), start=18, end=162, fill=(146, 244, 255, 150), width=2)

            frame_image = source_image.copy()
            x_shift = 14.0 * math.cos(orbit_angle)
            vertical_bob = -10.0 * math.sin(orbit_angle)
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

            max_bounds = (146, 146)
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

        ttk.Label(status_frame, text="Position:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky=tk.W, pady=2)
        self.pos_label = tk.Label(
            status_frame,
            textvariable=self.position_var,
            bg="white",
            relief="sunken",
            font=("Arial", 10),
            padx=5,
            pady=2,
        )
        self.pos_label.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Message:", font=("Arial", 10, "bold")).grid(row=2, column=0, sticky=tk.W, pady=2)
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
        self.message_label.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Mode:", font=("Arial", 10, "bold")).grid(row=3, column=0, sticky=tk.W, pady=2)
        mode_color = "#4CAF50" if GPIO_AVAILABLE else "#FF9800"
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
        self.mode_label.grid(row=3, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Detection:", font=("Arial", 10, "bold")).grid(row=4, column=0, sticky=tk.W, pady=2)
        self.detection_label = tk.Label(
            status_frame,
            textvariable=self.detection_var,
            bg="#F7F7F7",
            relief="sunken",
            font=("Arial", 10),
            padx=5,
            pady=2,
            anchor="w",
            justify="left",
        )
        self.detection_label.grid(row=4, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Output Dir:", font=("Arial", 10, "bold")).grid(row=5, column=0, sticky=tk.W, pady=2)
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
        self.output_dir_label.grid(row=5, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

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

        ttk.Separator(motion_frame, orient="horizontal").grid(row=8, column=0, sticky=(tk.W, tk.E), pady=8)
        ttk.Label(motion_frame, text="Manual Move (mm):", font=("Arial", 9, "bold")).grid(row=9, column=0, pady=(5, 2), sticky=tk.W)
        self.manual_move_entry = ttk.Entry(motion_frame, width=12, font=("Arial", 10))
        self.manual_move_entry.grid(row=10, column=0, pady=2, sticky=(tk.W, tk.E))
        self.register_control(self.manual_move_entry)

        manual_button = self.make_button(motion_frame, "Move Relative", "#607D8B", self.manual_move)
        manual_button.grid(row=11, column=0, pady=3, sticky=(tk.W, tk.E))

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
            width=48,
            height=20,
            anchor=tk.CENTER,
            justify="center",
        )
        self.preview_label.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.W, tk.E))

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

        self.stop_button = self.make_button(system_frame, "STOP", "#F44336", self.system_stop)
        self.stop_button.grid(row=0, column=1, pady=3, padx=5, sticky=(tk.W, tk.E))
        self.stop_button.config(state=tk.DISABLED)

        self.reset_button = self.make_button(system_frame, "RESET", "#FF9800", self.system_reset)
        self.reset_button.grid(row=0, column=2, pady=3, padx=5, sticky=(tk.W, tk.E))

        clear_log_button = self.make_button(system_frame, "CLEAR LOG", "#607D8B", self.clear_log)
        clear_log_button.grid(row=0, column=3, pady=3, padx=5, sticky=(tk.W, tk.E))

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
            image.thumbnail((128, 128), resample.LANCZOS)
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
        try:
            self.position_var.set(f"{get_current_position():.2f} mm")
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
        except queue.Empty:
            pass

        self.root.after(100, self.process_queue)

    def set_status(self, state: str, message: str):
        self.state_var.set(state.upper())
        self.message_var.set(message)
        color = {
            "idle": "#4CAF50",
            "running": "#2196F3",
            "detecting": "#FF9800",
            "moving": "#FF9800",
            "picking": "#FF9800",
            "assaying": "#9C27B0",
            "error": "#F44336",
            "stopped": "#607D8B",
        }.get(state.lower(), "#9E9E9E")
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
        state = tk.DISABLED if busy else tk.NORMAL
        entry_state = "disabled" if busy else "normal"

        for widget in self.control_widgets:
            if widget in (self.stop_button, self.reset_button):
                continue
            try:
                if isinstance(widget, ttk.Entry):
                    widget.config(state=entry_state)
                else:
                    widget.config(state=state)
            except tk.TclError:
                pass

        for toggle in self.toggle_widgets:
            toggle.set_enabled(not busy)

        self.stop_button.config(state=tk.NORMAL if busy and cancellable else tk.DISABLED)
        self.reset_button.config(state=tk.DISABLED if busy else tk.NORMAL)

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
        return self.repo_root / "fin6" / "outputs" / "channel"

    def _settings_channel_output_dir(self) -> Path | None:
        settings_path = self.repo_root / "fin6" / ".fly_tracking_gui_settings.json"
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
                Path.home() / "fin6" / "outputs" / "channel",
                Path.home() / "geneticsdrosophiliaproject" / "fin6" / "outputs" / "channel",
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
            from PIL import Image, ImageTk

            image = Image.open(path)
            image = image.convert("RGB")
            resample = getattr(Image, "Resampling", Image)
            image.thumbnail((420, 300), resample.LANCZOS)
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview_label.config(image=self.preview_image, text="")
        except Exception:
            self.preview_image = None
            self.set_preview_placeholder(f"Preview unavailable:\n{path.name}")

    def set_preview_placeholder(self, message: str):
        self.preview_label.config(image="", text=message, bg="black", fg="white")

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
        if enabled:
            vacuum_on()
        else:
            vacuum_off()
        self.ui_queue.put(("actuator", "vacuum", enabled))

    def _set_vibration(self, enabled: bool):
        if enabled:
            vibration_on()
        else:
            vibration_off()
        self.ui_queue.put(("actuator", "vibration", enabled))

    def _clamp_operational(self, position_mm: float) -> float:
        return max(0.0, min(position_mm, get_operational_max_mm()))

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

        return sorted((self._clamp_operational(value) for value in positions), reverse=True)

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
        self.worker_status("assaying", "Running assay.")
        self.ui_queue.put(("actuator", "vibration", True))
        try:
            assay()
        finally:
            self.ui_queue.put(("actuator", "vibration", False))

    def _classify_worker(self):
        self.worker_status("running", "Capturing and classifying fly.")
        result = classify_fly()
        self.ui_queue.put(("classification_result", result))

    def _reset_worker(self):
        self.worker_status("running", "Resetting actuators to safe state.")
        self._set_vacuum(False)
        self._set_vibration(False)
        self.ui_queue.put(("clear_stop",))

    def _run_automated_worker(self):
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
                home_to_zero()

                self.worker_status("moving", f"Cycle {cycle_index}: moving to channel photo position.")
                move_to_absolute(camera_photo_position)

                last_detection_mtime, positions = self._wait_for_detection_result(last_detection_mtime)
                self.last_used_detection_mtime = last_detection_mtime

                if positions == "done":
                    self.worker_log("No more flies remaining. Ending automated run.")
                    break

                pickup_position = positions[0]
                self.worker_log(f"Selected pickup position: {pickup_position:.2f} mm")

                self.worker_status("moving", f"Cycle {cycle_index}: moving to pickup position.")
                move_to_absolute(pickup_position)

                self.worker_status("picking", f"Cycle {cycle_index}: picking fly.")
                self._set_vacuum(True)
                self._sleep_with_stop(2.0)

                self.worker_status("moving", "Moving to chamber center.")
                move_to_absolute(config.CHAMBER_CENTER)

                self.worker_status("running", "Dropping fly in chamber.")
                self._set_vacuum(False)
                self._sleep_with_stop(chamber_drop_s)

                self.worker_status("running", "Identification window active.")
                self._sleep_with_stop(chamber_identify_s)

                self.worker_status("picking", "Picking fly from chamber.")
                self._set_vacuum(True)
                self._sleep_with_stop(chamber_pickup_s)

                self.worker_status("moving", f"Moving to {tube_label}.")
                move_to_absolute(tube_position)

                self.worker_status("running", f"Dropping fly into {tube_label}.")
                self._set_vacuum(False)
                self._sleep_with_stop(tube_drop_s)

                self.worker_status("running", f"Cycle {cycle_index}: returning home.")
                home_to_zero()

            self.worker_status("assaying", "Sorting complete. Assay starts in 10 seconds.")
            for seconds_left in range(10, 0, -1):
                self._check_stop()
                self.worker_log(f"Assay starts in {seconds_left}...")
                self._sleep_with_stop(1.0)

            self.ui_queue.put(("actuator", "vibration", True))
            try:
                assay()
            finally:
                self.ui_queue.put(("actuator", "vibration", False))
        finally:
            self._set_vacuum(False)
            self.ui_queue.put(("actuator", "vibration", False))

    def home_gantry(self):
        self.start_task("home", "moving", "Homing gantry.", home_to_zero)

    def move_to_position(self, position: float, label: str):
        self.start_task(
            f"move to {label}",
            "moving",
            f"Moving to {label}.",
            lambda: move_to_absolute(position),
        )

    def manual_move(self):
        assert self.manual_move_entry is not None
        try:
            distance = float(self.manual_move_entry.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid distance value.")
            self.manual_move_entry.delete(0, tk.END)
            return

        def task():
            move_relative(distance)

        if self.start_task("manual move", "moving", f"Moving relative by {distance:.2f} mm.", task):
            self.manual_move_entry.delete(0, tk.END)

    def set_vacuum_from_ui(self, enabled: bool):
        def task():
            self.worker_status("running", f"Turning vacuum {'on' if enabled else 'off'}.")
            self._set_vacuum(enabled)

        self.start_task("vacuum", "running", f"Turning vacuum {'on' if enabled else 'off'}.", task)

    def set_vibration_from_ui(self, enabled: bool):
        def task():
            self.worker_status("running", f"Turning vibration {'on' if enabled else 'off'}.")
            self._set_vibration(enabled)

        self.start_task("vibration", "running", f"Turning vibration {'on' if enabled else 'off'}.", task)

    def run_automated(self):
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
        self.start_task("assay", "assaying", "Running assay.", self._run_assay_worker)

    def classify_fly_gui(self):
        self.start_task("classification", "running", "Classifying fly.", self._classify_worker)

    def system_start(self):
        self.run_automated()

    def system_stop(self):
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
        self.start_task("reset", "running", "Resetting system.", self._reset_worker)

    def on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Quit", "A task is still running. Close the GUI anyway?"):
                return
        self.stop_entry_animation()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()
