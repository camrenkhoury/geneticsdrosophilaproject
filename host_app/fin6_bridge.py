from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shared.config.project_paths import ASSAY_OUTPUT_DIR, CHANNEL_OUTPUT_DIR, FIN6_DIR


SETTINGS_PATH = FIN6_DIR / ".fly_tracking_gui_settings.json"


@dataclass(frozen=True)
class Fin6ChannelSettings:
    background_path: Path
    calibration_path: Path
    output_dir: Path
    device: str
    width: int
    height: int
    fps: int
    channel_mm: float
    score_thresh: int
    band_half_width: int


@dataclass(frozen=True)
class Fin6AssaySettings:
    background_path: Path
    calibration_path: Path
    output_dir: Path
    seconds: float
    fps: float
    camera_width: int
    camera_height: int
    camera_backend: str
    camera_device: str
    camera_index: int
    min_area: int
    max_area: int
    min_threshold: float
    inner_margin_px: int
    max_flies_per_vial: int
    snapshot_interval_s: float
    no_align: bool
    show_positions: bool


@dataclass(frozen=True)
class Fin6SetupStatus:
    settings_path: Path
    settings_file_exists: bool
    channel: Fin6ChannelSettings
    assay: Fin6AssaySettings
    channel_background_ready: bool
    channel_calibration_ready: bool
    assay_background_ready: bool
    assay_calibration_ready: bool

    @property
    def channel_ready(self) -> bool:
        return self.channel_background_ready and self.channel_calibration_ready

    @property
    def assay_ready(self) -> bool:
        return self.assay_background_ready and self.assay_calibration_ready


def _load_settings_file() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_path(raw_value: Any, default_path: Path) -> Path:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return default_path
    candidate = Path(raw_text).expanduser()
    if candidate.is_absolute():
        return candidate
    return (FIN6_DIR / candidate).resolve()


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def get_setup_status() -> Fin6SetupStatus:
    saved = _load_settings_file()
    channel = Fin6ChannelSettings(
        background_path=_resolve_path(saved.get("channel_background_var"), FIN6_DIR / "backgrounds" / "channel_bg.png"),
        calibration_path=_resolve_path(saved.get("channel_calibration_var"), FIN6_DIR / "calibrations" / "channel_calibration.json"),
        output_dir=_resolve_path(saved.get("channel_output_var"), CHANNEL_OUTPUT_DIR),
        device=str(saved.get("channel_device_var") or "/dev/video8"),
        width=_to_int(saved.get("channel_width_var"), 1920),
        height=_to_int(saved.get("channel_height_var"), 1080),
        fps=_to_int(saved.get("channel_fps_var"), 30),
        channel_mm=_to_float(saved.get("channel_mm_var"), 111.0),
        score_thresh=_to_int(saved.get("channel_score_var"), 20),
        band_half_width=_to_int(saved.get("channel_band_var"), 35),
    )
    assay = Fin6AssaySettings(
        background_path=_resolve_path(saved.get("assay_background_var"), FIN6_DIR / "backgrounds" / "assay_bg.png"),
        calibration_path=_resolve_path(saved.get("assay_calibration_var"), FIN6_DIR / "calibrations" / "assay_calibration.json"),
        output_dir=_resolve_path(saved.get("assay_output_var"), ASSAY_OUTPUT_DIR),
        seconds=_to_float(saved.get("assay_seconds_var"), 30.0),
        fps=_to_float(saved.get("assay_fps_var"), 5.0),
        camera_width=_to_int(saved.get("assay_width_var"), 1536),
        camera_height=_to_int(saved.get("assay_height_var"), 864),
        camera_backend=str(saved.get("assay_camera_backend_var") or "opencv"),
        camera_device=str(saved.get("assay_camera_device_var") or "/dev/video10"),
        camera_index=_to_int(saved.get("assay_camera_index_var"), 0),
        min_area=_to_int(saved.get("assay_min_area_var"), 10),
        max_area=_to_int(saved.get("assay_max_area_var"), 250),
        min_threshold=_to_float(saved.get("assay_threshold_var"), 16.0),
        inner_margin_px=_to_int(saved.get("assay_margin_var"), 8),
        max_flies_per_vial=_to_int(saved.get("assay_max_flies_var"), 10),
        snapshot_interval_s=_to_float(saved.get("assay_snapshot_var"), 1.0),
        no_align=_to_bool(saved.get("assay_no_align_var"), False),
        show_positions=_to_bool(saved.get("assay_show_xy_overlay_var"), False),
    )
    return Fin6SetupStatus(
        settings_path=SETTINGS_PATH,
        settings_file_exists=SETTINGS_PATH.exists(),
        channel=channel,
        assay=assay,
        channel_background_ready=channel.background_path.exists(),
        channel_calibration_ready=channel.calibration_path.exists(),
        assay_background_ready=assay.background_path.exists(),
        assay_calibration_ready=assay.calibration_path.exists(),
    )


