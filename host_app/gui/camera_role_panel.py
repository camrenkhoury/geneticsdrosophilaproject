from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable
from tkinter import messagebox, ttk


@dataclass
class CameraRoleActions:
    fetch_roles: Callable[[], dict[str, Any]]
    save_roles: Callable[[str, str, int, str, str], dict[str, Any]]


class CameraRolePanel:
    def __init__(
        self,
        parent: tk.Misc,
        *,
        actions: CameraRoleActions,
        on_saved: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.parent = parent
        self.actions = actions
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.log_callback = log_callback

        self.window = tk.Toplevel(parent)
        self.window.title("Camera Roles")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self._handle_cancel)

        self.status_var = tk.StringVar(value="Loading camera roles...")
        self.channel_var = tk.StringVar(value="Auto-detect channel camera")
        self.sexing_var = tk.StringVar(value="Pi Camera 0 | Default ribbon camera")
        self.assay_var = tk.StringVar(value="Auto-detect assay camera")

        self._busy = False
        self._closed = False
        self._channel_map: dict[str, tuple[str, str]] = {"Auto-detect channel camera": ("auto:channel", "")}
        self._assay_map: dict[str, tuple[str, str]] = {"Auto-detect assay camera": ("auto:assay", "")}
        self._sexing_map: dict[str, int] = {"Pi Camera 0 | Default ribbon camera": 0}

        self._build_ui()
        self._focus_window()
        self._load_roles()

    def is_open(self) -> bool:
        return not self._closed and bool(self.window.winfo_exists())

    def show(self) -> None:
        self._focus_window()

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

    def _build_ui(self) -> None:
        root = ttk.Frame(self.window, padding=14)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="Camera Roles", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            root,
            text="Choose which physical camera is used for Channel Detection, Sexing, and Assay.",
            wraplength=520,
            justify=tk.LEFT,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 12))
        ttk.Label(root, textvariable=self.status_var, wraplength=520, justify=tk.LEFT).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12)
        )

        ttk.Label(root, text="Channel camera").grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.channel_combo = ttk.Combobox(root, textvariable=self.channel_var, state="readonly", width=56)
        self.channel_combo.grid(row=3, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(root, text="Sexing camera").grid(row=4, column=0, sticky="w", pady=(0, 6))
        self.sexing_combo = ttk.Combobox(root, textvariable=self.sexing_var, state="readonly", width=56)
        self.sexing_combo.grid(row=4, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(root, text="Assay camera").grid(row=5, column=0, sticky="w", pady=(0, 12))
        self.assay_combo = ttk.Combobox(root, textvariable=self.assay_var, state="readonly", width=56)
        self.assay_combo.grid(row=5, column=1, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(root)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e")
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._handle_cancel)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))
        self.save_button = ttk.Button(buttons, text="Save Camera Roles", command=self._save_roles)
        self.save_button.grid(row=0, column=1)

    def _focus_window(self) -> None:
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            self.window.grab_set()
            self.window.attributes("-topmost", True)
            self.window.after(250, lambda: self.window.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.status_var.set(message)
        combo_state = "disabled" if busy else "readonly"
        button_state = tk.DISABLED if busy else tk.NORMAL
        for combo in (self.channel_combo, self.sexing_combo, self.assay_combo):
            try:
                combo.config(state=combo_state)
            except tk.TclError:
                pass
        try:
            self.save_button.config(state=button_state)
        except tk.TclError:
            pass
        try:
            self.cancel_button.config(state=tk.NORMAL)
        except tk.TclError:
            pass

    def _load_roles(self) -> None:
        self._set_busy(True, "Loading camera roles...")

        def runner() -> None:
            try:
                payload = self.actions.fetch_roles()
            except Exception as exc:
                self.window.after(0, lambda: self._handle_error(str(exc)))
                return
            self.window.after(0, lambda: self._apply_roles(payload))

        threading.Thread(target=runner, daemon=True).start()

    def _apply_roles(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        self._channel_map = self._build_uvc_map(payload.get("channel") or {}, "Auto-detect channel camera", "channel")
        self._assay_map = self._build_uvc_map(payload.get("assay") or {}, "Auto-detect assay camera", "assay")
        self._sexing_map = self._build_sexing_map(payload.get("sexing") or {})

        self.channel_combo["values"] = list(self._channel_map.keys())
        self.assay_combo["values"] = list(self._assay_map.keys())
        self.sexing_combo["values"] = list(self._sexing_map.keys())

        self.channel_var.set(self._selected_uvc_label(payload.get("channel") or {}, self._channel_map, "Auto-detect channel camera"))
        self.assay_var.set(self._selected_uvc_label(payload.get("assay") or {}, self._assay_map, "Auto-detect assay camera"))
        self.sexing_var.set(self._selected_sexing_label(payload.get("sexing") or {}, self._sexing_map))
        self._set_busy(False, "Camera roles loaded.")

    def _build_uvc_map(self, payload: dict[str, Any], auto_label: str, role_key: str) -> dict[str, tuple[str, str]]:
        selected_hint = str(payload.get("selected_hint", "") or "")
        mapping: dict[str, tuple[str, str]] = {auto_label: (f"auto:{role_key}", selected_hint)}
        for device in list(payload.get("devices", []) or []):
            label = str(device.get("label", "") or "").strip()
            stable_path = str(device.get("stable_path", "") or "").strip()
            preferred_hint = str(device.get("card_name", "") or "").strip()
            if not label or not stable_path:
                continue
            mapping[label] = (stable_path, preferred_hint)
        return mapping

    def _build_sexing_map(self, payload: dict[str, Any]) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for device in list(payload.get("devices", []) or []):
            label = str(device.get("label", "") or "").strip()
            index = int(device.get("camera_index", 0))
            if label:
                mapping[label] = index
        if not mapping:
            mapping["Pi Camera 0 | Default ribbon camera"] = 0
        return mapping

    def _selected_uvc_label(self, payload: dict[str, Any], mapping: dict[str, tuple[str, str]], default_label: str) -> str:
        selected_device = str(payload.get("selected_device", "") or "")
        if selected_device.lower() in {"", "auto", "auto:channel", "auto:assay", "channel", "assay"}:
            return default_label
        for label, (device_reference, _preferred_hint) in mapping.items():
            if device_reference == selected_device:
                return label
        return default_label

    def _selected_sexing_label(self, payload: dict[str, Any], mapping: dict[str, int]) -> str:
        selected_index = int(payload.get("selected_index", 0))
        for label, camera_index in mapping.items():
            if camera_index == selected_index:
                return label
        return next(iter(mapping.keys()))

    def _save_roles(self) -> None:
        channel_device, channel_hint = self._channel_map.get(self.channel_var.get(), ("auto:channel", ""))
        assay_device, assay_hint = self._assay_map.get(self.assay_var.get(), ("auto:assay", ""))
        sexing_index = int(self._sexing_map.get(self.sexing_var.get(), 0))
        self._set_busy(True, "Saving camera roles...")

        def runner() -> None:
            try:
                self.actions.save_roles(channel_device, channel_hint, sexing_index, assay_device, assay_hint)
            except Exception as exc:
                self.window.after(0, lambda: self._handle_error(str(exc)))
                return
            self.window.after(0, self._handle_saved)

        threading.Thread(target=runner, daemon=True).start()

    def _handle_saved(self) -> None:
        if self._closed:
            return
        if callable(self.log_callback):
            self.log_callback("Camera roles saved.")
        self.close()
        if callable(self.on_saved):
            self.on_saved()

    def _handle_error(self, message: str) -> None:
        if self._closed:
            return
        self._set_busy(False, "Camera roles need attention.")
        messagebox.showerror("Camera Roles", message, parent=self.window)

    def _handle_cancel(self) -> None:
        self.close()
        if callable(self.on_cancel):
            self.on_cancel()
