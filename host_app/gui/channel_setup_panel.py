from __future__ import annotations

import io
import threading
import tkinter as tk
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from tkinter import messagebox, ttk


@dataclass
class ChannelSetupActions:
    fetch_status: Callable[[], Any]
    capture_background: Callable[[], dict[str, Any]]
    save_calibration: Callable[[tuple[int, int], tuple[int, int], float], dict[str, Any]]
    fetch_background_bytes: Callable[[], bytes | None]


class ChannelSetupPanel:
    def __init__(
        self,
        parent: tk.Misc,
        *,
        actions: ChannelSetupActions,
        on_ready: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        status_callback: Callable[[str, str], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.parent = parent
        self.actions = actions
        self.on_ready = on_ready
        self.on_cancel = on_cancel
        self.status_callback = status_callback
        self.log_callback = log_callback

        self.window = tk.Toplevel(parent)
        self.window.title("Channel Detection Setup")
        self.window.resizable(True, True)
        self.window.minsize(760, 620)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self._handle_cancel)

        self.status_var = tk.StringVar(value="Loading Channel Detection Setup...")
        self.detail_var = tk.StringVar(
            value=(
                "Empty the channel, move the nozzle out of the camera view, "
                "capture a clean background, click the left and right channel ends, and save."
            )
        )
        self.background_state_var = tk.StringVar(value="Background: checking")
        self.calibration_state_var = tk.StringVar(value="Calibration: checking")
        self.selection_var = tk.StringVar(value="Pick the left channel end, then the right channel end.")
        self.channel_mm_var = tk.StringVar(value="111.0")

        self._photo = None
        self._pil_image = None
        self._display_box: tuple[int, int, int, int] | None = None
        self._selected_points: list[tuple[int, int]] = []
        self._busy = False
        self._closed = False

        self._build_ui()
        self._focus_window()
        self.refresh_status_and_preview()

    def show(self) -> None:
        self._focus_window()

    def is_open(self) -> bool:
        return not self._closed and bool(self.window.winfo_exists())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def refresh_status_and_preview(self) -> None:
        self._run_worker("Loading Channel Detection Setup...", self._load_status_payload, self._apply_status_payload)

    def _build_ui(self) -> None:
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)

        root = ttk.Frame(self.window, padding=14)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        ttk.Label(root, text="Channel Detection Setup", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(root, textvariable=self.detail_var, wraplength=760, justify=tk.LEFT).grid(row=1, column=0, sticky="ew", pady=(6, 12))

        content = ttk.Frame(root)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=5)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        left = ttk.Frame(content, padding=(0, 0, 8, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        left.grid(row=0, column=0, sticky="nsew")

        status_frame = ttk.LabelFrame(left, text="Setup State", padding=10)
        status_frame.grid(row=0, column=0, sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(status_frame, text="Status:").grid(row=0, column=0, sticky="w")
        ttk.Label(status_frame, textvariable=self.status_var, wraplength=520, justify=tk.LEFT).grid(row=0, column=1, sticky="ew")
        ttk.Label(status_frame, text="Background:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.background_state_var, wraplength=520, justify=tk.LEFT).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(status_frame, text="Calibration:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.calibration_state_var, wraplength=520, justify=tk.LEFT).grid(row=2, column=1, sticky="ew", pady=(8, 0))

        preview_frame = ttk.LabelFrame(left, text="Background Preview", padding=10)
        preview_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(preview_frame, background="#111111", highlightthickness=0, width=760, height=420)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Button-1>", self._handle_canvas_click)
        self.canvas.bind("<Configure>", self._render_preview)

        right = ttk.Frame(content)
        right.columnconfigure(0, weight=1)
        right.grid(row=0, column=1, sticky="nsew")

        instructions_frame = ttk.LabelFrame(right, text="Operator Steps", padding=10)
        instructions_frame.grid(row=0, column=0, sticky="new")
        ttk.Label(
            instructions_frame,
            text=(
                "1. Empty the channel.\n"
                "2. Move the nozzle out of the camera view.\n"
                "3. Capture a clean background.\n"
                "4. Click the left channel end, then the right channel end.\n"
                "5. Save the setup."
            ),
            justify=tk.LEFT,
            wraplength=260,
        ).grid(row=0, column=0, sticky="w")

        controls = ttk.LabelFrame(right, text="Controls", padding=10)
        controls.grid(row=1, column=0, sticky="new", pady=(12, 0))
        controls.columnconfigure(0, weight=1)
        ttk.Label(controls, text="Channel length (mm)").grid(row=0, column=0, sticky="w")
        self.channel_mm_entry = ttk.Entry(controls, textvariable=self.channel_mm_var, width=18)
        self.channel_mm_entry.grid(row=1, column=0, sticky="ew", pady=(4, 10))

        self.capture_button = ttk.Button(controls, text="Capture Background", command=self._capture_background)
        self.capture_button.grid(row=2, column=0, sticky="ew")
        self.reset_points_button = ttk.Button(controls, text="Reset Point Selection", command=self._reset_points)
        self.reset_points_button.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.save_button = ttk.Button(controls, text="Save Calibration", command=self._save_calibration)
        self.save_button.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.refresh_button = ttk.Button(controls, text="Refresh", command=self.refresh_status_and_preview)
        self.refresh_button.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        footer = ttk.Frame(right)
        footer.grid(row=2, column=0, sticky="sew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.selection_var, wraplength=280, justify=tk.LEFT).grid(row=0, column=0, sticky="ew")
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self._handle_cancel)
        self.cancel_button.grid(row=1, column=0, sticky="e", pady=(12, 0))

        self._update_control_states()

    def _focus_window(self) -> None:
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            self.window.grab_set()
            self.window.attributes("-topmost", True)
            self.window.after(250, self._clear_topmost)
        except tk.TclError:
            pass

    def _clear_topmost(self) -> None:
        try:
            if self.window.winfo_exists():
                self.window.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _handle_cancel(self) -> None:
        if self._busy:
            messagebox.showwarning(
                "Channel Detection Setup",
                "Wait for the current setup action to finish before closing this window.",
                parent=self.window,
            )
            return
        self.close()
        if callable(self.on_cancel):
            self.on_cancel()

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        if message:
            self.status_var.set(message)
        self._update_control_states()

    def _update_control_states(self) -> None:
        state = tk.DISABLED if self._busy else tk.NORMAL
        entry_state = "disabled" if self._busy else "normal"
        for widget in (self.capture_button, self.reset_points_button, self.save_button, self.refresh_button, self.cancel_button):
            try:
                widget.config(state=state)
            except tk.TclError:
                pass
        try:
            self.channel_mm_entry.config(state=entry_state)
        except tk.TclError:
            pass
        if not self._selected_points:
            try:
                self.save_button.config(state=tk.DISABLED)
            except tk.TclError:
                pass
        elif len(self._selected_points) < 2 or self._busy:
            try:
                self.save_button.config(state=tk.DISABLED)
            except tk.TclError:
                pass

    def _run_worker(self, start_message: str, worker: Callable[[], Any], on_success: Callable[[Any], None]) -> None:
        self._set_busy(True, start_message)

        def runner() -> None:
            try:
                result = worker()
            except Exception as exc:
                self.window.after(0, lambda: self._handle_worker_error(str(exc)))
                return
            self.window.after(0, lambda: on_success(result))

        threading.Thread(target=runner, daemon=True).start()

    def _handle_worker_error(self, message: str) -> None:
        self._set_busy(False, "Channel Detection Setup needs attention.")
        self.detail_var.set(message)
        if callable(self.status_callback):
            self.status_callback("error", message)
        if callable(self.log_callback):
            self.log_callback(f"Channel Detection Setup error: {message}")
        messagebox.showerror("Channel Detection Setup Error", message, parent=self.window)

    def _load_status_payload(self) -> dict[str, Any]:
        status = self.actions.fetch_status()
        image_bytes = self.actions.fetch_background_bytes()
        return {
            "status": status,
            "image_bytes": image_bytes,
        }

    def _apply_status_payload(self, payload: dict[str, Any]) -> None:
        self._set_busy(False)
        status = self._to_namespace(payload["status"])
        image_bytes = payload.get("image_bytes")
        self.background_state_var.set("Saved" if getattr(status, "channel_background_ready", False) else "Missing")
        self.calibration_state_var.set("Saved" if getattr(status, "channel_calibration_ready", False) else "Missing")
        if getattr(status, "channel", None) is not None:
            self.channel_mm_var.set(f"{float(getattr(status.channel, 'channel_mm', 111.0)):.1f}")
        if getattr(status, "channel_background_ready", False):
            self.status_var.set("Background ready. Capture again if you need a new clean reference.")
        else:
            self.status_var.set("Capture a clean channel background to begin setup.")
        if image_bytes:
            self._load_preview_bytes(image_bytes)
        else:
            self._pil_image = None
            self._photo = None
            self.canvas.delete("all")
            self.canvas.create_text(
                max(120, self.canvas.winfo_width() // 2),
                max(80, self.canvas.winfo_height() // 2),
                text="No saved background yet.\nUse Capture Background to create one.",
                fill="#FFFFFF",
                justify=tk.CENTER,
                font=("Segoe UI", 12, "bold"),
            )
        self._render_preview()
        self._update_selection_label()

    def _capture_background(self) -> None:
        self._run_worker(
            "Capturing clean channel background...",
            self.actions.capture_background,
            self._handle_capture_complete,
        )

    def _handle_capture_complete(self, payload: dict[str, Any]) -> None:
        self._selected_points.clear()
        self.selection_var.set("Background saved. Click the left channel end, then the right channel end.")
        camera_description = str(payload.get("camera_description", "")).strip()
        message = "Background captured."
        if camera_description:
            message = f"Background captured from {camera_description}."
        self.status_var.set(message)
        if callable(self.status_callback):
            self.status_callback("running", message)
        if callable(self.log_callback):
            self.log_callback(message)
        self.refresh_status_and_preview()

    def _save_calibration(self) -> None:
        if len(self._selected_points) != 2:
            messagebox.showwarning(
                "Channel Detection Setup",
                "Click the left channel end, then the right channel end before saving calibration.",
                parent=self.window,
            )
            return
        try:
            channel_mm = float(self.channel_mm_var.get())
        except ValueError:
            messagebox.showerror("Channel Detection Setup", "Channel length must be numeric.", parent=self.window)
            return

        left_pt, right_pt = self._selected_points
        self._run_worker(
            "Saving channel calibration...",
            lambda: self.actions.save_calibration(left_pt, right_pt, channel_mm),
            self._handle_save_complete,
        )

    def _handle_save_complete(self, payload: dict[str, Any]) -> None:
        self._set_busy(False, "Channel Detection Setup saved.")
        if callable(self.log_callback):
            self.log_callback("Channel Detection Setup saved.")
        if callable(self.status_callback):
            self.status_callback("running", "Channel Detection Setup saved.")
        self.close()
        if callable(self.on_ready):
            self.on_ready()

    def _load_preview_bytes(self, image_bytes: bytes) -> None:
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes))
            self._pil_image = image.convert("RGB")
        except Exception as exc:
            raise RuntimeError(f"Could not load the channel background preview: {exc}") from exc

    def _render_preview(self, _event=None) -> None:
        self.canvas.delete("all")
        if self._pil_image is None:
            return
        try:
            from PIL import ImageTk
        except Exception:
            return

        canvas_width = max(320, self.canvas.winfo_width())
        canvas_height = max(220, self.canvas.winfo_height())
        image = self._pil_image.copy()
        image.thumbnail((canvas_width, canvas_height))
        offset_x = max(0, (canvas_width - image.width) // 2)
        offset_y = max(0, (canvas_height - image.height) // 2)
        self._display_box = (offset_x, offset_y, image.width, image.height)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(offset_x, offset_y, image=self._photo, anchor=tk.NW)
        for index, point in enumerate(self._selected_points):
            canvas_point = self._image_to_canvas(point)
            if canvas_point is None:
                continue
            x, y = canvas_point
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#00E676", outline="#00E676")
            self.canvas.create_text(x + 12, y - 12, text="L" if index == 0 else "R", fill="#00E676", anchor=tk.SW)
        if len(self._selected_points) == 2:
            left = self._image_to_canvas(self._selected_points[0])
            right = self._image_to_canvas(self._selected_points[1])
            if left and right:
                self.canvas.create_line(left[0], left[1], right[0], right[1], fill="#00E676", width=2)

    def _handle_canvas_click(self, event) -> None:
        if self._busy or self._pil_image is None:
            return
        image_point = self._canvas_to_image((event.x, event.y))
        if image_point is None:
            return
        if len(self._selected_points) >= 2:
            self._selected_points = [image_point]
        else:
            self._selected_points.append(image_point)
        self._update_selection_label()
        self._render_preview()
        self._update_control_states()

    def _reset_points(self) -> None:
        self._selected_points.clear()
        self._update_selection_label()
        self._render_preview()
        self._update_control_states()

    def _update_selection_label(self) -> None:
        if len(self._selected_points) == 0:
            self.selection_var.set("Pick the left channel end, then the right channel end.")
        elif len(self._selected_points) == 1:
            x, y = self._selected_points[0]
            self.selection_var.set(f"Left point saved at ({x}, {y}). Pick the right channel end.")
        else:
            left = self._selected_points[0]
            right = self._selected_points[1]
            self.selection_var.set(
                f"Ready to save. Left=({left[0]}, {left[1]}) Right=({right[0]}, {right[1]})."
            )

    def _canvas_to_image(self, canvas_point: tuple[int, int]) -> tuple[int, int] | None:
        if self._display_box is None or self._pil_image is None:
            return None
        offset_x, offset_y, display_width, display_height = self._display_box
        x, y = canvas_point
        if x < offset_x or y < offset_y or x > offset_x + display_width or y > offset_y + display_height:
            return None
        scale_x = self._pil_image.width / float(display_width)
        scale_y = self._pil_image.height / float(display_height)
        image_x = int(round((x - offset_x) * scale_x))
        image_y = int(round((y - offset_y) * scale_y))
        image_x = max(0, min(self._pil_image.width - 1, image_x))
        image_y = max(0, min(self._pil_image.height - 1, image_y))
        return (image_x, image_y)

    def _image_to_canvas(self, image_point: tuple[int, int]) -> tuple[int, int] | None:
        if self._display_box is None or self._pil_image is None:
            return None
        offset_x, offset_y, display_width, display_height = self._display_box
        scale_x = display_width / float(self._pil_image.width)
        scale_y = display_height / float(self._pil_image.height)
        x = int(round(offset_x + (image_point[0] * scale_x)))
        y = int(round(offset_y + (image_point[1] * scale_y)))
        return (x, y)

    @staticmethod
    def _to_namespace(value: Any) -> Any:
        if isinstance(value, dict):
            return SimpleNamespace(**{key: ChannelSetupPanel._to_namespace(item) for key, item in value.items()})
        if isinstance(value, list):
            return [ChannelSetupPanel._to_namespace(item) for item in value]
        return value