def detect_channel_once_from_saved_settings() -> dict[str, Any]:
    import cv2

    from vision.fin6.camera_sources import BrioCamera, BrioConfig
    from vision.fin6.fly_x_detector import process_fly_detection

    status = get_setup_status()
    if not status.channel_ready:
        missing = []
        if not status.channel_background_ready:
            missing.append(f"background: {status.channel.background_path}")
        if not status.channel_calibration_ready:
            missing.append(f"calibration: {status.channel.calibration_path}")
        raise FileNotFoundError("Channel setup is incomplete:\n" + "\n".join(missing))

    channel = status.channel
    with BrioCamera(
        BrioConfig(
            device=channel.device,
            width=channel.width,
            height=channel.height,
            fps=channel.fps,
        )
    ) as camera:
        frame = camera.read()

    result, annotated, mask = process_fly_detection(
        background=str(channel.background_path),
        frame=frame,
        calibration_path=str(channel.calibration_path),
        score_thresh=channel.score_thresh,
        band_half_width=channel.band_half_width,
    )

    channel.output_dir.mkdir(parents=True, exist_ok=True)
    annotated_path = channel.output_dir / "last_channel_annotated.png"
    mask_path = channel.output_dir / "last_channel_mask.png"
    result_path = channel.output_dir / "last_channel_result.json"

    if not cv2.imwrite(str(annotated_path), annotated):
        raise IOError(f"Could not save annotated channel image to {annotated_path}")
    if not cv2.imwrite(str(mask_path), mask):
        raise IOError(f"Could not save channel mask image to {mask_path}")
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return {
        "result": result,
        "output_dir": channel.output_dir,
        "annotated_path": annotated_path,
        "mask_path": mask_path,
        "result_path": result_path,
    }


def run_assay_from_saved_settings(
    preview_callback: Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], None] | None = None,
    stop_event=None,
) -> dict[str, Any]:
    from vision.fin6.assay_tracking import run_assay_session

    status = get_setup_status()
    if not status.assay_ready:
        missing = []
        if not status.assay_background_ready:
            missing.append(f"background: {status.assay.background_path}")
        if not status.assay_calibration_ready:
            missing.append(f"calibration: {status.assay.calibration_path}")
        raise FileNotFoundError("Assay setup is incomplete:\n" + "\n".join(missing))

    assay = status.assay
    live_fps = max(8.0, float(assay.fps))
    return run_assay_session(
        background_path=str(assay.background_path),
        calibration_path=str(assay.calibration_path),
        output_dir=str(assay.output_dir),
        seconds=float(assay.seconds),
        fps=live_fps,
        camera_width=int(assay.camera_width),
        camera_height=int(assay.camera_height),
        camera_backend=assay.camera_backend,
        camera_device=assay.camera_device,
        camera_index=int(assay.camera_index),
        min_area=int(assay.min_area),
        max_area=int(assay.max_area),
        min_threshold=float(assay.min_threshold),
        inner_margin_px=int(assay.inner_margin_px),
        max_flies_per_vial=int(assay.max_flies_per_vial),
        snapshot_interval_s=float(assay.snapshot_interval_s),
        no_align=bool(assay.no_align),
        show_positions=bool(assay.show_positions),
        preview_callback=preview_callback,
        stop_event=stop_event,
    )


def launch_fin6_gui() -> subprocess.Popen:
    script_path = FIN6_DIR / "fly_tracking_gui.py"
    return subprocess.Popen([sys.executable, str(script_path)], cwd=str(FIN6_DIR))
