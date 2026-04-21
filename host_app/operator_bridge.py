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

from shared.config.project_paths import (
    ASSAY_OUTPUT_DIR,
    CHANNEL_OUTPUT_DIR,
    FIN6_DIR,
    REPO_ROOT,
    ensure_code_directory_on_path,
    ensure_repo_root_on_path,
)

ensure_repo_root_on_path()
ensure_code_directory_on_path()
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
_REMOTE_ASSAY_CONTROLLER: "EmbeddedAssayWorkspaceController | None" = None
_STITCH_OPERATOR_LABEL = "Integrated3"
_STITCH_OPERATOR_IMPLEMENTATION_ROOT = (REPO_ROOT / "CodeDirectory" / "Integrated3").resolve()
_STITCH_OPERATOR_PACKAGE_NAME = "_avi_integrated3_stitch_operator"


def _prepend_sys_path(path: Path) -> None:
    text = str(path)
    while text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def _ensure_stitch_operator_import_paths() -> Path:
    implementation_root = _STITCH_OPERATOR_IMPLEMENTATION_ROOT
    if not implementation_root.exists():
        raise RuntimeError(f"{_STITCH_OPERATOR_LABEL} directory is missing: {implementation_root}")

    # Keep the shared repository CodeDirectory ahead of the embedded bundle so
    # config/motion/vacuum/vibration resolve from the canonical project copy,
    # while still leaving the Integrated3 fin6 bundle available for its unique
    # assay workspace modules.
    ordered_paths = [
        REPO_ROOT,
        REPO_ROOT / "CodeDirectory",
        implementation_root,
        implementation_root / "fin6",
    ]
    for path in reversed(ordered_paths):
        _prepend_sys_path(path)
    return implementation_root


def _merge_stitch_operator_config_defaults(implementation_root: Path) -> None:
    config_path = implementation_root / "CodeDirectory" / "config.py"
    if not config_path.exists():
        return
    module_name = "_avi_integrated3_config_defaults"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, config_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {_STITCH_OPERATOR_LABEL} config defaults from {config_path}")
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


def _install_stitch_operator_bootstrap_shim(package_name: str, implementation_root: Path) -> None:
    module_name = f"{package_name}.bootstrap"
    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "PROJECT_ROOT", None) == implementation_root:
        return

    module = types.ModuleType(module_name)
    module.__file__ = str(implementation_root / "stitch_operator" / "bootstrap.py")
    module.__package__ = package_name

    def project_root() -> Path:
        return implementation_root

    def ensure_repo_paths() -> Path:
        ordered_paths = [
            REPO_ROOT,
            REPO_ROOT / "CodeDirectory",
            implementation_root,
            implementation_root / "fin6",
        ]
        for path in reversed(ordered_paths):
            _prepend_sys_path(path)
        return implementation_root

    module.project_root = project_root  # type: ignore[attr-defined]
    module.ensure_repo_paths = ensure_repo_paths  # type: ignore[attr-defined]
    module.PROJECT_ROOT = ensure_repo_paths()  # type: ignore[attr-defined]
    sys.modules[module_name] = module


