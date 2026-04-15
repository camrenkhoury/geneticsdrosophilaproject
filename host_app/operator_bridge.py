from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from shared.config.project_paths import ASSAY_OUTPUT_DIR, CHANNEL_OUTPUT_DIR, FIN6_DIR, REPO_ROOT
import config


SETTINGS_PATH = FIN6_DIR / ".fly_tracking_gui_settings.json"
CHANNEL_ARTIFACT_TRACE_PATH = FIN6_DIR / ".channel_artifact_trace.log"
CHANNEL_SETUP_SOURCE_PATH = FIN6_DIR / "backgrounds" / "channel_setup_source.png"
CHANNEL_PHOTO_POSITION_MM = 191.0
CHANNEL_PHOTO_POSITION_TOL_MM = 1.0

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
    "sexing_camera_backend_var": "rpicam",
    "assay_camera_backend_var": "opencv",
    "assay_camera_device_var": "auto:assay",
    "assay_camera_preferred_hint_var": "",
}

_DEFAULT_NUMERIC_SETTINGS: dict[str, float] = {
    "channel_mm_var": float(getattr(config, "CHANNEL_LENGTH", 149.0)),
    "sexing_camera_index_var": 0.0,
}

_LEGACY_DEVICE_DEFAULTS: dict[str, set[str]] = {
    "channel_device_var": {"/dev/video8"},
    "assay_camera_device_var": {"/dev/video10"},
}

_STITCH_OPERATOR_ASSAY_COMPONENTS: dict[str, Any] | None = None


def _ensure_stitch_operator_import_paths() -> Path:
    implementation_root = (REPO_ROOT / "Avi Detection GUI code" / "Integrated1").resolve()
    if not implementation_root.exists():
        raise RuntimeError(f"Implementation 1 directory is missing: {implementation_root}")
    for path in (implementation_root, implementation_root / "fin6"):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    return implementation_root


