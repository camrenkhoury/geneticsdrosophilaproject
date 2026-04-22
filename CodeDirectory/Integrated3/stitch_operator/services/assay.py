from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cv2

from ..bootstrap import PROJECT_ROOT, ensure_repo_paths
from ..settings import OperatorSettings

ensure_repo_paths()
from assay_processing import ProcessingError, manual_upload_run, process_last_assay  # noqa: E402
from assay_profile import AssayProfile, ProfileStore  # noqa: E402
from assay_recording import RecordingError, record_assay_run  # noqa: E402
from assay_tracking import calibrate_assay_interactive, load_assay_calibration, preview_assay_frame, save_assay_calibration  # noqa: E402
from background_manager import (  # noqa: E402
    BackgroundError,
    capture_profile_background,
    current_background_preview_path,
    get_background_store,
    restore_previous_background,
)
from box_upload import (  # noqa: E402
    BoxUploadError,
    discover_legacy_box_settings,
    resolve_effective_box_settings,
    write_box_templates,
)
from camera_sources import describe_camera_selection, open_assay_camera  # noqa: E402
from shared_utils import newest_child_dir  # noqa: E402
from transform_utils import apply_image_transform  # noqa: E402


class AssayService:
    def __init__(self, settings: OperatorSettings):
        self.settings = settings
        self.project_root = (PROJECT_ROOT / "fin6").resolve()
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.profile_store = ProfileStore(self.project_root / "profiles")
        self.runtime_dir = (PROJECT_ROOT / "stitch_operator" / "runtime").resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.preview_path = self.runtime_dir / "assay_preview.png"
        self.live_preview_path = self.runtime_dir / "assay_live_preview.png"
        self._legacy_box_cache = discover_legacy_box_settings(PROJECT_ROOT)
        self._status_cache: Optional[Dict[str, Any]] = None
        self._profile_summary_cache: Optional[Dict[str, Any]] = None
        self.profile = self._load_or_create_profile(settings.active_assay_profile)
        self._sync_profile_defaults()
        self._ensure_operator_calibration_layout()

    def _load_or_create_profile(self, profile_name: str) -> AssayProfile:
        try:
            return self.profile_store.load_profile(profile_name)
        except Exception:
            profile = self.profile_store.create_profile(profile_name)
            return profile

    def _invalidate_cache(self) -> None:
        self._status_cache = None
        self._profile_summary_cache = None

    def _sync_profile_defaults(self) -> None:
        slug = self.profile.slug
        if not self.profile.calibration_path:
            self.profile.calibration_path = str((self.project_root / "calibrations" / f"{slug}_calibration.json").resolve())
        if not self.profile.outputs.output_root:
            self.profile.outputs.output_root = str((self.project_root / "outputs" / "assay").resolve())
        self.save_profile()

    def _junk_physical_indices(self) -> List[int]:
        ignored: List[int] = []
        for index, vial in enumerate(self.settings.vial_definitions, start=1):
            target = str(vial.target_sex or "").lower().strip()
            if target in {"junk", "discard", "unknown"}:
                ignored.append(int(index))
        return ignored

    def _ensure_operator_calibration_layout(self) -> None:
        calibration_path = self.calibration_path
        if not calibration_path.exists():
            return
        try:
            calibration = load_assay_calibration(calibration_path)
        except Exception:
            return

        expected_total = len(self.settings.vial_definitions)
        if expected_total and len(calibration.vials) != expected_total:
            return

        ignored = set(self._junk_physical_indices())
        changed = False
        for index, vial in enumerate(calibration.vials, start=1):
            expected_enabled = int(index) not in ignored
            if bool(vial.enabled) != expected_enabled:
                vial.enabled = expected_enabled
                changed = True
            if index <= expected_total:
                expected_label = str(self.settings.vial_definitions[index - 1].label or f"V{index}")
                if expected_label and str(vial.label or "") != expected_label:
                    vial.label = expected_label
                    changed = True

        current_ignored = sorted(int(x) for x in getattr(calibration, "ignored_physical_indices", []) or [])
        desired_ignored = sorted(int(x) for x in ignored)
        if current_ignored != desired_ignored:
            calibration.ignored_physical_indices = desired_ignored
            changed = True

        if changed:
            save_assay_calibration(calibration_path, calibration)

    def save_profile(self) -> Path:
        self._invalidate_cache()
        return self.profile_store.save_profile(self.profile)

    def load_profile(self, profile_name: str) -> AssayProfile:
        self.profile = self.profile_store.load_profile(profile_name)
        self._sync_profile_defaults()
        self._ensure_operator_calibration_layout()
        return self.profile

    def list_profiles(self) -> List[str]:
        return self.profile_store.list_profile_names()

    @property
    def calibration_path(self) -> Path:
        path = Path(str(self.profile.calibration_path)).expanduser()
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def assay_camera_text(self) -> str:
        try:
            descriptor = describe_camera_selection(
                self.profile.assay_camera.device,
                role="assay",
                preferred_hint=self.profile.assay_camera.preferred_hint,
            )
        except Exception as exc:
            return f"Assay camera unavailable: {exc}"
        if descriptor is None:
            return "Assay camera unavailable"
        return f"{descriptor.card_name} ({descriptor.stable_path})"

    def _box_status(self) -> Dict[str, Any]:
        effective = resolve_effective_box_settings(self.profile.box_upload, legacy_repo_root=PROJECT_ROOT)
        legacy = dict(self._legacy_box_cache or {})
        config_candidate = str(effective.config_file or self.profile.box_upload.config_file or "")
        return {
            "box_enabled": bool(effective.enabled),
            "box_artifact_mode": str(effective.artifact_mode or "summaries"),
            "box_auto_upload_processing": bool(effective.upload_after_processing),
            "box_auto_upload_recording": bool(effective.upload_after_recording),
            "box_upload_backgrounds": bool(effective.upload_backgrounds),
            "box_tokens_file": str(effective.tokens_file or ""),
            "box_config_file": config_candidate,
            "box_legacy_source": str(legacy.get("legacy_source", "") or ""),
            "box_parent_folder_id": str(effective.parent_folder_id or ""),
            "box_folder_prefix": str(effective.folder_prefix or "fly_assay"),
        }

    def status(self) -> Dict[str, Any]:
        if self._status_cache is not None:
            return dict(self._status_cache)
        store = get_background_store(self.profile, self.project_root)
        payload = {
            "profile": self.profile.name,
            "profile_path": str(self.profile_store.profile_path(self.profile.name).resolve()),
            "calibration_ready": self.calibration_path.exists(),
            "calibration_path": str(self.calibration_path),
            "background_ready": store.current_transformed_path.exists() and store.current_meta_path.exists(),
            "background_preview": str(store.current_transformed_path.resolve()) if store.current_transformed_path.exists() else "",
            "background_previous": str(store.previous_transformed_path.resolve()) if store.previous_transformed_path.exists() else "",
            "last_run_dir": self.profile.last_run_dir,
            "camera": self.assay_camera_text(),
            "assay_duration_s": float(self.profile.assay_duration_s),
            "analysis_fps": float(self.profile.analysis.analysis_fps),
            "detector_min_threshold": float(self.profile.detector.min_threshold),
            "detector_min_area": int(self.profile.detector.min_area),
            "detector_max_area": int(self.profile.detector.max_area),
            "motor_pulse_ms": int(self.profile.motor.pulse_ms),
            "motor_pulse_user_configured": bool(getattr(self.profile.motor, "pulse_user_configured", False)),
            "motor_settle_delay_ms": int(self.profile.motor.settle_delay_ms),
            "auto_process_after_recording": bool(self.profile.analysis.auto_process_after_recording),
            "save_mask_video": bool(self.profile.analysis.save_mask_video),
            "save_demo_graphs": bool(getattr(self.profile.outputs, "save_demo_graphs", True)),
            "save_preview_snapshots": bool(self.profile.outputs.save_preview_snapshots),
            "snapshot_interval_s": float(self.profile.outputs.snapshot_interval_s),
        }
        payload.update(self._box_status())
        self._status_cache = dict(payload)
        return dict(payload)

    def profile_summary(self) -> Dict[str, Any]:
        if self._profile_summary_cache is not None:
            return dict(self._profile_summary_cache)
        status = self.status()
        summary = {
            "name": self.profile.name,
            "assay_duration_s": float(self.profile.assay_duration_s),
            "analysis_fps": float(self.profile.analysis.analysis_fps),
            "detector_min_threshold": float(self.profile.detector.min_threshold),
            "detector_min_area": int(self.profile.detector.min_area),
            "detector_max_area": int(self.profile.detector.max_area),
            "motor_pulse_ms": int(self.profile.motor.pulse_ms),
            "motor_pulse_user_configured": bool(getattr(self.profile.motor, "pulse_user_configured", False)),
            "motor_settle_delay_ms": int(self.profile.motor.settle_delay_ms),
            "auto_process_after_recording": bool(self.profile.analysis.auto_process_after_recording),
            "save_demo_graphs": bool(getattr(self.profile.outputs, "save_demo_graphs", True)),
            "box_enabled": bool(status["box_enabled"]),
            "box_artifact_mode": str(status["box_artifact_mode"]),
            "box_auto_upload_processing": bool(status["box_auto_upload_processing"]),
            "box_auto_upload_recording": bool(status["box_auto_upload_recording"]),
            "box_config_file": str(status["box_config_file"] or ""),
            "box_tokens_file": str(status["box_tokens_file"] or ""),
            "box_legacy_source": str(status["box_legacy_source"] or ""),
        }
        self._profile_summary_cache = dict(summary)
        return dict(summary)

    def patch_profile_fields(self, **fields: Any) -> Path:
        if "assay_duration_s" in fields:
            self.profile.assay_duration_s = float(fields["assay_duration_s"])
        if "analysis_fps" in fields:
            self.profile.analysis.analysis_fps = float(fields["analysis_fps"])
        if "detector_min_threshold" in fields:
            self.profile.detector.min_threshold = float(fields["detector_min_threshold"])
        if "detector_min_area" in fields:
            self.profile.detector.min_area = int(float(fields["detector_min_area"]))
        if "detector_max_area" in fields:
            self.profile.detector.max_area = int(float(fields["detector_max_area"]))
        if "motor_pulse_ms" in fields:
            self.profile.motor.pulse_ms = max(1, int(round(float(fields["motor_pulse_ms"]))))
            self.profile.motor.pulse_user_configured = True
        if "motor_settle_delay_ms" in fields:
            self.profile.motor.settle_delay_ms = max(0, int(round(float(fields["motor_settle_delay_ms"]))))
        if "auto_process_after_recording" in fields:
            self.profile.analysis.auto_process_after_recording = bool(fields["auto_process_after_recording"])
        if "save_mask_video" in fields:
            self.profile.analysis.save_mask_video = bool(fields["save_mask_video"])
        if "save_demo_graphs" in fields:
            self.profile.outputs.save_demo_graphs = bool(fields["save_demo_graphs"])
        if "save_preview_snapshots" in fields:
            self.profile.outputs.save_preview_snapshots = bool(fields["save_preview_snapshots"])
        if "snapshot_interval_s" in fields:
            self.profile.outputs.snapshot_interval_s = float(fields["snapshot_interval_s"])
        if "box_enabled" in fields:
            self.profile.box_upload.enabled = bool(fields["box_enabled"])
        if "box_parent_folder_id" in fields:
            self.profile.box_upload.parent_folder_id = str(fields["box_parent_folder_id"] or "").strip()
        if "box_config_file" in fields:
            self.profile.box_upload.config_file = str(fields["box_config_file"] or "").strip()
        if "box_tokens_file" in fields:
            self.profile.box_upload.tokens_file = str(fields["box_tokens_file"] or "").strip()
        if "box_artifact_mode" in fields:
            text = str(fields["box_artifact_mode"] or "").strip()
            if text:
                self.profile.box_upload.artifact_mode = text
        if "box_upload_after_processing" in fields:
            self.profile.box_upload.upload_after_processing = bool(fields["box_upload_after_processing"])
        if "box_upload_after_recording" in fields:
            self.profile.box_upload.upload_after_recording = bool(fields["box_upload_after_recording"])
        if "box_upload_backgrounds" in fields:
            self.profile.box_upload.upload_backgrounds = bool(fields["box_upload_backgrounds"])
        if "box_folder_prefix" in fields:
            text = str(fields["box_folder_prefix"] or "").strip()
            if text:
                self.profile.box_upload.folder_prefix = text
        return self.save_profile()

    def seed_box_templates(self, *, overwrite: bool = True) -> Dict[str, str]:
        target_dir = self.project_root / "box_setup"
        result = write_box_templates(target_dir, overwrite=overwrite, legacy_repo_root=PROJECT_ROOT)
        config_path = str(result.get("config_file", "") or "")
        if config_path:
            self.profile.box_upload.config_file = config_path
            self.save_profile()
        return result

    def capture_background(self, logger: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        record = capture_profile_background(self.profile, self.project_root, logger=logger)
        self.profile.current_background_path = str(record.transformed_path)
        self.profile.background_meta_path = str(get_background_store(self.profile, self.project_root).current_meta_path.resolve())
        self.save_profile()
        return record.to_dict()

    def restore_previous_background(self) -> Dict[str, Any]:
        record = restore_previous_background(self.profile, self.project_root)
        self.profile.current_background_path = str(record.transformed_path)
        self.save_profile()
        return record.to_dict()

    def calibrate(self) -> str:
        bg_path = current_background_preview_path(self.profile, self.project_root)
        if bg_path is None or not bg_path.exists():
            raise BackgroundError("Assay background missing. Capture a background first.")
        self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibrate_assay_interactive(
            background_path=bg_path,
            output_json=self.calibration_path,
            total_vials=len(self.settings.vial_definitions),
            ignored_physical_indices=tuple(self._junk_physical_indices()),
        )
        self._ensure_operator_calibration_layout()
        self.profile.calibration_path = str(self.calibration_path)
        self.save_profile()
        return str(self.calibration_path)

    def _capture_raw_frame(self):
        try:
            with open_assay_camera(
                camera_backend=self.profile.assay_camera.backend,
                width=int(self.profile.assay_camera.width),
                height=int(self.profile.assay_camera.height),
                fps=float(self.profile.assay_camera.fps),
                camera_index=int(self.profile.assay_camera.camera_index),
                camera_device=self.profile.assay_camera.device,
                preferred_hint=self.profile.assay_camera.preferred_hint,
                role="assay",
            ) as camera:
                return camera.read()
        except Exception as exc:
            raise RecordingError(f"Assay camera capture failed: {exc}") from exc

    def capture_preview(self) -> Dict[str, Any]:
        frame_bgr = self._capture_raw_frame()
        transformed = apply_image_transform(frame_bgr, self.profile.transform)
        preview_bgr = transformed.copy()
        info = {"mode": "raw"}

        bg_path = current_background_preview_path(self.profile, self.project_root)
        if bg_path is not None and bg_path.exists() and self.calibration_path.exists():
            background_bgr = cv2.imread(str(bg_path), cv2.IMREAD_COLOR)
            calibration = load_assay_calibration(self.calibration_path)
            preview_images, rows, info = preview_assay_frame(
                background_bgr=background_bgr,
                frame_bgr=transformed,
                calibration=calibration,
                min_area=int(self.profile.detector.min_area),
                max_area=int(self.profile.detector.max_area),
                min_threshold=float(self.profile.detector.min_threshold),
                inner_margin_px=int(self.profile.detector.inner_margin_px),
                no_align=not bool(self.profile.analysis.alignment_enabled),
                max_flies_per_vial=int(self.profile.detector.max_flies_per_vial),
                show_positions=bool(self.profile.analysis.show_positions),
            )
            preview_bgr = preview_images.get("calibration")
            if preview_bgr is None:
                preview_bgr = preview_images.get("aligned")
            if preview_bgr is None:
                preview_bgr = transformed.copy()
            info["rows"] = rows
            info["mode"] = "calibration"

        cv2.imwrite(str(self.preview_path), preview_bgr)
        return {"preview_path": str(self.preview_path.resolve()), "info": info}

    def run_assay(
        self,
        *,
        stop_event=None,
        logger: Optional[Callable[[str], None]] = None,
        preview_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        def _preview_proxy(payload: Dict[str, Any]) -> None:
            preview_bgr = payload.get("preview_bgr")
            if preview_bgr is not None:
                cv2.imwrite(str(self.live_preview_path), preview_bgr)
                payload = dict(payload)
                payload["preview_path"] = str(self.live_preview_path.resolve())
            if preview_callback is not None:
                preview_callback(payload)

        manifest = record_assay_run(
            self.profile,
            self.project_root,
            preview_callback=_preview_proxy,
            stop_event=stop_event,
            logger=logger,
        )
        self.profile.last_run_dir = str(manifest.get("run_dir", "") or "")
        self.save_profile()
        return manifest

    def _load_per_vial_summary(self, csv_path: str | Path) -> List[Dict[str, Any]]:
        path = Path(csv_path)
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows.append(dict(row))
        return rows

    def process_last(self, logger: Optional[Callable[[str], None]] = None, progress_callback=None) -> Dict[str, Any]:
        result = process_last_assay(self.profile, self.project_root, logger=logger, progress_callback=progress_callback)
        self.profile.last_run_dir = str(result.get("run_dir", self.profile.last_run_dir) or self.profile.last_run_dir)
        self.save_profile()
        if result.get("per_vial_summary_csv"):
            result["per_vial_summary_rows"] = self._load_per_vial_summary(result["per_vial_summary_csv"])
        return result

    def upload_last(self, logger: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        run_dir = self.last_run_dir()
        if run_dir is None:
            raise ProcessingError("No assay run is available for upload.")
        return manual_upload_run(run_dir, self.profile.box_upload, artifact_mode=None, logger=logger)

    def last_run_dir(self) -> Optional[Path]:
        if self.profile.last_run_dir:
            path = Path(str(self.profile.last_run_dir)).expanduser()
            if path.exists():
                return path.resolve()
        output_root = Path(str(self.profile.outputs.output_root)).expanduser()
        if not output_root.is_absolute():
            output_root = (self.project_root / output_root).resolve()
        newest = newest_child_dir(output_root, prefix="assay_")
        if newest is not None:
            return newest.resolve()
        return None