def _load_stitch_operator_assay_components() -> dict[str, Any]:
    global _STITCH_OPERATOR_ASSAY_COMPONENTS
    if _STITCH_OPERATOR_ASSAY_COMPONENTS is not None:
        return _STITCH_OPERATOR_ASSAY_COMPONENTS

    implementation_root = _ensure_stitch_operator_import_paths()
    _merge_stitch_operator_config_defaults(implementation_root)
    package_root = implementation_root / "stitch_operator"
    package_name = _STITCH_OPERATOR_PACKAGE_NAME
    _ensure_dynamic_package(package_name, package_root)
    _ensure_dynamic_package(f"{package_name}.services", package_root / "services")
    try:
        _install_stitch_operator_bootstrap_shim(package_name, implementation_root)
        settings_mod = _load_module_from_file(f"{package_name}.settings", package_root / "settings.py")
        state_mod = _load_module_from_file(f"{package_name}.state", package_root / "state.py")
        assay_service_mod = _load_module_from_file(
            f"{package_name}.services.assay",
            package_root / "services" / "assay.py",
        )
        assay_embed_mod = _load_module_from_file(f"{package_name}.assay_embed", package_root / "assay_embed.py")
    except Exception as exc:
        raise RuntimeError(
            f"Could not import {_STITCH_OPERATOR_LABEL} assay modules from {implementation_root}: {type(exc).__name__}: {exc}"
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


def _get_remote_assay_controller() -> EmbeddedAssayWorkspaceController:
    global _REMOTE_ASSAY_CONTROLLER
    if _REMOTE_ASSAY_CONTROLLER is None:
        _REMOTE_ASSAY_CONTROLLER = EmbeddedAssayWorkspaceController()
    return _REMOTE_ASSAY_CONTROLLER


def _get_remote_assay_service():
    return _get_remote_assay_controller().assay


def _assay_runtime_dir() -> Path:
    runtime_dir = Path(_get_remote_assay_service().runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _assay_preview_path(mode: str) -> Path:
    safe_mode = str(mode or "preview").strip().lower().replace("/", "_")
    return _assay_runtime_dir() / f"assay_preview_{safe_mode}.png"


def _copy_file_if_needed(source: Path, destination: Path) -> Path:
    import shutil

    source_resolved = source.resolve()
    destination_resolved = destination.resolve()
    if source_resolved != destination_resolved:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_resolved, destination_resolved)
    return destination_resolved


def _write_preview_image(mode: str, image_bgr) -> Path:
    import cv2

    preview_path = _assay_preview_path(mode)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(preview_path), image_bgr):
        raise IOError(f"Could not save assay preview image: {preview_path}")
    return preview_path.resolve()


def _load_assay_support_modules() -> dict[str, Any]:
    _ensure_stitch_operator_import_paths()
    import cv2
    from assay_processing import batch_process_folder, process_assay_run
    from assay_tracking import (
        AssayCalibration,
        VialCalibration,
        build_assay_calibration,
        load_assay_calibration,
        preview_assay_frame,
        render_assay_calibration_overlay,
        save_assay_calibration,
    )
    from background_manager import (
        BackgroundError,
        current_background_preview_path,
        get_background_store,
        import_profile_background,
    )
    from shared_utils import load_json
    from transform_utils import apply_image_transform

    return {
        "cv2": cv2,
        "AssayCalibration": AssayCalibration,
        "VialCalibration": VialCalibration,
        "BackgroundError": BackgroundError,
        "build_assay_calibration": build_assay_calibration,
        "load_assay_calibration": load_assay_calibration,
        "preview_assay_frame": preview_assay_frame,
        "render_assay_calibration_overlay": render_assay_calibration_overlay,
        "save_assay_calibration": save_assay_calibration,
        "current_background_preview_path": current_background_preview_path,
        "get_background_store": get_background_store,
        "import_profile_background": import_profile_background,
        "process_assay_run": process_assay_run,
        "batch_process_folder": batch_process_folder,
        "load_json": load_json,
        "apply_image_transform": apply_image_transform,
    }


def _normalize_assay_calibration_payload(payload: dict[str, Any]):
    modules = _load_assay_support_modules()
    calibration_cls = modules["AssayCalibration"]
    if not isinstance(payload, dict):
        raise ValueError("Calibration payload must be a JSON object.")
    return calibration_cls.from_dict(dict(payload))


def _current_assay_background_path() -> Path:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    preview_path = modules["current_background_preview_path"](service.profile, service.project_root)
    if preview_path is None or not Path(preview_path).exists():
        raise FileNotFoundError("Assay background is not available yet.")
    return Path(preview_path).resolve()


