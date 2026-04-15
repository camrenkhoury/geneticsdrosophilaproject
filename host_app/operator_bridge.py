from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shared.config.project_paths import ASSAY_OUTPUT_DIR, CHANNEL_OUTPUT_DIR, FIN6_DIR
import config


SETTINGS_PATH = FIN6_DIR / ".fly_tracking_gui_settings.json"

_DEFAULT_PROJECT_PATHS: dict[str, Path] = {
    "channel_background_var": FIN6_DIR / "backgrounds" / "channel_bg.png",
    "channel_calibration_var": FIN6_DIR / "calibrations" / "channel_calibration.json",
    "channel_output_var": CHANNEL_OUTPUT_DIR,
    "assay_background_var": FIN6_DIR / "backgrounds" / "assay_bg.png",
    "assay_calibration_var": FIN6_DIR / "calibrations" / "assay_calibration.json",
    "assay_output_var": ASSAY_OUTPUT_DIR,
}

_DEFAULT_STRING_SETTINGS: dict[str, str] = {
    "channel_device_var": "auto:channel",
    "channel_preferred_hint_var": "",
    "assay_camera_backend_var": "opencv",
    "assay_camera_device_var": "auto:assay",
    "assay_camera_preferred_hint_var": "",
}

_LEGACY_DEVICE_DEFAULTS: dict[str, set[str]] = {
    "channel_device_var": {"/dev/video8"},
    "assay_camera_device_var": {"/dev/video10"},
}


@dataclass(frozen=True)
class Fin6ChannelSettings:
    background_path: Path
    calibration_path: Path
    output_dir: Path
    device: str
    preferred_hint: str
    width: int
    height: int
    fps: int
    channel_mm: float
    score_thresh: int
    band_half_width: int
    no_align: bool


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
    camera_preferred_hint: str
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


