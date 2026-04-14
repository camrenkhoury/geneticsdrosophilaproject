from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional, Tuple

from .controller import WorkflowController
from .settings import OperatorSettingsStore
from .state import OperatorState, VialState, WorkflowStage
from .theme import BIG_COUNT_FONT, BODY_FONT, BUTTON_FONT, CHIP_FONT, HEADLINE_FONT, LABEL_FONT, MONO_FONT, PALETTE, SECTION_FONT, TITLE_FONT

try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover - fallback path on minimal installs
    Image = None
    ImageTk = None


class ScrollableFrame(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.configure(bg=kwargs.get("bg", PALETTE.background))
        self.canvas = tk.Canvas(self, bg=self["bg"], highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=self["bg"])
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._resize_inner)

    def _resize_inner(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)


class ChoiceDialog(tk.Toplevel):
    def __init__(
        self,
        parent,
        title: str,
        message: str,
        options: List[str],
        *,
        timeout_s: Optional[float] = None,
    ):
        super().__init__(parent)
        self.result: Optional[str] = None
        self._timeout_deadline = None if timeout_s is None else (time.monotonic() + max(1.0, float(timeout_s)))
        self._countdown_var = tk.StringVar(value="")
        self.title(title)
        self.configure(bg=PALETTE.surface_lowest)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        body = tk.Frame(self, bg=PALETTE.surface_lowest, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, bg=PALETTE.surface_lowest, fg=PALETTE.text, font=SECTION_FONT).pack(anchor="w")
        tk.Label(body, text=message, bg=PALETTE.surface_lowest, fg=PALETTE.text_muted, font=BODY_FONT, justify="left", wraplength=420).pack(anchor="w", pady=(8, 12))
        if self._timeout_deadline is not None:
            tk.Label(
                body,
                textvariable=self._countdown_var,
                bg=PALETTE.surface_lowest,
                fg=PALETTE.text_muted,
                font=LABEL_FONT,
                justify="left",
                wraplength=420,
            ).pack(anchor="w", pady=(0, 14))
            self._tick_timeout()
        row = tk.Frame(body, bg=PALETTE.surface_lowest)
        row.pack(fill="x")
        for option in options:
            tk.Button(
                row,
                text=option,
                command=lambda value=option: self._choose(value),
                bg=PALETTE.primary if option == options[0] else PALETTE.secondary_container,
                fg=PALETTE.surface_lowest if option == options[0] else PALETTE.text,
                activebackground=PALETTE.primary_container,
                activeforeground=PALETTE.surface_lowest,
                relief="flat",
                font=BUTTON_FONT,
                padx=18,
                pady=10,
                cursor="hand2",
            ).pack(side="left", padx=(0, 10))
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _tick_timeout(self):
        if self._timeout_deadline is None:
            return
        remaining = max(0, int(round(self._timeout_deadline - time.monotonic())))
        if remaining <= 0:
            self._close()
            return
        self._countdown_var.set(f"Waiting for operator choice. This window will close automatically in {remaining}s.")
        self.after(250, self._tick_timeout)

    def _choose(self, value: str):
        self.result = value
        self.destroy()

    def _close(self):
        self.result = None
        self.destroy()


class OperatorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Drosophila Stitch Operator")
        self.geometry("1640x1020")
        self.minsize(1320, 860)
        self.configure(bg=PALETTE.background)

        self.settings_store = OperatorSettingsStore()
        self.controller = WorkflowController(self.settings_store)

        self._nav_buttons: Dict[str, tk.Button] = {}
        self._pages: Dict[str, tk.Frame] = {}
        self._image_widgets: Dict[str, Dict[str, object]] = {}
        self._vial_widgets: Dict[str, List[Dict[str, tk.Label]]] = {}
        self._result_vial_widgets: List[Dict[str, tk.Label]] = []
        self._last_log_count = 0
        self._active_page = "Workflow"
        self._ui_refresh_ms = 500

        self.status_stage_var = tk.StringVar(value="Idle")
        self.status_message_var = tk.StringVar(value="System idle.")
        self.status_next_var = tk.StringVar(value="Initialize")
        self.status_profile_var = tk.StringVar(value="")
        self.status_homed_var = tk.StringVar(value="Not homed")
        self.status_model_var = tk.StringVar(value="Model missing")
        self.status_channel_camera_var = tk.StringVar(value="Channel camera")
        self.status_assay_camera_var = tk.StringVar(value="Assay camera")
        self.footer_var = tk.StringVar(value="Ready")
        self.uptime_var = tk.StringVar(value="00:00:00")

        self.debug_model_var = tk.StringVar(value=self.controller.settings.sexing_model_path)
        self.debug_profile_var = tk.StringVar(value=self.controller.assay.profile.name)
        self.debug_move_var = tk.StringVar(value="0")
        self.debug_abs_var = tk.StringVar(value="0")
        self.tuning_pickup_offset_var = tk.StringVar(value="")
        self.tuning_pick_delay_var = tk.StringVar(value="")
        self.tuning_drop_delay_var = tk.StringVar(value="")
        self.tuning_release_delay_var = tk.StringVar(value="")
        self.tuning_classification_delay_var = tk.StringVar(value="")
        self.tuning_channel_camera_pos_var = tk.StringVar(value="")
        self.tuning_chamber_clear_offset_var = tk.StringVar(value="")
        self.tuning_channel_mm_var = tk.StringVar(value="")

        self.assay_duration_var = tk.StringVar(value="")
        self.assay_analysis_fps_var = tk.StringVar(value="")
        self.assay_threshold_var = tk.StringVar(value="")
        self.assay_min_area_var = tk.StringVar(value="")
        self.assay_max_area_var = tk.StringVar(value="")
        self.assay_box_mode_var = tk.StringVar(value="summaries")
        self.assay_box_config_var = tk.StringVar(value="")
        self.assay_box_enabled_var = tk.BooleanVar(value=False)
        self.assay_box_auto_upload_var = tk.BooleanVar(value=False)

        self._reload_tuning_vars_from_controller()
        self._reload_assay_tuning_vars_from_controller()

        self._build_shell()
        self._show_page("Workflow")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll_events)
        self.after(self._ui_refresh_ms, self._refresh_ui)
        self.after(1000, self._periodic_controller_refresh)

    # ------------------------------------------------------------------
    # shell + shared helpers
    # ------------------------------------------------------------------
    def _build_shell(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = tk.Frame(self, bg=PALETTE.surface_highest, height=68)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)
        header.grid_propagate(False)

        left = tk.Frame(header, bg=PALETTE.surface_highest)
        left.grid(row=0, column=0, sticky="w", padx=24, pady=10)
        tk.Label(left, text="DrosophilaSpr26", bg=PALETTE.surface_highest, fg=PALETTE.text, font=("Inter", 22, "bold")).pack(anchor="w")
        tk.Label(left, text="Automated loading console", bg=PALETTE.surface_highest, fg=PALETTE.text_muted, font=BODY_FONT).pack(anchor="w")

        right = tk.Frame(header, bg=PALETTE.surface_highest)
        right.grid(row=0, column=1, sticky="e", padx=24)
        self._chip(right, self.status_profile_var, PALETTE.surface_lowest, PALETTE.text).pack(side="left", padx=6)
        self._chip(right, self.status_homed_var, PALETTE.secondary_fixed, PALETTE.text).pack(side="left", padx=6)
        self._chip(right, self.status_model_var, PALETTE.secondary_fixed, PALETTE.text).pack(side="left", padx=6)

        nav = tk.Frame(self, bg=PALETTE.background)
        nav.grid(row=1, column=0, sticky="ew", padx=18, pady=(12, 0))
        for name in ["Workflow", "Channel", "Loading / Sexing", "Assay", "Results", "Debug / Advanced"]:
            btn = tk.Button(
                nav,
                text=name,
                command=lambda n=name: self._show_page(n),
                bg=PALETTE.surface_highest,
                fg=PALETTE.text_muted,
                activebackground=PALETTE.surface_low,
                activeforeground=PALETTE.text,
                relief="flat",
                font=("Inter", 11, "bold"),
                padx=18,
                pady=12,
                cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 8))
            self._nav_buttons[name] = btn

        content = tk.Frame(self, bg=PALETTE.background)
        content.grid(row=2, column=0, sticky="nsew", padx=18, pady=18)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.content = content

        self._pages["Workflow"] = self._build_workflow_page(content)
        self._pages["Channel"] = self._build_channel_page(content)
        self._pages["Loading / Sexing"] = self._build_loading_page(content)
        self._pages["Assay"] = self._build_assay_page(content)
        self._pages["Results"] = self._build_results_page(content)
        self._pages["Debug / Advanced"] = self._build_debug_page(content)
        for page in self._pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        footer = tk.Frame(self, bg=PALETTE.surface_lowest, height=58)
        footer.grid(row=3, column=0, sticky="ew", padx=120, pady=(0, 14))
        footer.grid_propagate(False)
        tk.Label(footer, textvariable=self.status_stage_var, bg=PALETTE.surface_lowest, fg=PALETTE.primary, font=("Inter", 11, "bold")).pack(side="left", padx=(22, 12))
        tk.Label(footer, textvariable=self.footer_var, bg=PALETTE.surface_lowest, fg=PALETTE.text_muted, font=BODY_FONT).pack(side="left")
        tk.Label(footer, textvariable=self.uptime_var, bg=PALETTE.surface_lowest, fg=PALETTE.text, font=MONO_FONT).pack(side="right", padx=22)

    def _chip(self, parent, text_var: tk.StringVar, bg: str, fg: str) -> tk.Label:
        return tk.Label(parent, textvariable=text_var, bg=bg, fg=fg, font=CHIP_FONT, padx=10, pady=6)

    def _card(self, parent, title: str, subtitle: str = "", *, height: Optional[int] = None) -> Tuple[tk.Frame, tk.Frame]:
        outer = tk.Frame(parent, bg=PALETTE.surface_low, padx=18, pady=16)
        if height is not None:
            outer.configure(height=height)
            outer.pack_propagate(False)
        tk.Label(outer, text=title, bg=PALETTE.surface_low, fg=PALETTE.text, font=SECTION_FONT).pack(anchor="w")
        if subtitle:
            tk.Label(outer, text=subtitle, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT, wraplength=680, justify="left").pack(anchor="w", pady=(4, 12))
        body = tk.Frame(outer, bg=PALETTE.surface_low)
        body.pack(fill="both", expand=True)
        return outer, body

    def _action_button(self, parent, text: str, command, *, primary: bool = True, width: int = 18) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=PALETTE.primary if primary else PALETTE.secondary_container,
            fg=PALETTE.surface_lowest if primary else PALETTE.text,
            activebackground=PALETTE.primary_container if primary else PALETTE.secondary_fixed,
            activeforeground=PALETTE.surface_lowest if primary else PALETTE.text,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=BUTTON_FONT,
            padx=12,
            pady=12,
            width=width,
            cursor="hand2",
        )

    def _image_panel(self, parent, key: str, title: str, *, width: int = 520, height: int = 300) -> tk.Frame:
        outer, body = self._card(parent, title)
        surface = tk.Frame(body, bg="#000000", width=width, height=height)
        surface.pack(fill="both", expand=True)
        surface.pack_propagate(False)
        panel = tk.Label(surface, text="No preview available", bg="#000000", fg="#d0d0d0", font=BODY_FONT, anchor="center", justify="center")
        panel.pack(fill="both", expand=True)
        self._image_widgets[key] = {
            "label": panel,
            "surface": surface,
            "photo": None,
            "size": (width, height),
            "last_path": "",
            "last_mtime": None,
            "last_box": None,
        }
        return outer

    def _trim_black_borders(self, image):
        if Image is None:
            return image
        try:
            grayscale = image.convert("L")
            mask = grayscale.point(lambda px: 255 if int(px) > 8 else 0)
            bbox = mask.getbbox()
            if not bbox:
                return image
            left, top, right, bottom = bbox
            width = max(1, int(right - left))
            height = max(1, int(bottom - top))
            if width < max(40, int(image.width * 0.10)) or height < max(24, int(image.height * 0.08)):
                return image
            if left <= 1 and top <= 1 and right >= image.width - 1 and bottom >= image.height - 1:
                return image
            pad = 6
            return image.crop((
                max(0, int(left) - pad),
                max(0, int(top) - pad),
                min(image.width, int(right) + pad),
                min(image.height, int(bottom) + pad),
            ))
        except Exception:
            return image

    def _update_image_widget(self, key: str, path_text: str, placeholder: str) -> None:
        target = self._image_widgets.get(key)
        if target is None:
            return
        label: tk.Label = target["label"]  # type: ignore[assignment]
        if not path_text:
            label.configure(text=placeholder, image="", bg="#000000", fg="#d0d0d0")
            target["photo"] = None
            target["last_path"] = ""
            target["last_mtime"] = None
            target["last_box"] = None
            return
        path = Path(path_text)
        if not path.exists() or Image is None or ImageTk is None:
            label.configure(text=placeholder, image="", bg="#000000", fg="#d0d0d0")
            target["photo"] = None
            target["last_path"] = ""
            target["last_mtime"] = None
            target["last_box"] = None
            return
        try:
            mtime = path.stat().st_mtime
            fallback_w, fallback_h = target["size"]  # type: ignore[misc]
            box_w = max(120, int(label.winfo_width()) if label.winfo_width() > 1 else int(fallback_w))
            box_h = max(90, int(label.winfo_height()) if label.winfo_height() > 1 else int(fallback_h))
            box = (box_w, box_h)
            if (
                target.get("last_path") == str(path)
                and target.get("last_mtime") == mtime
                and target.get("last_box") == box
                and target.get("photo") is not None
            ):
                return

            image = Image.open(path).convert("RGB")
            if key == "channel_main":
                image = self._trim_black_borders(image)
            src_w, src_h = image.size
            if src_w <= 0 or src_h <= 0:
                raise ValueError("Invalid image size")
            scale = min(box_w / float(src_w), box_h / float(src_h))
            scaled_w = max(1, int(round(src_w * scale)))
            scaled_h = max(1, int(round(src_h * scale)))
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            if (scaled_w, scaled_h) != image.size:
                working = image.resize((scaled_w, scaled_h), resample)
            else:
                working = image
            canvas = Image.new("RGB", box, "#000000")
            offset_x = max(0, (box_w - scaled_w) // 2)
            offset_y = max(0, (box_h - scaled_h) // 2)
            canvas.paste(working, (offset_x, offset_y))
            photo = ImageTk.PhotoImage(canvas)
            label.configure(image=photo, text="", bg="#000000")
            label.image = photo
            target["photo"] = photo
            target["last_path"] = str(path)
            target["last_mtime"] = mtime
            target["last_box"] = box
        except Exception:
            label.configure(text=placeholder, image="", bg="#000000", fg="#d0d0d0")
            target["photo"] = None
            target["last_path"] = ""
            target["last_mtime"] = None
            target["last_box"] = None

    def _metric_block(self, parent, title: str, value_var: tk.StringVar, *, large: bool = False) -> tk.Frame:
        frame = tk.Frame(parent, bg=PALETTE.surface_lowest, padx=16, pady=14)
        tk.Label(frame, text=title, bg=PALETTE.surface_lowest, fg=PALETTE.text_muted, font=LABEL_FONT).pack(anchor="w")
        tk.Label(frame, textvariable=value_var, bg=PALETTE.surface_lowest, fg=PALETTE.text, font=BIG_COUNT_FONT if large else HEADLINE_FONT).pack(anchor="w", pady=(6, 0))
        return frame

    def _build_vial_cards(self, parent, key: str, *, show_result_metric: bool = False) -> List[Dict[str, tk.Label]]:
        row = tk.Frame(parent, bg=PALETTE.background)
        row.pack(fill="x", pady=(8, 0))
        widgets: List[Dict[str, tk.Label]] = []
        vials = self.controller.snapshot().vials
        for idx, vial in enumerate(vials):
            card = tk.Frame(row, bg=PALETTE.surface_low, padx=14, pady=14)
            card.pack(side="left", fill="both", expand=True, padx=(0 if idx == 0 else 12, 0))
            title = tk.Label(card, text=vial.label, bg=PALETTE.surface_low, fg=PALETTE.text, font=SECTION_FONT)
            title.pack(anchor="w")
            sex_label = "Junk" if str(vial.target_sex).lower() == "junk" else str(vial.target_sex).title()
            sex = tk.Label(card, text=sex_label, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT)
            sex.pack(anchor="w", pady=(2, 10))
            count = tk.Label(card, text=f"{vial.current_count} / {vial.max_count}", bg=PALETTE.surface_low, fg=PALETTE.text, font=HEADLINE_FONT)
            count.pack(anchor="w")
            metric_default = "Junk / recovery" if str(vial.target_sex).lower() == "junk" else "Available"
            metric = tk.Label(card, text=metric_default, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT)
            metric.pack(anchor="w", pady=(8, 0))
            status = tk.Label(card, text=vial.status, bg=PALETTE.secondary_fixed, fg=PALETTE.text, font=CHIP_FONT, padx=8, pady=4)
            status.pack(anchor="w", pady=(10, 0))
            widgets.append({"title": title, "sex": sex, "count": count, "metric": metric, "status": status})
        self._vial_widgets[key] = widgets
        return widgets

    def _show_page(self, name: str):
        self._active_page = name
        for page_name, page in self._pages.items():
            if page_name == name:
                page.tkraise()
            btn = self._nav_buttons.get(page_name)
            if btn is not None:
                active = page_name == name
                btn.configure(bg=PALETTE.surface_lowest if active else PALETTE.surface_highest, fg=PALETTE.text if active else PALETTE.text_muted)
        self._refresh_images(self.controller.snapshot())

    # ------------------------------------------------------------------
    # page builders
    # ------------------------------------------------------------------
    def _build_workflow_page(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=PALETTE.background)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        self.workflow_stage_value = tk.StringVar(value="Idle")
        self.workflow_message_value = tk.StringVar(value="System idle.")
        self.workflow_next_value = tk.StringVar(value="Initialize")
        self.workflow_profile_var = tk.StringVar(value="")
        self.workflow_homed_var = tk.StringVar(value="")
        self.workflow_model_var = tk.StringVar(value="")
        self.workflow_channel_camera_var = tk.StringVar(value="")
        self.workflow_assay_camera_var = tk.StringVar(value="")
        self.workflow_hint_var = tk.StringVar(value="Press START AUTO to initialize, capture, and route specimens in sequence.")
        self.workflow_channel_count_var = tk.StringVar(value="0")
        self.workflow_current_fly_var = tk.StringVar(value="--")
        self.workflow_last_sex_var = tk.StringVar(value="--")
        self.workflow_destination_var = tk.StringVar(value="--")
        self.workflow_live_var = tk.StringVar(value="System idle.")

        top_card, top_body = self._card(page, "Auto loading")
        top_card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        top_body.columnconfigure(0, weight=0)
        top_body.columnconfigure(1, weight=1)

        controls = tk.Frame(top_body, bg=PALETTE.surface_lowest, padx=16, pady=14)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        tk.Label(controls, text="Controls", bg=PALETTE.surface_lowest, fg=PALETTE.text_muted, font=LABEL_FONT).pack(anchor="w")
        self._action_button(controls, "START AUTO", lambda: self.controller.start_auto_flow(), width=18).pack(anchor="w", pady=(10, 8))
        self._action_button(controls, "STOP", lambda: self.controller.stop_current_task(), primary=False, width=18).pack(anchor="w")
        self._action_button(controls, "NEW RUN / RESET COUNTS", self._confirm_reset_vial_counts, primary=False, width=18).pack(anchor="w", pady=(12, 0))
        helper_row = tk.Frame(controls, bg=PALETTE.surface_lowest)
        helper_row.pack(fill="x", pady=(14, 0))
        self._action_button(helper_row, "Initialize", lambda: self.controller.start_initialize(), primary=False, width=12).pack(side="left", padx=(0, 8))
        self._action_button(helper_row, "Capture Channel", lambda: self.controller.start_capture_channel(), primary=False, width=14).pack(side="left")
        tk.Label(
            controls,
            text="Use the Channel and Loading / Sexing tabs only when you need the annotated channel image or the live chamber image.",
            bg=PALETTE.surface_lowest,
            fg=PALETTE.text_muted,
            font=BODY_FONT,
            wraplength=240,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

        info = tk.Frame(top_body, bg=PALETTE.surface_lowest, padx=16, pady=14)
        info.grid(row=0, column=1, sticky="nsew")
        tk.Label(info, textvariable=self.workflow_message_value, bg=PALETTE.surface_lowest, fg=PALETTE.text, font=("Inter", 15, "bold"), justify="left", wraplength=980).pack(anchor="w")

        metric_grid = tk.Frame(info, bg=PALETTE.surface_lowest)
        metric_grid.pack(fill="x", pady=(14, 10))
        for column in range(3):
            metric_grid.columnconfigure(column, weight=1)
        metrics = [
            ("Stage", self.workflow_stage_value),
            ("Next", self.workflow_next_value),
            ("Flies in channel", self.workflow_channel_count_var),
            ("Current fly", self.workflow_current_fly_var),
            ("Last sex", self.workflow_last_sex_var),
            ("Destination", self.workflow_destination_var),
        ]
        for idx, (title, var) in enumerate(metrics):
            self._metric_block(metric_grid, title, var).grid(
                row=idx // 3,
                column=idx % 3,
                sticky="nsew",
                padx=(0 if idx % 3 == 0 else 10, 0),
                pady=(0, 10 if idx < 3 else 0),
            )

        live = tk.Frame(info, bg=PALETTE.surface_lowest)
        live.pack(fill="x")
        tk.Label(live, text="Live info", bg=PALETTE.surface_lowest, fg=PALETTE.text_muted, font=LABEL_FONT).pack(anchor="w")
        tk.Label(live, textvariable=self.workflow_live_var, bg=PALETTE.surface_lowest, fg=PALETTE.text, font=BODY_FONT, justify="left", wraplength=1100).pack(anchor="w", pady=(6, 0))

        log_card, log_body = self._card(page, "Live workflow log")
        log_card.grid(row=1, column=0, sticky="nsew", pady=(0, 16))
        self.workflow_log_text = scrolledtext.ScrolledText(
            log_body,
            height=14,
            bg=PALETTE.surface_lowest,
            fg=PALETTE.text,
            insertbackground=PALETTE.text,
            relief="flat",
            font=MONO_FONT,
            wrap="word",
            padx=12,
            pady=12,
        )
        self.workflow_log_text.pack(fill="both", expand=True)
        self.workflow_log_text.insert("1.0", "No workflow log entries yet.")
        self.workflow_log_text.configure(state="disabled")

        vial_section, vial_body = self._card(page, "Destination vials")
        vial_section.grid(row=2, column=0, sticky="ew")
        self._build_vial_cards(vial_body, "workflow")
        return page

    def _build_channel_page(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=PALETTE.background)
        page.columnconfigure(0, weight=5, minsize=900)
        page.columnconfigure(1, weight=2, minsize=360)
        page.rowconfigure(0, weight=1)

        self._image_panel(page, "channel_main", "Channel capture", width=1320, height=340).grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = tk.Frame(page, bg=PALETTE.background)
        right.grid(row=0, column=1, sticky="nsew")

        setup_card, setup_body = self._card(right, "Channel setup", "Capture a background once, calibrate once, then use Capture Channel during operation. This page shows the annotated detection image so the preview matches the positions the workflow is using.")
        setup_card.pack(fill="x")
        self.channel_bg_var = tk.StringVar(value="Background missing")
        self.channel_cal_var = tk.StringVar(value="Calibration missing")
        self.channel_count_var = tk.StringVar(value="0")
        self.channel_positions_var = tk.StringVar(value="--")
        for title, var in [
            ("Background", self.channel_bg_var),
            ("Calibration", self.channel_cal_var),
            ("Fly count", self.channel_count_var),
            ("X positions (mm)", self.channel_positions_var),
        ]:
            row = tk.Frame(setup_body, bg=PALETTE.surface_low)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=title, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=LABEL_FONT, width=16, anchor="w").pack(side="left")
            tk.Label(row, textvariable=var, bg=PALETTE.surface_low, fg=PALETTE.text, font=BODY_FONT, anchor="w", wraplength=320, justify="left").pack(side="left", fill="x", expand=True)

        actions = tk.Frame(setup_body, bg=PALETTE.surface_low)
        actions.pack(fill="x", pady=(12, 0))
        self._action_button(actions, "Capture Background", lambda: self.controller.start_capture_channel_background()).pack(side="left", padx=(0, 8))
        self._action_button(actions, "Calibrate", lambda: self.controller.start_calibrate_channel(), primary=False).pack(side="left", padx=8)
        self._action_button(actions, "Capture Channel", lambda: self.controller.start_capture_channel()).pack(side="left", padx=8)

        hint_card, hint_body = self._card(right, "Guidance")
        hint_card.pack(fill="both", expand=True, pady=(18, 0))
        self.channel_hint_var = tk.StringVar(value="Capture a clean background, calibrate once, then use Capture Channel to refresh the annotated detection view.")
        tk.Label(hint_body, textvariable=self.channel_hint_var, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT, justify="left", wraplength=360).pack(anchor="w")
        return page

    def _build_loading_page(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=PALETTE.background)
        page.columnconfigure(0, weight=6, minsize=920)
        page.columnconfigure(1, weight=3, minsize=360)
        page.rowconfigure(0, weight=1)

        left = tk.Frame(page, bg=PALETTE.background)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._image_panel(left, "loading_sexing", "Sexing chamber", width=1100, height=720).pack(fill="both", expand=True)

        right = tk.Frame(page, bg=PALETTE.background)
        right.grid(row=0, column=1, sticky="nsew")
        top_card, top_body = self._card(right, "Loading / sexing", "Keep this page focused on the current chamber image, the current sex result, and the chosen destination.")
        top_card.pack(fill="x")
        self.loading_sex_var = tk.StringVar(value="--")
        self.loading_conf_var = tk.StringVar(value="0%")
        self.loading_dest_var = tk.StringVar(value="--")
        self.loading_detail_var = tk.StringVar(value="Capture channel and route a fly to begin.")
        metrics = tk.Frame(top_body, bg=PALETTE.surface_low)
        metrics.pack(fill="x")
        self._metric_block(metrics, "Sex", self.loading_sex_var).pack(side="left", fill="both", expand=True, padx=(0, 10))
        self._metric_block(metrics, "Confidence", self.loading_conf_var).pack(side="left", fill="both", expand=True, padx=10)
        self._metric_block(metrics, "Destination", self.loading_dest_var).pack(side="left", fill="both", expand=True, padx=(10, 0))
        tk.Label(top_body, textvariable=self.loading_detail_var, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT, wraplength=360, justify="left").pack(anchor="w", pady=(12, 0))
        self._action_button(top_body, "Route Next Fly", lambda: self.controller.start_route_next_fly()).pack(anchor="w", pady=(16, 0))

        vial_card, vial_body = self._card(right, "Vial loading state")
        vial_card.pack(fill="x", pady=(18, 0))
        self._build_vial_cards(vial_body, "loading")
        return page

    def _build_assay_page(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=PALETTE.background)
        page.columnconfigure(0, weight=4, minsize=860)
        page.columnconfigure(1, weight=3, minsize=420)
        page.rowconfigure(0, weight=1)

        left = tk.Frame(page, bg=PALETTE.background)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._image_panel(left, "assay_main", "Assay preview", width=980, height=620).pack(fill="both", expand=True)

        right = tk.Frame(page, bg=PALETTE.background)
        right.grid(row=0, column=1, sticky="nsew")
        setup_card, setup_body = self._card(right, "Assay operations", "Profiles, background, calibration, recording, processing, and upload are all wired to the updated fin6 workflow.")
        setup_card.pack(fill="x")
        self.assay_profile_text_var = tk.StringVar(value="")
        self.assay_bg_var = tk.StringVar(value="")
        self.assay_cal_var = tk.StringVar(value="")
        self.assay_run_var = tk.StringVar(value="No assay yet")
        self.assay_duration_text_var = tk.StringVar(value="")
        self.assay_analysis_fps_text_var = tk.StringVar(value="")
        self.assay_threshold_text_var = tk.StringVar(value="")
        self.assay_box_text_var = tk.StringVar(value="")
        for title, var in [
            ("Profile", self.assay_profile_text_var),
            ("Background", self.assay_bg_var),
            ("Calibration", self.assay_cal_var),
            ("Last run", self.assay_run_var),
            ("Duration", self.assay_duration_text_var),
            ("Analysis fps", self.assay_analysis_fps_text_var),
            ("Threshold", self.assay_threshold_text_var),
            ("Box upload", self.assay_box_text_var),
        ]:
            row = tk.Frame(setup_body, bg=PALETTE.surface_low)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=title, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=LABEL_FONT, width=14, anchor="w").pack(side="left")
            tk.Label(row, textvariable=var, bg=PALETTE.surface_low, fg=PALETTE.text, font=BODY_FONT, anchor="w", wraplength=340, justify="left").pack(side="left", fill="x", expand=True)

        actions = tk.Frame(setup_body, bg=PALETTE.surface_low)
        actions.pack(fill="x", pady=(14, 0))
        self._action_button(actions, "Capture Preview", lambda: self.controller.start_capture_assay_preview()).grid(row=0, column=0, padx=(0, 8), pady=6)
        self._action_button(actions, "Capture Background", lambda: self.controller.start_capture_assay_background(), primary=False).grid(row=0, column=1, padx=8, pady=6)
        self._action_button(actions, "Restore Previous", lambda: self.controller.start_restore_assay_background(), primary=False).grid(row=0, column=2, padx=8, pady=6)
        self._action_button(actions, "Calibrate", lambda: self.controller.start_calibrate_assay(), primary=False).grid(row=1, column=0, padx=(0, 8), pady=6)
        self._action_button(actions, "Run Assay", lambda: self.controller.start_run_assay()).grid(row=1, column=1, padx=8, pady=6)
        self._action_button(actions, "Process Last", lambda: self.controller.start_process_last_assay(), primary=False).grid(row=1, column=2, padx=8, pady=6)
        self._action_button(actions, "Upload Last", lambda: self.controller.start_upload_last_run(), primary=False).grid(row=2, column=0, padx=(0, 8), pady=6)

        vial_card, vial_body = self._card(right, "Assay destinations")
        vial_card.pack(fill="x", pady=(18, 0))
        self._build_vial_cards(vial_body, "assay")
        return page

    def _build_results_page(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=PALETTE.background)
        page.columnconfigure(0, weight=4, minsize=860)
        page.columnconfigure(1, weight=3, minsize=420)
        page.rowconfigure(0, weight=1)

        left = tk.Frame(page, bg=PALETTE.background)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._image_panel(left, "results_preview", "Last assay / processed preview", width=980, height=560).pack(fill="both", expand=True)

        right = tk.Frame(page, bg=PALETTE.background)
        right.grid(row=0, column=1, sticky="nsew")
        summary_card, summary_body = self._card(
            right,
            "Results summary",
            "Run the assay from the Assay tab, then process the last run. Once processing finishes, the processed folder, summary CSV, and PDF appear here.",
        )
        summary_card.pack(fill="x")
        self.results_run_var = tk.StringVar(value="--")
        self.results_processed_var = tk.StringVar(value="--")
        self.results_crossings_var = tk.StringVar(value="0")
        self.results_processed_dir_var = tk.StringVar(value="--")
        self.results_summary_csv_var = tk.StringVar(value="--")
        self.results_pdf_var = tk.StringVar(value="--")
        self.results_upload_var = tk.StringVar(value="--")
        for title, var in [
            ("Run directory", self.results_run_var),
            ("Processed", self.results_processed_var),
            ("Processed folder", self.results_processed_dir_var),
            ("Summary CSV", self.results_summary_csv_var),
            ("Unique crossings", self.results_crossings_var),
            ("PDF", self.results_pdf_var),
            ("Upload", self.results_upload_var),
        ]:
            row = tk.Frame(summary_body, bg=PALETTE.surface_low)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=title, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=LABEL_FONT, width=16, anchor="w").pack(side="left")
            tk.Label(row, textvariable=var, bg=PALETTE.surface_low, fg=PALETTE.text, font=BODY_FONT, anchor="w", wraplength=320, justify="left").pack(side="left", fill="x", expand=True)

        btns = tk.Frame(summary_body, bg=PALETTE.surface_low)
        btns.pack(fill="x", pady=(12, 0))
        self._action_button(btns, "Open Run Folder", self._open_last_run, primary=False).pack(side="left", padx=(0, 8))
        self._action_button(btns, "Open Processed Folder", self._open_processed_dir, primary=False).pack(side="left", padx=8)
        self._action_button(btns, "Open Summary CSV", self._open_summary_csv, primary=False).pack(side="left", padx=8)
        self._action_button(btns, "Open PDF", self._open_last_pdf, primary=False).pack(side="left", padx=8)

        vial_card, vial_body = self._card(right, "Per-vial summary")
        vial_card.pack(fill="x", pady=(18, 0))
        self._build_vial_cards(vial_body, "results")
        return page

    def _build_debug_page(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=PALETTE.background)
        scroll = ScrollableFrame(page, bg=PALETTE.background)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        body.columnconfigure(0, weight=1)

        note_card, note_body = self._card(body, "Debug / Advanced", "This is the containment zone for dense controls, manual recovery, camera paths, settings, logs, raw state, and tuning.")
        note_card.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        tk.Label(note_body, text="Use these controls for troubleshooting, validation, calibration recovery, direct hardware actions, and persistent settings changes.", bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT).pack(anchor="w")

        motion_card, motion_body = self._card(body, "Manual motion + device control")
        motion_card.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        row1 = tk.Frame(motion_body, bg=PALETTE.surface_low)
        row1.pack(fill="x", pady=(0, 10))
        self._action_button(row1, "Home", lambda: self._run_debug_action(self.controller.move_home_debug)).pack(side="left", padx=(0, 8))
        self._action_button(row1, "Channel Cam", lambda: self._run_debug_action(self.controller.move_absolute_debug, self.controller.settings.channel_camera_position_mm), primary=False).pack(side="left", padx=8)
        self._action_button(row1, "Chamber", lambda: self._run_debug_action(self.controller.move_absolute_debug, self.controller.settings.chamber_position_mm), primary=False).pack(side="left", padx=8)
        for vial in self.controller.settings.vial_definitions:
            self._action_button(row1, vial.vial_id, lambda pos=vial.position_mm: self._run_debug_action(self.controller.move_absolute_debug, pos), primary=False, width=10).pack(side="left", padx=8)

        row2 = tk.Frame(motion_body, bg=PALETTE.surface_low)
        row2.pack(fill="x", pady=(0, 10))
        tk.Label(row2, text="Move relative (mm)", bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=LABEL_FONT).pack(side="left")
        ttk.Entry(row2, textvariable=self.debug_move_var, width=10).pack(side="left", padx=8)
        self._action_button(row2, "Go", lambda: self._run_debug_relative(), primary=False, width=8).pack(side="left", padx=8)
        for delta in (-20, -5, 5, 20):
            self._action_button(row2, f"{delta:+}", lambda d=delta: self._run_debug_action(self.controller.move_relative_debug, d), primary=False, width=8).pack(side="left", padx=6)

        row3 = tk.Frame(motion_body, bg=PALETTE.surface_low)
        row3.pack(fill="x")
        self._action_button(row3, "Vacuum ON", lambda: self._run_debug_action(self.controller.vacuum_on_debug), primary=False).pack(side="left", padx=(0, 8))
        self._action_button(row3, "Vacuum OFF", lambda: self._run_debug_action(self.controller.vacuum_off_debug), primary=False).pack(side="left", padx=8)
        self._action_button(row3, "Pulse Vibration", lambda: self._run_debug_action(self.controller.pulse_vibration_debug), primary=False).pack(side="left", padx=8)
        self._action_button(row3, "Outputs OFF", lambda: self._run_debug_action(self.controller.outputs_off_debug), primary=False).pack(side="left", padx=8)
        self._action_button(row3, "Reset Vial Counts", lambda: self._run_debug_action(self.controller.reset_vial_counts), primary=False).pack(side="left", padx=8)

        recovery_card, recovery_body = self._card(body, "Manual recovery")
        recovery_card.grid(row=2, column=0, sticky="ew", pady=(0, 18))
        tk.Label(recovery_body, text="If a fly remains in the chamber after an uncertain classification or interrupted cycle, use one of these recovery actions.", bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=BODY_FONT, wraplength=900, justify="left").pack(anchor="w", pady=(0, 10))
        recover_row = tk.Frame(recovery_body, bg=PALETTE.surface_low)
        recover_row.pack(fill="x")
        for vial in self.controller.settings.vial_definitions:
            self._action_button(recover_row, f"Send to {vial.vial_id}", lambda vid=vial.vial_id: self._run_debug_action(self.controller.manual_route_from_chamber, vid), primary=False, width=14).pack(side="left", padx=(0, 8))

        tuning_card, tuning_body = self._card(body, "Channel + loading tuning", "Adjust pickup offset, pickup/drop timing, release settle, channel approach position, chamber clear offset, and the channel calibration length here. Save applies immediately and persists to operator_settings.json.")
        tuning_card.grid(row=3, column=0, sticky="ew", pady=(0, 18))
        grid = tk.Frame(tuning_body, bg=PALETTE.surface_low)
        grid.pack(fill="x")
        for idx, (label, var) in enumerate([
            ("Pickup offset (mm)", self.tuning_pickup_offset_var),
            ("Pickup hold (s)", self.tuning_pick_delay_var),
            ("Drop hold (s)", self.tuning_drop_delay_var),
            ("Release settle (s)", self.tuning_release_delay_var),
            ("Classification delay (s)", self.tuning_classification_delay_var),
            ("Channel cam pos (mm)", self.tuning_channel_camera_pos_var),
            ("Chamber clear offset (mm)", self.tuning_chamber_clear_offset_var),
            ("Channel length (mm)", self.tuning_channel_mm_var),
        ]):
            row = tk.Frame(grid, bg=PALETTE.surface_low)
            row.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=(0 if idx % 2 == 0 else 10, 0), pady=6)
            tk.Label(row, text=label, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=LABEL_FONT).pack(anchor="w")
            ttk.Entry(row, textvariable=var, width=18).pack(anchor="w", pady=(4, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        btn_row = tk.Frame(tuning_body, bg=PALETTE.surface_low)
        btn_row.pack(fill="x", pady=(12, 0))
        self._action_button(btn_row, "Reload Tuning", self._reload_tuning_vars_from_controller, primary=False, width=14).pack(side="left", padx=(0, 8))
        self._action_button(btn_row, "Save Tuning", self._save_tuning_fields, primary=False, width=14).pack(side="left", padx=8)

        assay_card, assay_body = self._card(body, "Assay tuning + Box", "The updated fin6 assay workflow is now live here. Use these curated controls for common assay/profile changes while leaving the full profile JSON untouched unless you really need it.")
        assay_card.grid(row=4, column=0, sticky="ew", pady=(0, 18))
        assay_grid = tk.Frame(assay_body, bg=PALETTE.surface_low)
        assay_grid.pack(fill="x")
        assay_entries = [
            ("Assay duration (s)", self.assay_duration_var),
            ("Analysis fps", self.assay_analysis_fps_var),
            ("Threshold", self.assay_threshold_var),
            ("Min area", self.assay_min_area_var),
            ("Max area", self.assay_max_area_var),
            ("Box mode", self.assay_box_mode_var),
            ("Box config file", self.assay_box_config_var),
        ]
        for idx, (label, var) in enumerate(assay_entries):
            row = tk.Frame(assay_grid, bg=PALETTE.surface_low)
            row.grid(row=idx, column=0, sticky="ew", pady=4)
            tk.Label(row, text=label, bg=PALETTE.surface_low, fg=PALETTE.text_muted, font=LABEL_FONT).pack(anchor="w")
            ttk.Entry(row, textvariable=var, width=56).pack(anchor="w", pady=(4, 0))
        toggles = tk.Frame(assay_body, bg=PALETTE.surface_low)
        toggles.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(toggles, text="Box upload enabled", variable=self.assay_box_enabled_var).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(toggles, text="Auto upload after processing", variable=self.assay_box_auto_upload_var).pack(side="left", padx=18)
        assay_btns = tk.Frame(assay_body, bg=PALETTE.surface_low)
        assay_btns.pack(fill="x", pady=(12, 0))
        self._action_button(assay_btns, "Reload Assay Tuning", self._reload_assay_tuning_vars_from_controller, primary=False, width=18).pack(side="left", padx=(0, 8))
        self._action_button(assay_btns, "Save Assay Tuning", self._save_assay_tuning_fields, primary=False, width=16).pack(side="left", padx=8)
        self._action_button(assay_btns, "Seed Box Templates", self._seed_box_templates, primary=False, width=16).pack(side="left", padx=8)

        model_card, model_body = self._card(body, "Models + profiles")
        model_card.grid(row=5, column=0, sticky="ew", pady=(0, 18))
        model_row = tk.Frame(model_body, bg=PALETTE.surface_low)
        model_row.pack(fill="x", pady=(0, 10))
        ttk.Entry(model_row, textvariable=self.debug_model_var, width=70).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._action_button(model_row, "Browse", self._browse_model, primary=False, width=10).pack(side="left", padx=8)
        self._action_button(model_row, "Reload Model", self._reload_model, primary=False, width=14).pack(side="left", padx=8)

        combo_row = tk.Frame(model_body, bg=PALETTE.surface_low)
        combo_row.pack(fill="x")
        self.profile_combo = ttk.Combobox(combo_row, textvariable=self.debug_profile_var, values=self.controller.assay.list_profiles(), width=40)
        self.profile_combo.pack(side="left", padx=(0, 8))
        self._action_button(combo_row, "Load Profile", self._load_profile_from_debug, primary=False, width=14).pack(side="left", padx=8)

        settings_card, settings_body = self._card(body, "Settings JSON", "Every persisted operator setting can be edited here. Save applies immediately and writes to stitch_operator/operator_settings.json.")
        settings_card.grid(row=6, column=0, sticky="nsew", pady=(0, 18))
        settings_btns = tk.Frame(settings_body, bg=PALETTE.surface_low)
        settings_btns.pack(fill="x", pady=(0, 10))
        self._action_button(settings_btns, "Reload from Disk", self._reload_settings_json, primary=False, width=16).pack(side="left", padx=(0, 8))
        self._action_button(settings_btns, "Save + Apply", self._save_settings_json, primary=False, width=14).pack(side="left", padx=8)
        self.debug_settings_text = scrolledtext.ScrolledText(settings_body, height=18, font=MONO_FONT, wrap="none")
        self.debug_settings_text.pack(fill="both", expand=True)
        self.debug_settings_text.insert("1.0", self.controller.settings_json())

        state_card, state_body = self._card(body, "Raw workflow state")
        state_card.grid(row=7, column=0, sticky="nsew", pady=(0, 18))
        self.debug_state_text = scrolledtext.ScrolledText(state_body, height=18, font=MONO_FONT, wrap="none")
        self.debug_state_text.pack(fill="both", expand=True)

        log_card, log_body = self._card(body, "Logs")
        log_card.grid(row=8, column=0, sticky="nsew")
        self.debug_log_text = scrolledtext.ScrolledText(log_body, height=16, font=MONO_FONT, wrap="word")
        self.debug_log_text.pack(fill="both", expand=True)
        return page

    def _browse_model(self):
        path = filedialog.askopenfilename(title="Select sexing model", filetypes=[("PyTorch model", "*.pt"), ("All files", "*.*")])
        if path:
            self.debug_model_var.set(path)

    def _reload_model(self):
        self.controller.set_model_path(self.debug_model_var.get())
        self.footer_var.set("Sexing model reloaded.")

    def _load_profile_from_debug(self):
        profile = self.debug_profile_var.get().strip()
        if not profile:
            return
        try:
            self.controller.set_active_profile(profile)
            self.profile_combo.configure(values=self.controller.assay.list_profiles())
            self.footer_var.set(f"Active profile: {profile}")
            self._reload_settings_editor_from_controller()
            self._reload_assay_tuning_vars_from_controller()
        except Exception as exc:
            messagebox.showerror("Profile error", str(exc))

    def _reload_settings_editor_from_controller(self):
        settings_json = self.controller.settings_json()
        self.debug_settings_text.delete("1.0", "end")
        self.debug_settings_text.insert("1.0", settings_json)

    def _reload_tuning_vars_from_controller(self):
        settings = self.controller.settings
        self.tuning_pickup_offset_var.set(f"{float(settings.pickup_offset_mm):.2f}")
        self.tuning_pick_delay_var.set(f"{float(settings.vacuum_pick_delay_s):.2f}")
        self.tuning_drop_delay_var.set(f"{float(settings.vacuum_drop_delay_s):.2f}")
        self.tuning_release_delay_var.set(f"{float(getattr(settings, 'vacuum_release_settle_s', 0.25)):.2f}")
        self.tuning_classification_delay_var.set(f"{float(settings.classification_delay_s):.2f}")
        self.tuning_channel_camera_pos_var.set(f"{float(settings.channel_camera_position_mm):.2f}")
        self.tuning_chamber_clear_offset_var.set(f"{float(settings.chamber_clear_offset_mm):.2f}")
        self.tuning_channel_mm_var.set(f"{float(settings.channel_mm):.2f}")

    def _save_tuning_fields(self):
        try:
            patch = {
                "pickup_offset_mm": float(self.tuning_pickup_offset_var.get()),
                "vacuum_pick_delay_s": float(self.tuning_pick_delay_var.get()),
                "vacuum_drop_delay_s": float(self.tuning_drop_delay_var.get()),
                "vacuum_release_settle_s": float(self.tuning_release_delay_var.get()),
                "classification_delay_s": float(self.tuning_classification_delay_var.get()),
                "channel_camera_position_mm": float(self.tuning_channel_camera_pos_var.get()),
                "chamber_clear_offset_mm": float(self.tuning_chamber_clear_offset_var.get()),
                "channel_mm": float(self.tuning_channel_mm_var.get()),
            }
        except ValueError:
            messagebox.showerror("Tuning error", "All tuning fields must be numeric.")
            return
        try:
            self.controller.patch_settings_fields(**patch)
            self._reload_settings_editor_from_controller()
            self._reload_tuning_vars_from_controller()
            self.footer_var.set("Channel / loading tuning saved.")
        except Exception as exc:
            messagebox.showerror("Tuning error", str(exc))

    def _reload_assay_tuning_vars_from_controller(self):
        summary = self.controller.assay_profile_summary()
        self.assay_duration_var.set(f"{float(summary.get('assay_duration_s', 10.0)):.2f}")
        self.assay_analysis_fps_var.set(f"{float(summary.get('analysis_fps', 5.0)):.2f}")
        self.assay_threshold_var.set(f"{float(summary.get('detector_min_threshold', 12.0)):.2f}")
        self.assay_min_area_var.set(str(int(summary.get('detector_min_area', 10))))
        self.assay_max_area_var.set(str(int(summary.get('detector_max_area', 250))))
        self.assay_box_mode_var.set(str(summary.get('box_artifact_mode', 'summaries') or 'summaries'))
        self.assay_box_config_var.set(str(summary.get('box_config_file', '') or ''))
        self.assay_box_enabled_var.set(bool(summary.get('box_enabled', False)))
        self.assay_box_auto_upload_var.set(bool(summary.get('box_auto_upload_processing', False)))

    def _save_assay_tuning_fields(self):
        try:
            patch = {
                "assay_duration_s": float(self.assay_duration_var.get()),
                "analysis_fps": float(self.assay_analysis_fps_var.get()),
                "detector_min_threshold": float(self.assay_threshold_var.get()),
                "detector_min_area": int(float(self.assay_min_area_var.get())),
                "detector_max_area": int(float(self.assay_max_area_var.get())),
                "box_enabled": bool(self.assay_box_enabled_var.get()),
                "box_artifact_mode": self.assay_box_mode_var.get().strip() or 'summaries',
                "box_config_file": self.assay_box_config_var.get().strip(),
                "box_upload_after_processing": bool(self.assay_box_auto_upload_var.get()),
            }
        except ValueError:
            messagebox.showerror("Assay tuning error", "Assay tuning fields must contain valid numeric values.")
            return
        try:
            self.controller.patch_assay_profile_fields(**patch)
            self._reload_assay_tuning_vars_from_controller()
            self.footer_var.set("Assay tuning saved.")
        except Exception as exc:
            messagebox.showerror("Assay tuning error", str(exc))

    def _seed_box_templates(self):
        try:
            result = self.controller.seed_box_templates(overwrite=True)
            self._reload_assay_tuning_vars_from_controller()
            config_file = result.get("config_file", "")
            self.footer_var.set(f"Box templates refreshed: {config_file}")
        except Exception as exc:
            messagebox.showerror("Box setup error", str(exc))

    def _save_settings_json(self):
        try:
            self.controller.save_settings_json(self.debug_settings_text.get("1.0", "end-1c"))
            self.profile_combo.configure(values=self.controller.assay.list_profiles())
            self._reload_settings_editor_from_controller()
            self._reload_tuning_vars_from_controller()
            self._reload_assay_tuning_vars_from_controller()
            self.footer_var.set("Operator settings saved.")
        except Exception as exc:
            messagebox.showerror("Settings error", str(exc))

    def _reload_settings_json(self):
        try:
            self.controller.reload_settings_from_disk()
            self.profile_combo.configure(values=self.controller.assay.list_profiles())
            self._reload_settings_editor_from_controller()
            self._reload_tuning_vars_from_controller()
            self._reload_assay_tuning_vars_from_controller()
            self.footer_var.set("Operator settings reloaded.")
        except Exception as exc:
            messagebox.showerror("Settings error", str(exc))

    def _run_debug_relative(self):
        try:
            delta = float(self.debug_move_var.get())
        except ValueError:
            messagebox.showerror("Invalid value", "Enter a numeric relative move in millimetres.")
            return
        self._run_debug_action(self.controller.move_relative_debug, delta)

    def _run_debug_action(self, func, *args):
        snapshot = self.controller.snapshot()
        if snapshot.busy:
            messagebox.showwarning("Busy", "Wait for the current automated task to finish before using manual controls.")
            return

        def worker():
            try:
                func(*args)
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Debug action failed", str(exc)))
            finally:
                self.controller.periodic_refresh()

        threading.Thread(target=worker, daemon=True).start()

    def _open_path(self, path_text: str):
        if not path_text:
            return
        path = Path(path_text)
        if not path.exists():
            messagebox.showerror("Missing file", f"Path does not exist: {path}")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def _open_last_run(self):
        self._open_path(self.controller.snapshot().assay.run_dir)

    def _open_processed_dir(self):
        self._open_path(self.controller.snapshot().assay.processed_dir)

    def _open_summary_csv(self):
        self._open_path(self.controller.snapshot().assay.summary_csv_path)

    def _open_last_pdf(self):
        self._open_path(self.controller.snapshot().assay.pdf_path)

    def _confirm_reset_vial_counts(self):
        if not messagebox.askyesno(
            "Prepare new run",
            "Reset all vial counts and mark the operator console ready for a new run?",
        ):
            return
        self._run_debug_action(self.controller.reset_vial_counts)

    def _refresh_workflow_log(self, snapshot: OperatorState):
        if not hasattr(self, "workflow_log_text"):
            return
        lines = snapshot.recent_logs[-60:]
        text = "\n".join(lines) if lines else "No workflow log entries yet."
        current = self.workflow_log_text.get("1.0", "end-1c")
        if current == text:
            return
        self.workflow_log_text.configure(state="normal")
        self.workflow_log_text.delete("1.0", "end")
        self.workflow_log_text.insert("1.0", text)
        self.workflow_log_text.see("end")
        self.workflow_log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # polling + refresh
    # ------------------------------------------------------------------
    def _poll_events(self):
        while True:
            try:
                event = self.controller.events.get_nowait()
            except Exception:
                break
            if event.get("type") == "choice":
                dialog = ChoiceDialog(
                    self,
                    event.get("title", "Choose"),
                    event.get("message", ""),
                    list(event.get("options", [])),
                    timeout_s=event.get("timeout_s"),
                )
                self.wait_window(dialog)
                self.controller.resolve_choice(str(event.get("request_id")), dialog.result)
        self.after(150, self._poll_events)

    def _periodic_controller_refresh(self):
        self.controller.periodic_refresh()
        self.after(1000, self._periodic_controller_refresh)

    def _refresh_ui(self):
        snapshot = self.controller.snapshot()
        self._refresh_status(snapshot)
        self._refresh_images(snapshot)
        self._refresh_vials(snapshot)
        self._refresh_workflow_log(snapshot)
        self._refresh_debug(snapshot)
        self.after(self._ui_refresh_ms, self._refresh_ui)

    def _refresh_status(self, snapshot: OperatorState):
        self.status_stage_var.set(snapshot.stage_label)
        self.status_message_var.set(snapshot.status_message)
        self.status_next_var.set(snapshot.next_action)
        self.status_profile_var.set(f"Profile: {snapshot.readiness.active_profile or '—'}")
        self.status_homed_var.set("Homed" if snapshot.readiness.homed else "Not homed")
        self.status_model_var.set("Model loaded" if snapshot.readiness.model_ready else "Model missing")
        self.status_channel_camera_var.set(snapshot.readiness.channel_camera)
        self.status_assay_camera_var.set(snapshot.readiness.assay_camera)
        self.footer_var.set(snapshot.status_message)
        self.uptime_var.set(time.strftime("%H:%M:%S", time.gmtime(snapshot.uptime_seconds())))

        self.workflow_stage_value.set(snapshot.stage_label)
        self.workflow_message_value.set(snapshot.status_message)
        self.workflow_next_value.set(snapshot.next_action)
        self.workflow_profile_var.set(snapshot.readiness.active_profile or "—")
        self.workflow_homed_var.set("Homed" if snapshot.readiness.homed else "Initialize required")
        self.workflow_model_var.set("Loaded" if snapshot.readiness.model_ready else "Missing")
        self.workflow_channel_camera_var.set(snapshot.readiness.channel_camera)
        self.workflow_assay_camera_var.set(snapshot.readiness.assay_camera)
        self.workflow_channel_count_var.set(str(snapshot.channel.count))
        self.workflow_current_fly_var.set(snapshot.current_target or "--")
        self.workflow_last_sex_var.set(
            snapshot.sexing.label.title() if snapshot.sexing.label not in {"--", "UNCERTAIN"} else (snapshot.sexing.label or "--")
        )
        self.workflow_destination_var.set(snapshot.selected_destination or "--")

        live_lines = [snapshot.status_message]
        detail = str(snapshot.sexing.detail or "").strip()
        if detail and detail not in {"Awaiting next specimen.", "Classifying the next specimen..."}:
            live_lines.append(detail)
        for entry in snapshot.recent_logs[-3:]:
            if entry not in live_lines:
                live_lines.append(entry)
        live_text = "\n".join(live_lines[-4:])
        self.workflow_live_var.set(live_text)
        self.workflow_hint_var.set(live_text)

        self.channel_bg_var.set("Ready" if snapshot.readiness.channel_background_ready else "Missing")
        self.channel_cal_var.set("Ready" if snapshot.readiness.channel_calibration_ready else "Missing")
        self.channel_count_var.set(str(snapshot.channel.count))
        self.channel_positions_var.set(", ".join(f"{value:.1f}" for value in snapshot.channel.x_positions_mm[:8]) or "--")
        self.channel_hint_var.set("Channel result is current." if not snapshot.channel.stale else "Capture the channel again to refresh positions.")

        self.loading_sex_var.set(snapshot.sexing.label.title() if snapshot.sexing.label not in {"--", "UNCERTAIN"} else snapshot.sexing.label)
        self.loading_conf_var.set(f"{snapshot.sexing.confidence * 100:.0f}%")
        self.loading_dest_var.set(snapshot.selected_destination or "--")
        self.loading_detail_var.set(snapshot.sexing.detail or "Route the next fly to refresh the sexing result.")

        self.assay_profile_text_var.set(snapshot.readiness.active_profile or "—")
        self.assay_bg_var.set("Ready" if snapshot.readiness.assay_background_ready else "Missing")
        self.assay_cal_var.set("Ready" if snapshot.readiness.assay_calibration_ready else "Missing")
        self.assay_run_var.set(snapshot.assay.run_dir or "No assay yet")
        assay_summary = self.controller.assay_profile_summary()
        self.assay_duration_text_var.set(f"{float(assay_summary.get('assay_duration_s', 0.0)):.1f} s")
        self.assay_analysis_fps_text_var.set(f"{float(assay_summary.get('analysis_fps', 0.0)):.1f}")
        self.assay_threshold_text_var.set(f"{float(assay_summary.get('detector_min_threshold', 0.0)):.1f}")
        box_bits = []
        box_bits.append("Enabled" if assay_summary.get('box_enabled') else "Off")
        if assay_summary.get('box_enabled'):
            box_bits.append(str(assay_summary.get('box_artifact_mode', 'summaries')))
            if assay_summary.get('box_auto_upload_processing'):
                box_bits.append('auto after processing')
        legacy_source = str(assay_summary.get('box_legacy_source', '') or '')
        if legacy_source:
            box_bits.append(f"legacy: {legacy_source}")
        self.assay_box_text_var.set(" · ".join(box_bits))

        self.results_run_var.set(snapshot.assay.run_dir or "—")
        self.results_processed_var.set(snapshot.assay.processed_at or "—")
        self.results_processed_dir_var.set(snapshot.assay.processed_dir or "—")
        self.results_summary_csv_var.set(snapshot.assay.summary_csv_path or "—")
        self.results_crossings_var.set(str(snapshot.assay.unique_crossings_total))
        self.results_pdf_var.set(snapshot.assay.pdf_path or "—")
        self.results_upload_var.set(snapshot.assay.upload_status or "—")

    def _refresh_images(self, snapshot: OperatorState):
        channel_scene = snapshot.channel.annotated_image_path or snapshot.channel.raw_image_path
        page_images = {
            "Channel": [
                ("channel_main", channel_scene, "No channel preview available."),
            ],
            "Loading / Sexing": [
                ("loading_sexing", snapshot.sexing.image_path, "No sexing preview available."),
            ],
            "Assay": [
                ("assay_main", snapshot.assay.preview_image_path, "Capture an assay preview or run an assay."),
            ],
            "Results": [
                ("results_preview", snapshot.assay.preview_image_path, "Process an assay to populate results previews."),
            ],
        }
        for key, path_text, placeholder in page_images.get(self._active_page, []):
            self._update_image_widget(key, path_text, placeholder)

    def _refresh_vials(self, snapshot: OperatorState):
        for key in ["workflow", "loading", "assay", "results"]:
            widgets = self._vial_widgets.get(key, [])
            for vial, widget in zip(snapshot.vials, widgets):
                widget["title"].configure(text=vial.label)
                display_sex = "Junk" if str(vial.target_sex).lower() == "junk" else str(vial.target_sex).title()
                widget["sex"].configure(text=display_sex)
                widget["count"].configure(text=f"{vial.current_count} / {vial.max_count}")
                widget["status"].configure(text=vial.status, bg=PALETTE.secondary_fixed if vial.status != "FULL" else PALETTE.error_container)
                if key == "results":
                    metric_text = self._result_metric_text(snapshot, vial)
                elif str(vial.target_sex).lower() == "junk":
                    metric_text = "Ignored in assay" if key == "results" else "Junk / recovery"
                else:
                    metric_text = "Available"
                widget["metric"].configure(text=metric_text)

    def _result_metric_text(self, snapshot: OperatorState, vial: VialState) -> str:
        if str(vial.target_sex).lower() == "junk":
            return "Junk / recovery"
        if not snapshot.assay.per_vial_summary:
            return "No processed metrics yet"
        try:
            vial_index = int(vial.vial_id.replace("V", ""))
        except Exception:
            vial_index = None
        if vial_index is None:
            return "No processed metrics yet"
        for row in snapshot.assay.per_vial_summary:
            try:
                physical_vial_index = int(float(row.get("physical_vial_index", row.get("vial_index", -1))))
            except Exception:
                physical_vial_index = -1
            if physical_vial_index == vial_index:
                flies = row.get("number_of_flies_detected") or row.get("flies_detected") or "0"
                crossings = row.get("number_of_unique_threshold_crossings") or row.get("unique_threshold_crossings") or "0"
                return f"Flies {flies} · Crossings {crossings}"
        return "No processed metrics yet"

    def _refresh_debug(self, snapshot: OperatorState):
        state_json = snapshot.to_debug_json()
        current_json = self.debug_state_text.get("1.0", "end-1c")
        if current_json != state_json:
            self.debug_state_text.delete("1.0", "end")
            self.debug_state_text.insert("1.0", state_json)
        if len(snapshot.recent_logs) != self._last_log_count:
            self.debug_log_text.delete("1.0", "end")
            self.debug_log_text.insert("1.0", "\n".join(snapshot.recent_logs))
            self.debug_log_text.see("end")
            self._last_log_count = len(snapshot.recent_logs)
        self.debug_model_var.set(self.controller.settings.sexing_model_path)
        self.debug_profile_var.set(self.controller.assay.profile.name)
        self.profile_combo.configure(values=self.controller.assay.list_profiles())

    def _on_close(self):
        try:
            self.controller.stop_current_task()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = OperatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