def _assay_background_paths() -> dict[str, str]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    store = modules["get_background_store"](service.profile, service.project_root)
    return {
        "current_raw_path": str(Path(store.current_raw_path).resolve()) if Path(store.current_raw_path).exists() else "",
        "current_transformed_path": str(Path(store.current_transformed_path).resolve()) if Path(store.current_transformed_path).exists() else "",
        "current_meta_path": str(Path(store.current_meta_path).resolve()) if Path(store.current_meta_path).exists() else "",
        "previous_raw_path": str(Path(store.previous_raw_path).resolve()) if Path(store.previous_raw_path).exists() else "",
        "previous_transformed_path": str(Path(store.previous_transformed_path).resolve()) if Path(store.previous_transformed_path).exists() else "",
        "previous_meta_path": str(Path(store.previous_meta_path).resolve()) if Path(store.previous_meta_path).exists() else "",
    }


def list_assay_profiles() -> dict[str, Any]:
    controller = _get_remote_assay_controller()
    controller.refresh_readiness()
    return {
        "ok": True,
        "profiles": controller.assay.list_profiles(),
        "active_profile": controller.assay.profile.name,
    }


def get_assay_status() -> dict[str, Any]:
    controller = _get_remote_assay_controller()
    controller.refresh_readiness()
    payload = dict(controller.assay.status())
    payload.update(_assay_background_paths())
    return payload


def get_assay_profile_summary() -> dict[str, Any]:
    controller = _get_remote_assay_controller()
    controller.refresh_readiness()
    return controller.assay_profile_summary()


def patch_assay_profile_fields(**fields: Any) -> dict[str, Any]:
    controller = _get_remote_assay_controller()
    controller.patch_assay_profile_fields(**fields)
    return {
        "ok": True,
        "message": "Assay profile updated.",
        "profile": controller.assay.profile.name,
        "status": get_assay_status(),
        "summary": get_assay_profile_summary(),
    }


def activate_assay_profile(profile_name: str) -> dict[str, Any]:
    controller = _get_remote_assay_controller()
    controller.set_active_profile(profile_name)
    return {
        "ok": True,
        "message": f"Activated assay profile {controller.assay.profile.name}.",
        "profile": controller.assay.profile.name,
        "status": get_assay_status(),
        "summary": get_assay_profile_summary(),
    }


def seed_assay_box_templates(*, overwrite: bool = True) -> dict[str, Any]:
    service = _get_remote_assay_service()
    result = service.seed_box_templates(overwrite=overwrite)
    return {
        "ok": True,
        "message": "Box template files are ready.",
        **result,
    }


def capture_assay_background_from_saved_settings() -> dict[str, Any]:
    service = _get_remote_assay_service()
    record = service.capture_background()
    payload = {
        "ok": True,
        "message": "Assay background captured.",
        **record,
        **_assay_background_paths(),
    }
    return payload