def _merge_stitch_operator_config_defaults(implementation_root: Path) -> None:
    config_path = implementation_root / "CodeDirectory" / "config.py"
    if not config_path.exists():
        return
    module_name = "_avi_impl1_config_defaults"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, config_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load Implementation 1 config defaults from {config_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    for name in dir(module):
        if name.startswith("__"):
            continue
        if hasattr(config, name):
            continue
        setattr(config, name, getattr(module, name))


def _ensure_dynamic_package(package_name: str, package_dir: Path) -> None:
    if package_name in sys.modules:
        return
    module = types.ModuleType(package_name)
    module.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    module.__package__ = package_name
    sys.modules[package_name] = module


def _load_module_from_file(module_name: str, file_path: Path):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_stitch_operator_assay_components() -> dict[str, Any]:
    global _STITCH_OPERATOR_ASSAY_COMPONENTS
    if _STITCH_OPERATOR_ASSAY_COMPONENTS is not None:
        return _STITCH_OPERATOR_ASSAY_COMPONENTS

    implementation_root = _ensure_stitch_operator_import_paths()
    _merge_stitch_operator_config_defaults(implementation_root)
    package_root = implementation_root / "stitch_operator"
    package_name = "_avi_impl1_stitch_operator"
    _ensure_dynamic_package(package_name, package_root)
    _ensure_dynamic_package(f"{package_name}.services", package_root / "services")
    try:
        _load_module_from_file(f"{package_name}.bootstrap", package_root / "bootstrap.py")
        settings_mod = _load_module_from_file(f"{package_name}.settings", package_root / "settings.py")
        state_mod = _load_module_from_file(f"{package_name}.state", package_root / "state.py")
        assay_service_mod = _load_module_from_file(
            f"{package_name}.services.assay",
            package_root / "services" / "assay.py",
        )
        assay_embed_mod = _load_module_from_file(f"{package_name}.assay_embed", package_root / "assay_embed.py")
    except Exception as exc:
        raise RuntimeError(
            f"Could not import Implementation 1 assay modules from {implementation_root}: {type(exc).__name__}: {exc}"
        ) from exc

    _STITCH_OPERATOR_ASSAY_COMPONENTS = {
        "settings": settings_mod,
        "state": state_mod,
        "assay_service": assay_service_mod,
        "assay_embed": assay_embed_mod,
    }
    return _STITCH_OPERATOR_ASSAY_COMPONENTS


class EmbeddedAssayWorkspaceController:
    """Minimal controller surface required by EmbeddedAssayUI."""

    def __init__(self) -> None:
        components = _load_stitch_operator_assay_components()
        settings_mod = components["settings"]
        state_mod = components["state"]
        assay_service_mod = components["assay_service"]

        self._state_mod = state_mod
        self.settings_store = settings_mod.OperatorSettingsStore()
        self.settings = self.settings_store.load()
        self.assay = assay_service_mod.AssayService(self.settings)
        self.state = state_mod.OperatorState()
        self._update(status_message="Assay workspace ready.")
        self.refresh_readiness()
        if getattr(self.assay.profile, "last_run_dir", ""):
            self._set_assay_state({"run_dir": self.assay.profile.last_run_dir})

    def snapshot(self):
        return deepcopy(self.state)

    def _update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self.state, key, value)
        self.state.updated_at = time.time()

    def _set_assay_state(self, payload: dict[str, Any]) -> None:
        assay_state = replace(
            self.state.assay,
            run_dir=str(payload.get("run_dir", self.state.assay.run_dir) or self.state.assay.run_dir),
            preview_image_path=str(
                payload.get("preview_path", payload.get("preview_image_path", self.state.assay.preview_image_path))
                or self.state.assay.preview_image_path
            ),
            processed_dir=str(payload.get("processing_dir", self.state.assay.processed_dir) or self.state.assay.processed_dir),
            processed_at=str(payload.get("processed_at", self.state.assay.processed_at) or self.state.assay.processed_at),
            pdf_path=str(
                payload.get("pdf_path", payload.get("summary_pdf", payload.get("report_pdf", self.state.assay.pdf_path)))
                or self.state.assay.pdf_path
            ),
            processing_json=str(payload.get("processing_json", self.state.assay.processing_json) or self.state.assay.processing_json),
            summary_csv_path=str(
                payload.get("per_vial_summary_csv", self.state.assay.summary_csv_path) or self.state.assay.summary_csv_path
            ),
            upload_status=str(payload.get("upload_status", self.state.assay.upload_status) or self.state.assay.upload_status),
            unique_crossings_total=int(
                payload.get("unique_threshold_crossings_total", self.state.assay.unique_crossings_total)
                or self.state.assay.unique_crossings_total
            ),
            duration_s=float(
                payload.get("duration_s", payload.get("assay_duration_s", self.state.assay.duration_s))
                or self.state.assay.duration_s
            ),
            per_vial_summary=list(payload.get("per_vial_summary_rows", self.state.assay.per_vial_summary) or self.state.assay.per_vial_summary),
        )
        self.state.assay = assay_state
        self.state.updated_at = time.time()

    def refresh_readiness(self) -> None:
        assay_status = self.assay.status()
        readiness = self._state_mod.ReadinessState(
            homed=False,
            model_ready=False,
            channel_background_ready=False,
            channel_calibration_ready=False,
            assay_background_ready=bool(assay_status.get("background_ready", False)),
            assay_calibration_ready=bool(assay_status.get("calibration_ready", False)),
            active_profile=str(assay_status.get("profile", "") or ""),
            channel_camera="unused",
            assay_camera=str(assay_status.get("camera", "unknown") or "unknown"),
        )
        self.state.readiness = readiness
        self.state.updated_at = time.time()
        if self.assay.profile.last_run_dir and not self.state.assay.run_dir:
            self._set_assay_state({"run_dir": self.assay.profile.last_run_dir})

    def assay_profile_summary(self) -> dict[str, Any]:
        return dict(self.assay.profile_summary())

    def patch_assay_profile_fields(self, **fields: Any) -> None:
        self.assay.patch_profile_fields(**fields)
        self.refresh_readiness()

    def set_active_profile(self, profile_name: str) -> None:
        name = str(profile_name or "").strip()
        if not name:
            raise RuntimeError("Active assay profile name cannot be empty.")
        self.settings.active_assay_profile = name
        self.settings_store.save(self.settings)
        self.assay.load_profile(name)
        self.refresh_readiness()


def get_embedded_assay_ui_class():
    return _load_stitch_operator_assay_components()["assay_embed"].EmbeddedAssayUI


def create_embedded_assay_controller() -> EmbeddedAssayWorkspaceController:
    return EmbeddedAssayWorkspaceController()


