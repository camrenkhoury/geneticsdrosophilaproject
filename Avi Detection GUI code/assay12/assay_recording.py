#!/usr/bin/env python3
"""
Assay recording stage.

The record-first workflow captures a high-FPS raw video and stores a complete
run manifest plus the exact profile/background/calibration snapshots needed for
later offline processing.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import cv2

from assay_profile import AssayProfile
from background_manager import BackgroundError, BackgroundStore, get_background_store
from camera_sources import describe_camera_selection, open_assay_camera
from motor_control import MotorError, VibrationMotor
from shared_utils import ensure_dir, load_json, open_video_writer_with_path, save_json, timestamp_slug, timestamp_iso
from transform_utils import apply_image_transform, describe_transform


class RecordingError(RuntimeError):
    """Raised when a recording run cannot be completed."""


def _resolve_path(project_root: Path, raw_path: str) -> Path:
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def validate_recording_requirements(profile: AssayProfile, project_root: str | Path) -> Dict[str, Path]:
    root = Path(project_root)
    calibration_path = _resolve_path(root, profile.calibration_path)
    if not calibration_path.exists():
        raise RecordingError(
            f"Calibration file does not exist: {calibration_path}. Save or load a calibration before recording."
        )

    store = get_background_store(profile, root)
    if not store.current_meta_path.exists() or not store.current_transformed_path.exists() or not store.current_raw_path.exists():
        raise RecordingError("No active background is available. Capture or select a background before recording.")

    try:
        background_meta = load_json(store.current_meta_path)
    except Exception as exc:
        raise RecordingError(f"Could not read background metadata: {store.current_meta_path}") from exc
    current_signature = str(background_meta.get("transform_signature", "") or "")
    profile_signature = profile.transform.signature()
    if current_signature != profile_signature:
        rebuilt = store.rebuild_current_transform(profile.transform)
        if rebuilt is None:
            raise RecordingError(
                "The active background does not match the current transform and could not be rebuilt. "
                "Capture or import a new background before recording."
            )

    return {
        "calibration_path": calibration_path,
        "background_meta_path": store.current_meta_path,
        "background_raw_path": store.current_raw_path,
        "background_transformed_path": store.current_transformed_path,
    }


def record_assay_run(
    profile: AssayProfile,
    project_root: str | Path,
    *,
    preview_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop_event = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    if logger is None:
        logger = lambda _msg: None
    root = Path(project_root)
    required = validate_recording_requirements(profile, root)

    output_root = _resolve_path(root, profile.outputs.output_root)
    run_name = f"assay_{timestamp_slug()}"
    run_dir = ensure_dir(output_root / run_name)
    save_snapshot_images = bool(profile.outputs.save_preview_snapshots)
    snapshots_dir = ensure_dir(run_dir / "snapshots") if save_snapshot_images else None

    profile_snapshot_path = save_json(run_dir / "profile_snapshot.json", profile.to_dict())
    transform_snapshot_path = save_json(run_dir / "transform_snapshot.json", profile.transform.to_dict())
    calibration_snapshot_path = run_dir / "calibration_snapshot.json"
    shutil.copy2(required["calibration_path"], calibration_snapshot_path)
    shutil.copy2(required["background_meta_path"], run_dir / "background_meta_snapshot.json")
    shutil.copy2(required["background_raw_path"], run_dir / "background_raw_snapshot.png")
    shutil.copy2(required["background_transformed_path"], run_dir / "background_transformed_snapshot.png")

    record_fps = float(profile.assay_camera.fps)
    duration_s = float(profile.assay_duration_s)
    if record_fps <= 0:
        raise RecordingError("Record FPS must be positive.")
    if duration_s <= 0:
        raise RecordingError("Assay duration must be positive.")

    raw_video_requested = run_dir / "raw_video.mp4"
    snapshots_interval_s = max(0.1, float(profile.outputs.snapshot_interval_s))
    capture_info = describe_camera_selection(
        profile.assay_camera.device,
        role="assay",
        preferred_hint=profile.assay_camera.preferred_hint,
    )

    first_frame_shape = None
    frame_count = 0
    camera_index_in_use = None
    raw_video_path: Optional[Path] = None
    writer = None
    started_at = time.monotonic()

    logger(f"Recording assay into {run_dir}")

    need_preview_overlay = bool(save_snapshot_images or (preview_callback is not None))
    try:
        with open_assay_camera(
            camera_backend=profile.assay_camera.backend,
            width=int(profile.assay_camera.width),
            height=int(profile.assay_camera.height),
            fps=float(profile.assay_camera.fps),
            camera_index=int(profile.assay_camera.camera_index),
            camera_device=profile.assay_camera.device,
            preferred_hint=profile.assay_camera.preferred_hint,
            role="assay",
        ) as camera:
            camera_index_in_use = getattr(camera, "camera_index_in_use", None)

            preroll_s = max(0.0, float(profile.record_preroll_s))
            if preroll_s > 0:
                logger(f"Preroll: {preroll_s:.1f}s")
                preroll_end = time.monotonic() + preroll_s
                while time.monotonic() < preroll_end:
                    if stop_event is not None and bool(stop_event.is_set()):
                        raise RecordingError("Recording cancelled during pre-roll.")
                    _ = camera.read()
                    time.sleep(0.01)

            try:
                with VibrationMotor(profile.motor) as motor:
                    if profile.motor.enabled:
                        logger(
                            f"Pulsing motor via {getattr(motor, 'backend_name', 'unknown')} for {profile.motor.pulse_ms} ms"
                        )
                    motor.pulse()
            except MotorError:
                raise
            except Exception as exc:
                raise RecordingError(f"Motor pulse failed: {exc}") from exc

            start_time = time.monotonic()
            next_snapshot_t = 0.0
            frame_interval_s = 1.0 / record_fps

            while True:
                if stop_event is not None and bool(stop_event.is_set()):
                    break
                frame_bgr = camera.read()
                t_now = time.monotonic() - start_time
                if t_now > duration_s:
                    break

                if first_frame_shape is None:
                    h, w = frame_bgr.shape[:2]
                    first_frame_shape = [int(h), int(w)]
                    writer, raw_video_path = open_video_writer_with_path(
                        raw_video_requested,
                        fps=record_fps,
                        frame_size=(w, h),
                    )
                    logger(f"Raw video writer ready: {raw_video_path}")

                assert writer is not None
                writer.write(frame_bgr)

                overlay = None
                if need_preview_overlay:
                    transformed_preview = apply_image_transform(frame_bgr, profile.transform)
                    overlay = transformed_preview.copy()
                    countdown = max(0.0, duration_s - t_now)
                    header = f"Recording {t_now:0.2f}s / {duration_s:0.1f}s  remaining {countdown:0.1f}s"
                    cv2.putText(overlay, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
                    cv2.putText(overlay, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (20, 20, 20), 1, cv2.LINE_AA)

                if save_snapshot_images and overlay is not None and snapshots_dir is not None and t_now + 1e-9 >= next_snapshot_t:
                    cv2.imwrite(str(snapshots_dir / f"snapshot_{int(round(next_snapshot_t * 1000)):05d}ms.png"), overlay)
                    next_snapshot_t += snapshots_interval_s

                if preview_callback is not None and overlay is not None:
                    preview_callback(
                        {
                            "preview_bgr": overlay,
                            "raw_frame_bgr": frame_bgr,
                            "time_s": float(t_now),
                            "frame_index": int(frame_count),
                            "run_dir": str(run_dir),
                        }
                    )

                frame_count += 1
                target_time = start_time + (frame_count * frame_interval_s)
                while True:
                    remaining = target_time - time.monotonic()
                    if remaining <= 0.0:
                        break
                    if stop_event is not None and bool(stop_event.is_set()):
                        break
                    time.sleep(min(0.01, remaining))
    finally:
        if writer is not None:
            writer.release()

    if raw_video_path is None or not raw_video_path.exists():
        raise RecordingError("The assay finished without creating a raw video file.")

    manifest = {
        "schema_version": 1,
        "run_name": run_name,
        "run_dir": str(run_dir.resolve()),
        "created_at": timestamp_iso(),
        "duration_s": duration_s,
        "record_fps": record_fps,
        "frames_recorded": int(frame_count),
        "raw_video_path": str(raw_video_path.resolve()),
        "camera_backend": profile.assay_camera.backend,
        "camera_device_requested": str(profile.assay_camera.device),
        "camera_descriptor": None if capture_info is None else capture_info.to_dict(),
        "camera_index_requested": int(profile.assay_camera.camera_index),
        "camera_index_in_use": None if camera_index_in_use is None else int(camera_index_in_use),
        "frame_shape_hw": list(first_frame_shape or []),
        "transform_description": describe_transform(profile.transform),
        "transform_snapshot_path": str(transform_snapshot_path.resolve()),
        "profile_snapshot_path": str(profile_snapshot_path.resolve()),
        "calibration_snapshot_path": str(calibration_snapshot_path.resolve()),
        "background_raw_snapshot_path": str((run_dir / 'background_raw_snapshot.png').resolve()),
        "background_transformed_snapshot_path": str((run_dir / 'background_transformed_snapshot.png').resolve()),
        "background_meta_snapshot_path": str((run_dir / 'background_meta_snapshot.json').resolve()),
        "record_preroll_s": float(profile.record_preroll_s),
        "motor": profile.motor.to_dict(),
        "analysis_settings": profile.analysis.to_dict(),
        "detector_settings": profile.detector.to_dict(),
        "stopped_early": bool(stop_event is not None and bool(stop_event.is_set())),
        "elapsed_wall_s": float(time.monotonic() - started_at),
    }
    save_json(run_dir / "run_manifest.json", manifest)
    return manifest