def import_assay_background_from_saved_settings(
    *,
    source_path: str | None = None,
    image_base64: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    import base64

    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    import_path: Path
    temp_path: Path | None = None
    if image_base64:
        raw_bytes = base64.b64decode(image_base64.encode("ascii"))
        suffix = Path(str(filename or "uploaded_background.png")).suffix or ".png"
        temp_path = _assay_runtime_dir() / f"uploaded_assay_background{suffix}"
        temp_path.write_bytes(raw_bytes)
        import_path = temp_path
    else:
        import_path = Path(str(source_path or "")).expanduser().resolve()
    if not import_path.exists():
        raise FileNotFoundError(f"Assay background import source was not found: {import_path}")
    try:
        record = modules["import_profile_background"](
            service.profile,
            service.project_root,
            import_path,
        )
        service.profile.current_background_path = str(record.transformed_path)
        service.profile.background_meta_path = str(modules["get_background_store"](service.profile, service.project_root).current_meta_path.resolve())
        service.save_profile()
        return {
            "ok": True,
            "message": "Assay background imported.",
            **record.to_dict(),
            **_assay_background_paths(),
        }
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def restore_previous_assay_background_from_saved_settings() -> dict[str, Any]:
    service = _get_remote_assay_service()
    record = service.restore_previous_background()
    return {
        "ok": True,
        "message": "Previous assay background restored.",
        **record,
        **_assay_background_paths(),
    }


def rebuild_assay_background_transform_from_saved_settings() -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    store = modules["get_background_store"](service.profile, service.project_root)
    record = store.rebuild_current_transform(service.profile.transform)
    if record is None:
        raise modules["BackgroundError"]("No current assay background is available to rebuild.")
    service.profile.current_background_path = str(record.transformed_path)
    service.profile.background_meta_path = str(store.current_meta_path.resolve())
    service.save_profile()
    return {
        "ok": True,
        "message": "Assay background rebuilt with current transform.",
        **record.to_dict(),
        **_assay_background_paths(),
    }


def load_assay_calibration_data() -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    calibration_path = Path(service.calibration_path).resolve()
    if not calibration_path.exists():
        raise FileNotFoundError(f"Assay calibration does not exist yet: {calibration_path}")
    calibration = modules["load_assay_calibration"](calibration_path)
    return {
        "ok": True,
        "message": "Assay calibration loaded.",
        "calibration_path": str(calibration_path),
        "calibration": calibration.to_dict(),
    }


def save_assay_calibration_data(payload: dict[str, Any]) -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    calibration = _normalize_assay_calibration_payload(payload)
    calibration.background_path = str(_current_assay_background_path())
    calibration_path = Path(service.calibration_path).resolve()
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    modules["save_assay_calibration"](calibration_path, calibration)
    service.profile.calibration_path = str(calibration_path)
    service.save_profile()
    overlay = modules["render_assay_calibration_overlay"](modules["cv2"].imread(str(_current_assay_background_path()), modules["cv2"].IMREAD_COLOR), calibration)
    preview_path = _write_preview_image("calibration", overlay)
    return {
        "ok": True,
        "message": "Assay calibration saved.",
        "calibration_path": str(calibration_path),
        "preview_path": str(preview_path),
        "calibration": calibration.to_dict(),
    }


def _render_assay_preview(
    *,
    mode: str,
    calibration_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    normalized_mode = str(mode or "calibration").strip().lower()
    cv2 = modules["cv2"]
    apply_image_transform = modules["apply_image_transform"]

    if normalized_mode == "background":
        background_path = _current_assay_background_path()
        preview_path = _copy_file_if_needed(background_path, _assay_preview_path("background"))
        return {
            "ok": True,
            "message": "Assay background preview ready.",
            "mode": "background",
            "preview_path": str(preview_path),
            "available_modes": ["background"],
        }

    raw_bgr = service._capture_raw_frame()
    raw_preview_path = _write_preview_image("raw", raw_bgr)
    transformed_bgr = apply_image_transform(raw_bgr, service.profile.transform)
    transform_preview_path = _write_preview_image("transform", transformed_bgr)

    if normalized_mode in {"raw", "transform"}:
        selected_path = raw_preview_path if normalized_mode == "raw" else transform_preview_path
        return {
            "ok": True,
            "message": f"Assay {normalized_mode} preview ready.",
            "mode": normalized_mode,
            "preview_path": str(selected_path),
            "available_modes": ["raw", "transform"],
        }

    background_path = _current_assay_background_path()
    background_bgr = cv2.imread(str(background_path), cv2.IMREAD_COLOR)
    if background_bgr is None:
        raise FileNotFoundError(f"Could not read assay background image: {background_path}")
    if calibration_override is None:
        calibration_path = Path(service.calibration_path).resolve()
        if not calibration_path.exists():
            raise FileNotFoundError(f"Assay calibration does not exist yet: {calibration_path}")
        calibration = modules["load_assay_calibration"](calibration_path)
    else:
        calibration = _normalize_assay_calibration_payload(calibration_override)

    preview_images, rows, info = modules["preview_assay_frame"](
        background_bgr=background_bgr,
        frame_bgr=transformed_bgr,
        calibration=calibration,
        min_area=int(service.profile.detector.min_area),
        max_area=int(service.profile.detector.max_area),
        min_threshold=float(service.profile.detector.min_threshold),
        inner_margin_px=int(service.profile.detector.inner_margin_px),
        no_align=not bool(service.profile.analysis.alignment_enabled),
        max_flies_per_vial=int(service.profile.detector.max_flies_per_vial),
        show_positions=bool(service.profile.analysis.show_positions),
    )
    overlay_bgr = modules["render_assay_calibration_overlay"](background_bgr, calibration)
    _write_preview_image("background", background_bgr)
    _write_preview_image("calibration", preview_images.get("calibration") or overlay_bgr)
    if preview_images.get("aligned") is not None:
        _write_preview_image("aligned", preview_images["aligned"])
    if preview_images.get("annotated") is not None:
        _write_preview_image("annotated", preview_images["annotated"])
    if preview_images.get("mask") is not None:
        _write_preview_image("mask", preview_images["mask"])

    preview_map = {
        "calibration": _assay_preview_path("calibration"),
        "annotated": _assay_preview_path("annotated"),
        "mask": _assay_preview_path("mask"),
        "background": _assay_preview_path("background"),
        "raw": _assay_preview_path("raw"),
        "transform": _assay_preview_path("transform"),
    }
    if normalized_mode == "annotated" and not preview_map["annotated"].exists():
        normalized_mode = "calibration"
    if normalized_mode == "mask" and not preview_map["mask"].exists():
        normalized_mode = "calibration"
    selected_path = preview_map[normalized_mode]
    if not selected_path.exists():
        raise FileNotFoundError(f"Assay preview for mode '{normalized_mode}' is not available.")
    return {
        "ok": True,
        "message": f"Assay {normalized_mode} preview ready.",
        "mode": normalized_mode,
        "preview_path": str(selected_path.resolve()),
        "rows": rows,
        "info": info,
        "available_modes": [name for name, path in preview_map.items() if path.exists()],
    }


def capture_assay_preview_from_saved_settings(
    *,
    mode: str = "calibration",
    calibration_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _render_assay_preview(mode=mode, calibration_override=calibration_override)


def run_integrated3_assay_from_active_profile() -> dict[str, Any]:
    service = _get_remote_assay_service()
    result = service.run_assay()
    return {
        "ok": True,
        "message": "Integrated3 assay recording completed.",
        **result,
    }


def test_assay_calibration_from_saved_settings(
    *,
    calibration_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _render_assay_preview(mode="annotated", calibration_override=calibration_override)
    payload["message"] = "Assay calibration test preview ready."
    return payload


def process_last_assay_from_saved_settings() -> dict[str, Any]:
    service = _get_remote_assay_service()
    result = service.process_last()
    return {
        "ok": True,
        "message": "Processed latest assay run.",
        **result,
    }


def process_selected_assay_run(run_dir: str) -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    result = modules["process_assay_run"](run_dir, profile_override=service.profile)
    if result.get("run_dir"):
        service.profile.last_run_dir = str(result["run_dir"])
        service.save_profile()
    return {
        "ok": True,
        "message": "Processed selected assay run.",
        **result,
    }


def batch_process_assay_runs(folder: str) -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    results = modules["batch_process_folder"](folder, profile_override=service.profile)
    return {
        "ok": True,
        "message": f"Batch processed {len(results)} assay runs.",
        "results": results,
    }


def upload_last_assay_from_saved_settings() -> dict[str, Any]:
    service = _get_remote_assay_service()
    result = service.upload_last()
    return {
        "ok": True,
        "message": "Uploaded latest assay run.",
        **result,
    }


def get_latest_assay_run_manifest() -> dict[str, Any]:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    run_dir = service.last_run_dir()
    if run_dir is None:
        raise FileNotFoundError("No assay run is available yet.")
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Assay run manifest is missing: {manifest_path}")
    return {
        "ok": True,
        "run_dir": str(run_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest": modules["load_json"](manifest_path),
    }


def resolve_latest_assay_artifact_path(kind: str) -> Path:
    modules = _load_assay_support_modules()
    service = _get_remote_assay_service()
    run_dir = service.last_run_dir()
    if run_dir is None:
        raise FileNotFoundError("No assay run is available yet.")
    manifest_path = run_dir / "run_manifest.json"
    manifest = modules["load_json"](manifest_path) if manifest_path.exists() else {}
    latest_processing_path = run_dir / "processed" / "latest_processing.json"
    latest_processing = modules["load_json"](latest_processing_path) if latest_processing_path.exists() else {}
    kind_key = str(kind or "").strip().lower()

    def _candidate(raw: str | Path | None) -> Path | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = (run_dir / path).resolve()
        return path.resolve()

    candidates: list[Path] = []
    if kind_key == "raw_video":
        for raw in (
            manifest.get("raw_video_path"),
            run_dir / "raw_video.mp4",
            run_dir / "raw_video.avi",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    elif kind_key == "annotated_video":
        for raw in (
            latest_processing.get("annotated_video_path"),
            manifest.get("annotated_video_path"),
            run_dir / "processed" / "annotated_video.mp4",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    elif kind_key == "mask_video":
        for raw in (
            latest_processing.get("mask_video_path"),
            manifest.get("mask_video_path"),
            run_dir / "processed" / "mask_video.mp4",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    elif kind_key == "per_vial_summary_csv":
        for raw in (
            latest_processing.get("per_vial_summary_csv"),
            run_dir / "processed" / "per_vial_summary.csv",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    elif kind_key == "per_fly_summary_csv":
        for raw in (
            latest_processing.get("per_fly_summary_csv"),
            run_dir / "processed" / "per_fly_summary.csv",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    elif kind_key == "report_pdf":
        for raw in (
            latest_processing.get("report_pdf"),
            latest_processing.get("pdf_path"),
            run_dir / "processed" / "report.pdf",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    elif kind_key == "processing_json":
        for raw in (
            latest_processing.get("processing_session_json"),
            latest_processing.get("processing_json"),
            run_dir / "processed" / "processing_session.json",
        ):
            candidate = _candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
    else:
        raise ValueError(f"Unsupported assay artifact kind: {kind}")

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"Latest assay artifact '{kind}' is not available yet.")


def resolve_assay_preview_artifact_path(mode: str) -> Path:
    preview_path = _assay_preview_path(mode)
    if not preview_path.exists():
        raise FileNotFoundError(f"Assay preview '{mode}' is not available yet.")
    return preview_path.resolve()


def resolve_assay_background_artifact_path(which: str) -> Path:
    paths = _assay_background_paths()
    key = "current_transformed_path" if str(which).strip().lower() == "current" else "previous_transformed_path"
    value = str(paths.get(key, "") or "").strip()
    if not value:
        raise FileNotFoundError(f"Assay background '{which}' is not available yet.")
    path = Path(value).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Assay background '{which}' is not available yet.")
    return path


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
    "activate_assay_profile",
    "batch_process_assay_runs",
    "capture_assay_background_from_saved_settings",
    "capture_assay_preview_from_saved_settings",
    "capture_channel_background_from_saved_settings",
    "capture_channel_preview_from_saved_settings",
    "detect_channel_once_from_saved_settings",
    "get_assay_profile_summary",
    "get_assay_status",
    "get_latest_assay_run_manifest",
    "get_setup_status",
    "import_assay_background_from_saved_settings",
    "launch_fin6_gui",
    "list_camera_role_assignments",
    "list_assay_profiles",
    "list_available_cameras",
    "normalize_settings_file",
    "patch_assay_profile_fields",
    "process_last_assay_from_saved_settings",
    "process_selected_assay_run",
    "rebuild_assay_background_transform_from_saved_settings",
    "resolve_assay_background_artifact_path",
    "resolve_assay_preview_artifact_path",
    "resolve_latest_assay_artifact_path",
    "restore_previous_assay_background_from_saved_settings",
    "run_integrated3_assay_from_active_profile",
    "run_assay_from_saved_settings",
    "save_camera_role_assignments",
    "save_assay_calibration_data",
    "save_channel_calibration_from_points",
    "seed_assay_box_templates",
    "setup_status_to_dict",
    "test_assay_calibration_from_saved_settings",
    "load_assay_calibration_data",
    "update_channel_camera_selection",
    "upload_last_assay_from_saved_settings",
]
