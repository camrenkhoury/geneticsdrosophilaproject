from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, Optional

from .bootstrap import PROJECT_ROOT, ensure_repo_paths

ensure_repo_paths()

from assay_profile import ProfileStore  # noqa: E402
from shared_utils import load_json, save_json  # noqa: E402
from fly_tracking_gui import APP_BG, App as StandaloneFin6App  # noqa: E402


_ORIG_LOAD_PROFILE = StandaloneFin6App.load_profile
_ORIG_SAVE_PROFILE = StandaloneFin6App.save_profile
_ORIG_ASSAY_BG_DONE = StandaloneFin6App._on_assay_background_done
_ORIG_SAVE_CALIBRATION = StandaloneFin6App.save_assay_calibration_from_editor
_ORIG_ASSAY_RECORD_DONE = StandaloneFin6App._on_assay_record_done
_ORIG_ASSAY_PROCESS_DONE = StandaloneFin6App._on_assay_process_done
_ORIG_POLL_QUEUE = StandaloneFin6App._poll_queue


class EmbeddedAssayUI(tk.Frame):
    """Embeds the two assay-final tabs inside the stitch operator shell.

    The original fin6 GUI is implemented as a standalone Tk root. This adapter
    reuses almost all of those methods while providing a lightweight hidden
    frame that builds the two tab bodies directly into the stitch operator's
    Assay and Results pages.
    """

    def __init__(self, master: tk.Misc, controller=None) -> None:
        super().__init__(master, bg=APP_BG, highlightthickness=0, bd=0)
        self.controller = controller
        self.project_root = (PROJECT_ROOT / "fin6").resolve()
        self.profile_store = ProfileStore(self.project_root / "profiles")
        self.settings_path = self.project_root / ".fly_tracking_gui_settings.json"
        self.ui_queue: queue.Queue = queue.Queue()

        self.current_profile = None
        self.current_profile_path: Optional[Path] = None
        self.latest_assay_raw_frame = None
        self.assay_preview_images: Dict[str, Any] = {}
        self.assay_log_lines: list[str] = []
        self.channel_log_lines: list[str] = []
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

        self._pages_attached = False
        self._poll_started = False
        self._assay_parent: Optional[tk.Misc] = None
        self._debug_parent: Optional[tk.Misc] = None
        self._suspend_controller_sync = False
        self._pending_controller_profile_reload = False
        self._pending_assay_state_payload: Dict[str, Any] = {}
        self._pending_status_message: Optional[str] = None

        self._build_vars()
        self._configure_styles()

    # ------------------------------------------------------------------
    # embedded lifecycle + persistence
    # ------------------------------------------------------------------
    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        # Do not switch the global ttk theme or override base widget classes
        # inside the host shell. That restyles the surrounding Channel/Sexing
        # tabs when the assay workspace is first opened.
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#5f6b7a")
        style.configure("Section.TLabel", background="#ffffff", foreground="#0f172a", font=("DejaVu Sans", 10, "bold"))
        style.configure("Title.TLabel", background="#ffffff", foreground="#0f172a", font=("DejaVu Sans", 12, "bold"))
        style.configure("Primary.TButton", padding=(10, 6))
        style.configure("Small.TButton", padding=(6, 3))
        style.configure("Treeview", rowheight=26)

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            data = load_json(self.settings_path)
        except Exception:
            return
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
        payload: Dict[str, Any] = {}
        if self.settings_path.exists():
            try:
                payload.update(load_json(self.settings_path))
            except Exception:
                pass
        payload.update(
            {
                "channel_background": self.channel_background_var.get(),
                "channel_calibration": self.channel_calibration_var.get(),
                "assay_preview_mode": self.assay_preview_mode_var.get(),
                "channel_mm": self.channel_mm_var.get(),
                "channel_score_thresh": self.channel_score_thresh_var.get(),
                "channel_band_half_width": self.channel_band_half_width_var.get(),
            }
        )
        save_json(self.settings_path, payload)

    def attach_pages(self, assay_parent: tk.Misc, debug_parent: tk.Misc) -> None:
        if self._pages_attached:
            return
        self._assay_parent = assay_parent
        self._debug_parent = debug_parent
        try:
            assay_parent.configure(bg=APP_BG)
        except Exception:
            pass
        try:
            debug_parent.configure(bg=APP_BG)
        except Exception:
            pass
        # In the host shell, assay_parent is the "Setup + Run" tab and
        # debug_parent is the "Results" tab. The standalone fin6 app names are
        # inverted relative to that shell, so attach the full assay workspace
        # to the setup tab and the lighter playback/results page to the results
        # tab.
        self._build_assay_tab(assay_parent)
        self._build_biologist_tab(debug_parent)
        self._load_settings()
        self._suspend_controller_sync = True
        try:
            self._load_initial_profile()
        finally:
            self._suspend_controller_sync = False
        self._refresh_device_labels()
        self._refresh_assay_background_info()
        self._refresh_assay_canvas()
        self._update_region_tree()
        self._update_region_editor()
        self._update_status_labels()
        if not self._poll_started:
            self._poll_started = True
            self.after(80, self._poll_queue)
        self._pages_attached = True
        self.flush_controller_sync(force_reload=True)

    def _load_initial_profile(self) -> None:
        profile_name = str(getattr(getattr(self.controller, "settings", None), "active_assay_profile", "") or "").strip()
        if profile_name:
            try:
                profile = self.profile_store.load_profile(profile_name)
            except Exception:
                profile = self._default_profile(profile_name)
                self.profile_store.save_profile(profile)
            self.load_profile(profile)
            return
        self._load_startup_profile()

    def shutdown(self) -> None:
        try:
            if self.channel_live_stop_event is not None:
                self.channel_live_stop_event.set()
        except Exception:
            pass
        try:
            self._release_assay_video_playback(status_text=None)
        except Exception:
            pass
        try:
            self._save_settings()
        except Exception:
            pass

    def _on_close(self) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # controller syncing
    # ------------------------------------------------------------------
    def _controller_busy(self) -> bool:
        if self.controller is None:
            return False
        try:
            snapshot = self.controller.snapshot()
        except Exception:
            return False
        return bool(getattr(snapshot, "busy", False))

    def _queue_assay_state_update(self, payload: Optional[Dict[str, Any]] = None, status_message: Optional[str] = None) -> None:
        if payload:
            self._pending_assay_state_payload.update(payload)
        if status_message:
            self._pending_status_message = status_message

    def flush_controller_sync(self, *, force_reload: bool = False) -> None:
        if self.controller is None or self._suspend_controller_sync:
            return
        if self._controller_busy():
            self._pending_controller_profile_reload = self._pending_controller_profile_reload or force_reload
            return
        profile = self.current_profile
        if profile is not None:
            profile_name = str(getattr(profile, "name", "") or "").strip()
            if profile_name and self.profile_store.profile_path(profile_name).exists():
                try:
                    if str(self.controller.settings.active_assay_profile or "") != profile_name:
                        self.controller.set_active_profile(profile_name)
                    elif force_reload or self._pending_controller_profile_reload:
                        self.controller.assay.load_profile(profile_name)
                        self.controller.refresh_readiness()
                except Exception:
                    pass
        payload = dict(self._pending_assay_state_payload)
        status = self._pending_status_message
        self._pending_assay_state_payload.clear()
        self._pending_status_message = None
        self._pending_controller_profile_reload = False
        if payload:
            try:
                self.controller._set_assay_state(payload)
            except Exception:
                pass
        if status:
            try:
                self.controller._update(status_message=status)
            except Exception:
                pass
        try:
            self.controller.refresh_readiness()
        except Exception:
            pass

    def sync_from_controller_active_profile(self, *, force: bool = False) -> None:
        if self.controller is None or not self._pages_attached:
            return
        profile_name = str(getattr(getattr(self.controller, "settings", None), "active_assay_profile", "") or "").strip()
        if not profile_name:
            return
        current_name = str(getattr(self.current_profile, "name", "") or "")
        if force or current_name != profile_name:
            try:
                profile = self.profile_store.load_profile(profile_name)
            except Exception:
                profile = self._default_profile(profile_name)
                self.profile_store.save_profile(profile)
            self._suspend_controller_sync = True
            try:
                self.load_profile(profile)
            finally:
                self._suspend_controller_sync = False
        else:
            try:
                self._refresh_device_labels()
                self._refresh_assay_background_info()
                self._refresh_assay_canvas()
                self._update_region_tree()
                self._update_region_editor()
                self._update_status_labels()
            except Exception:
                pass
        self.flush_controller_sync(force_reload=False)

    # ------------------------------------------------------------------
    # wrappers around the standalone fin6 actions so the stitch controller
    # stays synchronized with the new embedded tabs.
    # ------------------------------------------------------------------
    def load_profile(self, profile) -> None:
        _ORIG_LOAD_PROFILE(self, profile)
        if self._pages_attached and not self._suspend_controller_sync:
            self.flush_controller_sync(force_reload=False)

    def save_profile(self) -> None:
        _ORIG_SAVE_PROFILE(self)
        if self._pages_attached and not self._suspend_controller_sync:
            self.flush_controller_sync(force_reload=True)

    def _on_assay_background_done(self, record: Any) -> None:
        _ORIG_ASSAY_BG_DONE(self, record)
        self.flush_controller_sync(force_reload=True)
        self._queue_assay_state_update(status_message="Assay background updated.")
        self.flush_controller_sync()

    def save_assay_calibration_from_editor(self, silent: bool = False) -> bool:
        saved = bool(_ORIG_SAVE_CALIBRATION(self, silent=silent))
        if saved:
            self.flush_controller_sync(force_reload=True)
            self._queue_assay_state_update(status_message="Assay calibration saved.")
            self.flush_controller_sync()
        return saved

    def _on_assay_record_done(self, result: Dict[str, Any]) -> None:
        _ORIG_ASSAY_RECORD_DONE(self, result)
        run_dir = str(result.get("run_dir", "") or "")
        payload: Dict[str, Any] = {}
        if run_dir:
            payload["run_dir"] = run_dir
        raw_path = str(result.get("raw_video_path", "") or "")
        if raw_path:
            payload["preview_path"] = raw_path
        self._queue_assay_state_update(payload, status_message=f"Assay recorded: {Path(run_dir).name}" if run_dir else "Assay recording complete.")
        self.flush_controller_sync(force_reload=True)

    def _on_assay_process_done(self, result: Dict[str, Any]) -> None:
        _ORIG_ASSAY_PROCESS_DONE(self, result)
        payload = dict(result or {})
        if "report_pdf" in payload and "pdf_path" not in payload:
            payload["pdf_path"] = payload.get("report_pdf")
        if "annotated_video_path" in payload and "preview_path" not in payload:
            payload["preview_path"] = payload.get("annotated_video_path")
        self._queue_assay_state_update(payload, status_message="Assay processing complete.")
        self.flush_controller_sync(force_reload=True)

    def _poll_queue(self) -> None:
        _ORIG_POLL_QUEUE(self)
        self.flush_controller_sync(force_reload=False)


# Copy every reusable method from the standalone Tk app unless this adapter
# already defines a custom implementation.
for _name, _value in StandaloneFin6App.__dict__.items():
    if _name.startswith("__"):
        continue
    if hasattr(EmbeddedAssayUI, _name):
        continue
    if callable(_value):
        setattr(EmbeddedAssayUI, _name, _value)