def _save_settings_file(data: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_project_layout() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    (FIN6_DIR / "backgrounds").mkdir(parents=True, exist_ok=True)
    (FIN6_DIR / "calibrations").mkdir(parents=True, exist_ok=True)
    CHANNEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ASSAY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _resolve_path(raw_value: Any, default_path: Path) -> Path:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return default_path
    candidate = Path(raw_text).expanduser()
    if candidate.is_absolute():
        return candidate
    return (FIN6_DIR / candidate).resolve()


def _looks_like_windows_path(raw_text: str) -> bool:
    return len(raw_text) >= 3 and raw_text[1] == ":" and raw_text[0].isalpha() and raw_text[2] in {"\\", "/"}


def _normalize_project_path(var_name: str, raw_value: Any) -> Path:
    default_path = _DEFAULT_PROJECT_PATHS[var_name]
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return default_path
    if _looks_like_windows_path(raw_text):
        return default_path

    current = Path(raw_text).expanduser()
    if not current.is_absolute():
        return (FIN6_DIR / current).resolve()

    if var_name in {"channel_background_var", "assay_background_var"}:
        try:
            current.relative_to(FIN6_DIR)
        except ValueError:
            return default_path

    if current.name == default_path.name and not current.exists():
        return default_path

    if var_name.endswith("_output_var") and not current.exists():
        return default_path

    return current


def _normalize_string_setting(var_name: str, raw_value: Any) -> str:
    default_value = _DEFAULT_STRING_SETTINGS[var_name]
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return default_value
    if raw_text in _LEGACY_DEVICE_DEFAULTS.get(var_name, set()):
        return default_value
    return raw_text


def normalize_settings_file(*, persist: bool = True) -> dict[str, Any]:
    _ensure_project_layout()
    saved = _load_settings_file()
    normalized = dict(saved)
    changed = False

    for var_name in _DEFAULT_PROJECT_PATHS:
        resolved = str(_normalize_project_path(var_name, saved.get(var_name)))
        if normalized.get(var_name) != resolved:
            normalized[var_name] = resolved
            changed = True

    for var_name in _DEFAULT_STRING_SETTINGS:
        value = _normalize_string_setting(var_name, saved.get(var_name))
        if normalized.get(var_name) != value:
            normalized[var_name] = value
            changed = True

    if persist and (changed or not SETTINGS_PATH.exists()):
        _save_settings_file(normalized)

    return normalized


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


def _camera_description(device_reference: str, *, role: str, preferred_hint: str = "") -> str:
    from vision.fin6.camera_sources import describe_camera_selection

    try:
        descriptor = describe_camera_selection(device_reference, role=role, preferred_hint=preferred_hint)
    except Exception as exc:
        return f"{role.title()} camera unavailable: {exc}"
    if descriptor is None:
        return f"{role.title()} camera unavailable"
    return f"{descriptor.card_name} ({descriptor.stable_path})"


def get_setup_status() -> Fin6SetupStatus:
    saved = normalize_settings_file()
    channel = Fin6ChannelSettings(
        background_path=_resolve_path(saved.get("channel_background_var"), FIN6_DIR / "backgrounds" / "channel_bg.png"),
        calibration_path=_resolve_path(saved.get("channel_calibration_var"), FIN6_DIR / "calibrations" / "channel_calibration.json"),
        output_dir=_resolve_path(saved.get("channel_output_var"), CHANNEL_OUTPUT_DIR),
        device=str(saved.get("channel_device_var") or "auto:channel"),
        preferred_hint=str(saved.get("channel_preferred_hint_var") or ""),
        width=_to_int(saved.get("channel_width_var"), 1920),
        height=_to_int(saved.get("channel_height_var"), 1080),
        fps=_to_int(saved.get("channel_fps_var"), 30),
        channel_mm=_to_float(saved.get("channel_mm_var"), float(getattr(config, "CHANNEL_LENGTH", 149.0))),
        score_thresh=_to_int(saved.get("channel_score_var"), 20),
        band_half_width=_to_int(saved.get("channel_band_var"), 35),
        no_align=_to_bool(saved.get("channel_no_align_var"), False),
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
        camera_device=str(saved.get("assay_camera_device_var") or "auto:assay"),
        camera_preferred_hint=str(saved.get("assay_camera_preferred_hint_var") or ""),
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


def setup_status_to_dict(status: Fin6SetupStatus) -> dict[str, Any]:
    return {
        "settings_path": str(status.settings_path),
        "settings_file_exists": bool(status.settings_file_exists),
        "channel_background_ready": bool(status.channel_background_ready),
        "channel_calibration_ready": bool(status.channel_calibration_ready),
        "assay_background_ready": bool(status.assay_background_ready),
        "assay_calibration_ready": bool(status.assay_calibration_ready),
        "channel_ready": bool(status.channel_ready),
        "assay_ready": bool(status.assay_ready),
        "channel": {
            "background_path": str(status.channel.background_path),
            "calibration_path": str(status.channel.calibration_path),
            "output_dir": str(status.channel.output_dir),
            "device": status.channel.device,
            "preferred_hint": status.channel.preferred_hint,
            "camera_description": _camera_description(
                status.channel.device,
                role="channel",
                preferred_hint=status.channel.preferred_hint,
            ),
            "width": int(status.channel.width),
            "height": int(status.channel.height),
            "fps": int(status.channel.fps),
            "channel_mm": float(status.channel.channel_mm),
            "score_thresh": int(status.channel.score_thresh),
            "band_half_width": int(status.channel.band_half_width),
            "no_align": bool(status.channel.no_align),
        },
        "assay": {
            "background_path": str(status.assay.background_path),
            "calibration_path": str(status.assay.calibration_path),
            "output_dir": str(status.assay.output_dir),
            "seconds": float(status.assay.seconds),
            "fps": float(status.assay.fps),
            "camera_width": int(status.assay.camera_width),
            "camera_height": int(status.assay.camera_height),
            "camera_backend": status.assay.camera_backend,
            "camera_device": status.assay.camera_device,
            "camera_preferred_hint": status.assay.camera_preferred_hint,
            "camera_description": _camera_description(
                status.assay.camera_device,
                role="assay",
                preferred_hint=status.assay.camera_preferred_hint,
            ),
            "camera_index": int(status.assay.camera_index),
            "min_area": int(status.assay.min_area),
            "max_area": int(status.assay.max_area),
            "min_threshold": float(status.assay.min_threshold),
            "inner_margin_px": int(status.assay.inner_margin_px),
            "max_flies_per_vial": int(status.assay.max_flies_per_vial),
            "snapshot_interval_s": float(status.assay.snapshot_interval_s),
            "no_align": bool(status.assay.no_align),
            "show_positions": bool(status.assay.show_positions),
        },
    }


def list_available_cameras() -> dict[str, Any]:
    from vision.fin6.camera_sources import describe_camera_selection, list_video_devices

    status = get_setup_status()
    devices = list_video_devices(prefer_index_zero=True)
    selected = describe_camera_selection(
        status.channel.device,
        role="channel",
        preferred_hint=status.channel.preferred_hint,
    )
    items: list[dict[str, Any]] = []
    for device in devices:
        label_parts = [device.card_name]
        if device.is_brio:
            label_parts.append("Brio")
        stable = device.stable_path or device.device_path
        if stable:
            label_parts.append(stable)
        role_guess = "channel" if device.is_brio else "other"
        items.append(
            {
                "label": " | ".join(label_parts),
                "device_path": device.device_path,
                "stable_path": device.stable_path,
                "card_name": device.card_name,
                "symlink_name": device.symlink_name,
                "by_id_path": device.by_id_path,
                "by_path_path": device.by_path_path,
                "is_brio": bool(device.is_brio),
                "role_guess": role_guess,
                "selected": bool(selected is not None and stable == selected.stable_path),
            }
        )
    return {
        "auto_label": "Auto-detect channel camera",
        "selected_device": str(status.channel.device),
        "selected_hint": str(status.channel.preferred_hint),
        "devices": items,
    }


def update_channel_camera_selection(device_reference: str, preferred_hint: str = "") -> dict[str, Any]:
    normalized = normalize_settings_file(persist=False)
    device_text = str(device_reference or "").strip()
    hint_text = str(preferred_hint or "").strip()
    if not device_text or device_text.lower() in {"auto", "auto:channel", "channel"}:
        normalized["channel_device_var"] = "auto:channel"
        normalized["channel_preferred_hint_var"] = hint_text
    else:
        normalized["channel_device_var"] = device_text
        normalized["channel_preferred_hint_var"] = hint_text
    _save_settings_file(normalized)
    status = get_setup_status()
    return {
        "ok": True,
        "message": "Channel camera selection saved.",
        "channel": setup_status_to_dict(status)["channel"],
    }


def _missing_channel_setup_message(status: Fin6SetupStatus) -> str:
    missing: list[str] = []
    if not status.channel_background_ready:
        missing.append("channel background")
    if not status.channel_calibration_ready:
        missing.append("channel calibration")
    joined = " and ".join(missing) if missing else "channel setup"
    return (
        "Saved Channel Detection Setup is incomplete. "
        f"Save the {joined} in Channel Detection Setup before running detection."
    )


def _missing_assay_setup_message(status: Fin6SetupStatus) -> str:
    missing: list[str] = []
    if not status.assay_background_ready:
        missing.append("assay background")
    if not status.assay_calibration_ready:
        missing.append("assay calibration")
    joined = " and ".join(missing) if missing else "assay setup"
    return (
        "Saved Assay Setup is incomplete. "
        f"Save the {joined} in Assay Setup before running the assay."
    )


def capture_channel_background_from_saved_settings(*, frame_count: int = 15) -> dict[str, Any]:
    from vision.fin6.brio_channel_cli import capture_brio_background

    status = get_setup_status()
    channel = status.channel
    channel.background_path.parent.mkdir(parents=True, exist_ok=True)

    saved_path = capture_brio_background(
        output_path=channel.background_path,
        device=channel.device,
        width=channel.width,
        height=channel.height,
        fps=channel.fps,
        frame_count=frame_count,
    )
    return {
        "background_path": str(Path(saved_path).resolve()),
        "camera_description": _camera_description(
            channel.device,
            role="channel",
            preferred_hint=channel.preferred_hint,
        ),
    }


def capture_channel_preview_from_saved_settings() -> dict[str, Any]:
    import cv2

    from vision.fin6.camera_sources import BrioCamera, BrioConfig

    status = get_setup_status()
    channel = status.channel
    preview_path = FIN6_DIR / "backgrounds" / "channel_setup_preview.jpg"
    preview_path.parent.mkdir(parents=True, exist_ok=True)

    with BrioCamera(
        BrioConfig(
            device=channel.device,
            width=channel.width,
            height=channel.height,
            fps=channel.fps,
            preferred_hint=channel.preferred_hint,
            role="channel",
            warmup_frames=8,
            flush_grabs=2,
            reconnect_attempts=1,
            reconnect_sleep_s=0.15,
            post_open_settle_s=0.03,
        )
    ) as camera:
        frame_bgr = camera.read()

    if not cv2.imwrite(str(preview_path), frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90]):
        raise IOError(f"Could not save setup preview image to {preview_path}")

    return {
        "preview_path": str(preview_path.resolve()),
        "camera_description": _camera_description(
            channel.device,
            role="channel",
            preferred_hint=channel.preferred_hint,
        ),
    }


def save_channel_calibration_from_points(
    left_point_px: tuple[int, int],
    right_point_px: tuple[int, int],
    *,
    channel_mm: float | None = None,
) -> dict[str, Any]:
    import cv2

    from vision.fin6.fly_x_detector import estimate_channel_crop_from_background, save_calibration

    status = get_setup_status()
    channel = status.channel
    if not channel.background_path.exists():
        raise FileNotFoundError(_missing_channel_setup_message(status))

    channel.calibration_path.parent.mkdir(parents=True, exist_ok=True)
    background_gray = cv2.imread(str(channel.background_path), cv2.IMREAD_GRAYSCALE)
    if background_gray is None:
        raise FileNotFoundError(f"Could not read saved channel background image: {channel.background_path}")

    left_pt = (int(left_point_px[0]), int(left_point_px[1]))
    right_pt = (int(right_point_px[0]), int(right_point_px[1]))
    resolved_channel_mm = float(channel_mm if channel_mm is not None else channel.channel_mm)
    crop_x_pad, crop_above_px, crop_below_px, _ = estimate_channel_crop_from_background(
        background_gray,
        left_pt,
        right_pt,
    )
    payload = save_calibration(
        channel.calibration_path,
        left_pt=left_pt,
        right_pt=right_pt,
        channel_length_mm=resolved_channel_mm,
        crop_x_pad=crop_x_pad,
        crop_above_px=crop_above_px,
        crop_below_px=crop_below_px,
    )
    return {
        "calibration_path": str(Path(channel.calibration_path).resolve()),
        "calibration": payload,
    }


def detect_channel_once_from_saved_settings() -> dict[str, Any]:
    import cv2

    from vision.fin6.camera_sources import BrioCamera, BrioConfig
    from vision.fin6.fly_x_detector import load_calibration_data, process_fly_detection

    status = get_setup_status()
    if not status.channel_ready:
        raise FileNotFoundError(_missing_channel_setup_message(status))

    channel = status.channel
    channel.output_dir.mkdir(parents=True, exist_ok=True)

    background_bgr = cv2.imread(str(channel.background_path), cv2.IMREAD_COLOR)
    if background_bgr is None:
        raise FileNotFoundError(f"Could not read saved channel background image: {channel.background_path}")
    calibration = load_calibration_data(channel.calibration_path)

    with BrioCamera(
        BrioConfig(
            device=channel.device,
            width=channel.width,
            height=channel.height,
            fps=channel.fps,
            preferred_hint=channel.preferred_hint,
            role="channel",
            warmup_frames=8,
            flush_grabs=2,
            reconnect_attempts=1,
            reconnect_sleep_s=0.15,
            post_open_settle_s=0.03,
        )
    ) as camera:
        frame_bgr = camera.read()

    result, annotated, mask = process_fly_detection(
        background=background_bgr,
        frame=frame_bgr,
        left_pt=tuple(map(int, calibration["left_point_px"])),
        right_pt=tuple(map(int, calibration["right_point_px"])),
        channel_mm=float(calibration.get("channel_length_mm", channel.channel_mm)),
        band_half_width=int(channel.band_half_width),
        score_thresh=int(channel.score_thresh),
        no_align=bool(channel.no_align),
        crop_x_pad=calibration.get("crop_x_pad"),
        crop_above_px=calibration.get("crop_above_px"),
        crop_below_px=calibration.get("crop_below_px"),
    )

    raw_path = channel.output_dir / "last_channel_raw.jpg"
    annotated_path = channel.output_dir / "last_channel_annotated.png"
    mask_path = channel.output_dir / "last_channel_mask.png"
    result_path = channel.output_dir / "last_channel_result.json"

    if not cv2.imwrite(str(raw_path), frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90]):
        raise IOError(f"Could not save raw channel image to {raw_path}")
    if not cv2.imwrite(str(annotated_path), annotated):
        raise IOError(f"Could not save annotated channel image to {annotated_path}")
    if not cv2.imwrite(str(mask_path), mask):
        raise IOError(f"Could not save channel mask image to {mask_path}")

    payload = dict(result)
    payload.update(
        {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "raw_image_path": str(raw_path.resolve()),
            "annotated_image_path": str(annotated_path.resolve()),
            "mask_image_path": str(mask_path.resolve()),
            "result_json_path": str(result_path.resolve()),
        }
    )
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "result": payload,
        "output_dir": channel.output_dir,
        "raw_path": raw_path,
        "annotated_path": annotated_path,
        "mask_path": mask_path,
        "result_path": result_path,
        "camera_description": _camera_description(
            channel.device,
            role="channel",
            preferred_hint=channel.preferred_hint,
        ),
    }


def run_assay_from_saved_settings(
    preview_callback: Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], None] | None = None,
    stop_event=None,
) -> dict[str, Any]:
    from vision.fin6.assay_tracking import run_assay_session

    status = get_setup_status()
    if not status.assay_ready:
        raise FileNotFoundError(_missing_assay_setup_message(status))

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
        camera_preferred_hint=assay.camera_preferred_hint,
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
    normalize_settings_file()
    script_path = FIN6_DIR / "fly_tracking_gui.py"
    env = os.environ.copy()
    env["DROSOPHILA_FIN6_RAISE"] = "1"
    return subprocess.Popen([sys.executable, str(script_path)], cwd=str(FIN6_DIR), env=env)


__all__ = [
    "Fin6AssaySettings",
    "Fin6ChannelSettings",
    "Fin6SetupStatus",
    "SETTINGS_PATH",
    "capture_channel_background_from_saved_settings",
    "capture_channel_preview_from_saved_settings",
    "detect_channel_once_from_saved_settings",
    "get_setup_status",
    "launch_fin6_gui",
    "list_available_cameras",
    "normalize_settings_file",
    "run_assay_from_saved_settings",
    "save_channel_calibration_from_points",
    "setup_status_to_dict",
    "update_channel_camera_selection",
]