def _append_channel_artifact_trace(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "subsystem": "channel_artifacts",
        "event": str(event),
        "module_path": str(Path(__file__).resolve()),
    }
    for key, value in fields.items():
        if isinstance(value, Path):
            payload[key] = str(value)
        else:
            payload[key] = value
    try:
        CHANNEL_ARTIFACT_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CHANNEL_ARTIFACT_TRACE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass


def _estimate_move_time_for_step_delay(distance_mm: float, step_delay: float) -> float | None:
    distance = abs(float(distance_mm))
    if distance <= 0.0:
        return None
    timing_factor = float(getattr(config, "TIMING_FACTOR", 1.0) or 1.0)
    total_steps = max(1, int(round(distance / float(config.MM_PER_STEP))))
    return (2.0 * total_steps * float(step_delay)) / timing_factor


def _ensure_channel_photo_position(*, reason: str) -> dict[str, Any]:
    try:
        import motion
    except Exception as exc:
        raise RuntimeError(f"Channel photo position guard could not import motion module: {exc}") from exc

    target_mm = float(CHANNEL_PHOTO_POSITION_MM)
    tolerance_mm = float(CHANNEL_PHOTO_POSITION_TOL_MM)
    requested_move = False
    before_position = float(motion.get_current_position())
    fast_step_delay = float(
        getattr(
            config,
            "CHANNEL_PHOTO_STEP_DELAY",
            getattr(config, "HOME_STEP_DELAY", getattr(config, "DEFAULT_STEP_DELAY", 0.00010)),
        )
    )
    planned_move_time = _estimate_move_time_for_step_delay(target_mm - before_position, fast_step_delay)

    if abs(before_position - target_mm) > tolerance_mm:
        requested_move = True
        motion.move_to_absolute(target_mm, planned_move_time)
        after_first_move = float(motion.get_current_position())
        if abs(after_first_move - target_mm) > tolerance_mm:
            retry_move_time = _estimate_move_time_for_step_delay(target_mm - after_first_move, fast_step_delay)
            motion.move_to_absolute(target_mm, retry_move_time)
        final_position = float(motion.get_current_position())
    else:
        final_position = before_position

    _append_channel_artifact_trace(
        "ensure_channel_photo_position",
        reason=reason,
        target_mm=target_mm,
        tolerance_mm=tolerance_mm,
        before_position_mm=before_position,
        requested_move=requested_move,
        requested_move_time_s=planned_move_time,
        final_position_mm=final_position,
    )

    if abs(final_position - target_mm) > tolerance_mm:
        raise RuntimeError(
            f"Channel photo position guard failed for {reason}: expected {target_mm:.2f} mm, "
            f"but gantry position is {final_position:.2f} mm."
        )

    return {
        "target_mm": target_mm,
        "tolerance_mm": tolerance_mm,
        "before_position_mm": before_position,
        "requested_move": requested_move,
        "final_position_mm": final_position,
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
class SexingCameraSettings:
    backend: str
    camera_index: int


@dataclass(frozen=True)
class Fin6SetupStatus:
    settings_path: Path
    settings_file_exists: bool
    channel: Fin6ChannelSettings
    sexing: SexingCameraSettings
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

    for var_name, default_value in _DEFAULT_NUMERIC_SETTINGS.items():
        raw_value = saved.get(var_name)
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            numeric_value = default_value
        if var_name == "channel_mm_var" and abs(numeric_value - 111.0) < 1e-6 and abs(default_value - 111.0) > 1e-6:
            numeric_value = default_value
        if str(normalized.get(var_name)) != str(numeric_value):
            normalized[var_name] = numeric_value
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
    sexing = SexingCameraSettings(
        backend=str(saved.get("sexing_camera_backend_var") or "rpicam"),
        camera_index=_to_int(saved.get("sexing_camera_index_var"), 0),
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
        sexing=sexing,
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
        "sexing": {
            "camera_backend": status.sexing.backend,
            "camera_index": int(status.sexing.camera_index),
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
    selected_stable = None if selected is None else selected.stable_path
    assay_selected = describe_camera_selection(
        status.assay.camera_device,
        role="assay",
        preferred_hint=status.assay.camera_preferred_hint,
    )
    assay_stable = None if assay_selected is None else assay_selected.stable_path

    def _channel_rank(device) -> tuple[int, str, str]:
        stable = device.stable_path or device.device_path
        if selected_stable and stable == selected_stable:
            return (0, device.card_name.lower(), stable)
        if device.is_brio and assay_stable and stable == assay_stable:
            return (2, device.card_name.lower(), stable)
        if device.is_brio:
            return (1, device.card_name.lower(), stable)
        return (3, device.card_name.lower(), stable)

    visible_devices = [
        device for device in devices
        if device.is_brio or (selected_stable and (device.stable_path or device.device_path) == selected_stable)
    ]
    visible_devices.sort(key=_channel_rank)

    items: list[dict[str, Any]] = []
    for device in visible_devices:
        label_parts = [device.card_name]
        if device.is_brio:
            label_parts.append("Brio")
        stable = device.stable_path or device.device_path
        if stable:
            label_parts.append(stable)
        role_guess = "channel"
        if assay_stable and stable == assay_stable:
            role_guess = "assay"
        items.append(
            {
                "label": " | ".join(label_parts),
                "device_path": device.device_path,
                "stable_path": device.stable_path,
                "card_name": device.card_name,
                "preferred_hint": str(device.by_id_path or device.by_path_path or device.stable_path or device.device_path),
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


def _list_pihq_cameras() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        from picamera2 import Picamera2  # type: ignore

        info = Picamera2.global_camera_info() or []
    except Exception:
        info = []
    for idx, info_item in enumerate(info):
        model = str(info_item.get("Model", "") or info_item.get("model", "") or "Pi Camera").strip()
        location = str(info_item.get("Location", "") or info_item.get("location", "") or "").strip()
        label_parts = [f"Pi Camera {idx}"]
        if model:
            label_parts.append(model)
        if location:
            label_parts.append(location)
        items.append(
            {
                "label": " | ".join(label_parts),
                "camera_index": idx,
                "model": model,
                "location": location,
            }
        )
    if not items:
        items.append(
            {
                "label": "Pi Camera 0 | Default ribbon camera",
                "camera_index": 0,
                "model": "Default",
                "location": "",
            }
        )
    return items


def list_camera_role_assignments() -> dict[str, Any]:
    from vision.fin6.camera_sources import describe_camera_selection, list_video_devices

    status = get_setup_status()
    devices = list_video_devices(prefer_index_zero=True)

    def _role_devices(selected_device: str, selected_hint: str, *, role: str, auto_label: str) -> dict[str, Any]:
        selected = describe_camera_selection(selected_device, role=role, preferred_hint=selected_hint)
        channel_selected = describe_camera_selection(
            status.channel.device,
            role="channel",
            preferred_hint=status.channel.preferred_hint,
        )
        assay_selected = describe_camera_selection(
            status.assay.camera_device,
            role="assay",
            preferred_hint=status.assay.camera_preferred_hint,
        )
        selected_stable = None if selected is None else selected.stable_path
        channel_stable = None if channel_selected is None else channel_selected.stable_path
        assay_stable = None if assay_selected is None else assay_selected.stable_path

        def _role_rank(device) -> tuple[int, str, str]:
            stable = device.stable_path or device.device_path
            if selected_stable and stable == selected_stable:
                return (0, device.card_name.lower(), stable)
            if role == "channel":
                if device.is_brio and assay_stable and stable == assay_stable:
                    return (2, device.card_name.lower(), stable)
                if device.is_brio:
                    return (1, device.card_name.lower(), stable)
                return (3, device.card_name.lower(), stable)
            if role == "assay":
                if assay_stable and stable == assay_stable:
                    return (0, device.card_name.lower(), stable)
                if channel_stable and stable == channel_stable:
                    return (3, device.card_name.lower(), stable)
                if not device.is_brio:
                    return (1, device.card_name.lower(), stable)
                return (2, device.card_name.lower(), stable)
            return (1, device.card_name.lower(), stable)

        role_devices = list(devices)
        if role == "channel":
            role_devices = [
                device for device in devices
                if device.is_brio or (selected_stable and (device.stable_path or device.device_path) == selected_stable)
            ]
        role_devices.sort(key=_role_rank)

        items: list[dict[str, Any]] = []
        for device in role_devices:
            label_parts = [device.card_name]
            if device.is_brio:
                label_parts.append("Brio")
            stable = device.stable_path or device.device_path
            if stable:
                label_parts.append(stable)
            items.append(
                {
                    "label": " | ".join(label_parts),
                    "stable_path": device.stable_path,
                    "card_name": device.card_name,
                    "preferred_hint": str(device.by_id_path or device.by_path_path or device.stable_path or device.device_path),
                    "selected": bool(selected is not None and stable == selected.stable_path),
                }
            )
        return {
            "auto_label": auto_label,
            "selected_device": str(selected_device),
            "selected_hint": str(selected_hint),
            "devices": items,
        }

    return {
        "channel": _role_devices(
            status.channel.device,
            status.channel.preferred_hint,
            role="channel",
            auto_label="Auto-detect channel camera",
        ),
        "sexing": {
            "backend": status.sexing.backend,
            "selected_index": int(status.sexing.camera_index),
            "devices": _list_pihq_cameras(),
        },
        "assay": _role_devices(
            status.assay.camera_device,
            status.assay.camera_preferred_hint,
            role="assay",
            auto_label="Auto-detect assay camera",
        ),
    }


def save_camera_role_assignments(
    *,
    channel_device: str,
    channel_preferred_hint: str,
    sexing_camera_index: int,
    assay_device: str,
    assay_preferred_hint: str,
) -> dict[str, Any]:
    normalized = normalize_settings_file(persist=False)
    channel_device_text = str(channel_device or "").strip()
    assay_device_text = str(assay_device or "").strip()
    normalized["channel_device_var"] = channel_device_text or "auto:channel"
    normalized["channel_preferred_hint_var"] = str(channel_preferred_hint or "").strip()
    normalized["sexing_camera_backend_var"] = "rpicam"
    normalized["sexing_camera_index_var"] = int(sexing_camera_index)
    normalized["assay_camera_device_var"] = assay_device_text or "auto:assay"
    normalized["assay_camera_preferred_hint_var"] = str(assay_preferred_hint or "").strip()
    _save_settings_file(normalized)
    return {
        "ok": True,
        "message": "Camera role assignments saved.",
        "roles": list_camera_role_assignments(),
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
    position_guard = _ensure_channel_photo_position(reason="capture_channel_background")

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
        "position_guard": position_guard,
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
    source_path = CHANNEL_SETUP_SOURCE_PATH
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    position_guard = _ensure_channel_photo_position(reason="capture_channel_preview")

    with BrioCamera(
        BrioConfig(
            device=channel.device,
            width=channel.width,
            height=channel.height,
            fps=channel.fps,
            preferred_hint=channel.preferred_hint,
            role="channel",
            warmup_frames=4,
            flush_grabs=1,
            reconnect_attempts=0,
            reconnect_sleep_s=0.05,
            post_open_settle_s=0.01,
        )
    ) as camera:
        frame_bgr = camera.read()

    if not cv2.imwrite(str(source_path), frame_bgr):
        raise IOError(f"Could not save setup source image to {source_path}")

    preview_bgr = frame_bgr
    preview_max_width = 1280
    if preview_bgr.shape[1] > preview_max_width:
        scale = preview_max_width / float(preview_bgr.shape[1])
        preview_bgr = cv2.resize(
            preview_bgr,
            (preview_max_width, max(1, int(round(preview_bgr.shape[0] * scale)))),
            interpolation=cv2.INTER_AREA,
        )

    if not cv2.imwrite(str(preview_path), preview_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80]):
        raise IOError(f"Could not save setup preview image to {preview_path}")
    try:
        ok, encoded = cv2.imencode(".jpg", preview_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        preview_b64 = base64.b64encode(encoded.tobytes()).decode("ascii") if ok else ""
    except Exception:
        preview_b64 = ""

    _append_channel_artifact_trace(
        "capture_setup_preview",
        camera_device=channel.device,
        preferred_hint=channel.preferred_hint,
        source_path=source_path,
        source_shape=list(frame_bgr.shape),
        preview_path=preview_path,
        preview_shape=list(preview_bgr.shape),
        saved_background_path=channel.background_path,
        saved_background_touched=False,
        position_guard=position_guard,
    )

    return {
        "preview_path": str(preview_path.resolve()),
        "source_path": str(source_path.resolve()),
        "source_size": [int(frame_bgr.shape[1]), int(frame_bgr.shape[0])],
        "preview_size": [int(preview_bgr.shape[1]), int(preview_bgr.shape[0])],
        "background_path": str(channel.background_path.resolve()),
        "preview_jpeg_base64": preview_b64,
        "position_guard": position_guard,
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
    import shutil

    from vision.fin6.fly_x_detector import estimate_channel_crop_from_background, save_calibration

    status = get_setup_status()
    channel = status.channel
    background_source_path = CHANNEL_SETUP_SOURCE_PATH if CHANNEL_SETUP_SOURCE_PATH.exists() else channel.background_path

    if not background_source_path.exists():
        raise FileNotFoundError(_missing_channel_setup_message(status))

    channel.background_path.parent.mkdir(parents=True, exist_ok=True)
    channel.calibration_path.parent.mkdir(parents=True, exist_ok=True)
    background_gray = cv2.imread(str(background_source_path), cv2.IMREAD_GRAYSCALE)
    if background_gray is None:
        raise FileNotFoundError(f"Could not read channel setup image: {background_source_path}")

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
    if background_source_path.resolve() != channel.background_path.resolve():
        shutil.copyfile(background_source_path, channel.background_path)
    _append_channel_artifact_trace(
        "save_channel_calibration",
        background_source_path=background_source_path,
        background_source_shape=list(background_gray.shape),
        saved_background_path=channel.background_path,
        calibration_path=channel.calibration_path,
        left_point_px=list(left_pt),
        right_point_px=list(right_pt),
        channel_mm=resolved_channel_mm,
        copied_source_into_saved_background=background_source_path.resolve() != channel.background_path.resolve(),
    )
    return {
        "background_path": str(Path(channel.background_path).resolve()),
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
    position_guard = _ensure_channel_photo_position(reason="detect_channel_runtime_capture")

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

    _append_channel_artifact_trace(
        "runtime_detect_inputs",
        saved_background_path=channel.background_path,
        saved_background_shape=list(background_bgr.shape),
        calibration_path=channel.calibration_path,
        runtime_frame_source="camera.read",
        runtime_frame_shape=list(frame_bgr.shape),
        position_guard=position_guard,
        raw_output_path=channel.output_dir / "last_channel_raw.jpg",
        annotated_output_path=channel.output_dir / "last_channel_annotated.png",
        mask_output_path=channel.output_dir / "last_channel_mask.png",
        result_output_path=channel.output_dir / "last_channel_result.json",
    )

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
    _append_channel_artifact_trace(
        "runtime_detect_outputs",
        result_status=payload.get("status"),
        fly_remaining=payload.get("fly_remaining"),
        count=len(payload.get("x_positions_mm", []) or []),
        x_positions_mm=payload.get("x_positions_mm"),
        left_point_px=payload.get("left_point_px"),
        right_point_px=payload.get("right_point_px"),
        channel_length_mm=payload.get("channel_length_mm"),
        cropped_output=payload.get("cropped_output"),
        auto_crop_from_background=payload.get("auto_crop_from_background"),
        input_shapes=payload.get("input_shapes"),
        detection_debug=payload.get("detection_debug"),
        raw_path=raw_path,
        annotated_path=annotated_path,
        mask_path=mask_path,
        result_path=result_path,
    )

    return {
        "result": payload,
        "output_dir": channel.output_dir,
        "raw_path": raw_path,
        "annotated_path": annotated_path,
        "mask_path": mask_path,
        "result_path": result_path,
        "position_guard": position_guard,
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


def launch_fin6_gui(*, start_tab: str = "channel") -> subprocess.Popen:
    normalize_settings_file()
    script_path = FIN6_DIR / "fly_tracking_gui.py"
    env = os.environ.copy()
    env["DROSOPHILA_FIN6_RAISE"] = "1"
    env["DROSOPHILA_FIN6_START_TAB"] = str(start_tab or "channel").strip().lower()
    return subprocess.Popen([sys.executable, str(script_path)], cwd=str(FIN6_DIR), env=env)


__all__ = [
    "Fin6AssaySettings",
    "Fin6ChannelSettings",
    "SexingCameraSettings",
    "Fin6SetupStatus",
    "SETTINGS_PATH",
    "capture_channel_background_from_saved_settings",
    "capture_channel_preview_from_saved_settings",
    "detect_channel_once_from_saved_settings",
    "get_setup_status",
    "launch_fin6_gui",
    "list_camera_role_assignments",
    "list_available_cameras",
    "normalize_settings_file",
    "run_assay_from_saved_settings",
    "save_camera_role_assignments",
    "save_channel_calibration_from_points",
    "setup_status_to_dict",
    "update_channel_camera_selection",
]
