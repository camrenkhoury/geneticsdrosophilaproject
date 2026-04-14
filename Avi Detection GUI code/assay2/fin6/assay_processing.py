#!/usr/bin/env python3
"""
Offline assay processing.

The assay workflow records a raw high-FPS video first, then processes later using
saved profile, transform, background, and calibration snapshots. This keeps the
operator workflow simple while still exporting reliable threshold-crossing,
height, and velocity metrics.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import pandas as pd

from assay_profile import AssayProfile
from assay_tracking import (
    AssayCalibration,
    Detection,
    MultiVialTracker,
    Track,
    annotate_assay_frame,
    assay_mask_to_bgr,
    detect_assay_frame,
    detections_to_dataframe,
    generate_graphs_and_pdf,
    load_assay_calibration,
    tracks_to_dataframe,
)
from box_upload import BoxUploadError, should_auto_upload, upload_run_artifacts
from shared_utils import ensure_dir, load_json, newest_child_dir, open_video_writer_with_path, save_json, timestamp_iso, timestamp_slug
from transform_utils import TransformSettings, apply_image_transform, describe_transform


class ProcessingError(RuntimeError):
    """Raised when offline processing cannot complete."""


THRESHOLD_CROSSING_COLUMNS = [
    "unique_event_id",
    "assay_tube_index",
    "physical_vial_index",
    "display_id",
    "internal_track_id",
    "crossing_frame_index",
    "crossing_time_s",
    "crossing_x_px",
    "crossing_y_px",
    "threshold_distance_px",
    "threshold_distance_mm",
    "deduplicated",
]


@dataclass
class ProcessingContext:
    run_dir: Path
    raw_video_path: Path
    run_manifest_path: Optional[Path]
    run_manifest: Dict[str, Any]
    profile: AssayProfile
    transform: TransformSettings
    calibration: AssayCalibration
    background_bgr: np.ndarray
    background_path: Path
    processing_dir: Path


def _resolve_path(path: str | Path, base_dir: Path) -> Path:
    p = Path(str(path)).expanduser()
    if p.is_absolute():
        return p
    return (base_dir / p).resolve()


def _find_raw_video(run_dir: Path, run_manifest: Dict[str, Any]) -> Path:
    candidates: List[Path] = []
    raw_from_manifest = str(run_manifest.get("raw_video_path", "") or "").strip()
    if raw_from_manifest:
        candidates.append(_resolve_path(raw_from_manifest, run_dir))
    candidates.extend([
        run_dir / "raw_video.mp4",
        run_dir / "raw_video.avi",
    ])
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise ProcessingError(f"Could not locate the raw assay video in {run_dir}")


def _load_profile_from_run(run_dir: Path, run_manifest: Dict[str, Any], profile_override: Optional[AssayProfile]) -> AssayProfile:
    if profile_override is not None:
        return profile_override
    profile_snapshot = run_dir / "profile_snapshot.json"
    if profile_snapshot.exists():
        return AssayProfile.from_dict(load_json(profile_snapshot))
    if run_manifest.get("profile_snapshot_path"):
        snapshot_path = _resolve_path(str(run_manifest["profile_snapshot_path"]), run_dir)
        if snapshot_path.exists():
            return AssayProfile.from_dict(load_json(snapshot_path))
    raise ProcessingError(
        f"Profile snapshot was not found for run {run_dir}. Re-record the run or provide a profile override."
    )


def _load_transform_from_run(run_dir: Path, run_manifest: Dict[str, Any], profile: AssayProfile) -> TransformSettings:
    transform_snapshot = run_dir / "transform_snapshot.json"
    if transform_snapshot.exists():
        return TransformSettings.from_dict(load_json(transform_snapshot))
    if run_manifest.get("transform_snapshot_path"):
        snapshot_path = _resolve_path(str(run_manifest["transform_snapshot_path"]), run_dir)
        if snapshot_path.exists():
            return TransformSettings.from_dict(load_json(snapshot_path))
    return profile.transform


def _load_calibration_from_run(run_dir: Path, run_manifest: Dict[str, Any], profile: AssayProfile) -> Tuple[AssayCalibration, Path]:
    candidates: List[Path] = [run_dir / "calibration_snapshot.json"]
    if run_manifest.get("calibration_snapshot_path"):
        candidates.append(_resolve_path(str(run_manifest["calibration_snapshot_path"]), run_dir))
    if profile.calibration_path:
        candidates.append(_resolve_path(profile.calibration_path, run_dir))
    for path in candidates:
        if path.exists():
            return load_assay_calibration(path), path.resolve()
    raise ProcessingError(
        f"Calibration snapshot was not found for run {run_dir}. Save or copy a calibration JSON into the run folder."
    )


def _load_background_from_run(run_dir: Path, run_manifest: Dict[str, Any], profile: AssayProfile) -> Tuple[np.ndarray, Path]:
    candidates: List[Path] = [run_dir / "background_transformed_snapshot.png"]
    if run_manifest.get("background_transformed_snapshot_path"):
        candidates.append(_resolve_path(str(run_manifest["background_transformed_snapshot_path"]), run_dir))
    if profile.current_background_path:
        candidates.append(_resolve_path(profile.current_background_path, run_dir))
    for path in candidates:
        if path.exists():
            background_bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if background_bgr is not None:
                return background_bgr, path.resolve()
    raise ProcessingError(
        f"Background snapshot was not found for run {run_dir}. Capture or import a background before recording."
    )


def load_processing_context(run_dir_or_video: str | Path, profile_override: Optional[AssayProfile] = None) -> ProcessingContext:
    input_path = Path(run_dir_or_video).expanduser().resolve()
    run_dir = input_path.parent if input_path.is_file() else input_path
    if not run_dir.exists():
        raise ProcessingError(f"Run directory does not exist: {run_dir}")

    run_manifest_path = run_dir / "run_manifest.json"
    run_manifest = load_json(run_manifest_path) if run_manifest_path.exists() else {}
    raw_video_path = input_path if input_path.is_file() else _find_raw_video(run_dir, run_manifest)
    profile = _load_profile_from_run(run_dir, run_manifest, profile_override)
    transform = _load_transform_from_run(run_dir, run_manifest, profile)
    calibration, calibration_path = _load_calibration_from_run(run_dir, run_manifest, profile)
    background_bgr, background_path = _load_background_from_run(run_dir, run_manifest, profile)
    processing_dir = _make_unique_processing_dir(run_dir)

    # Keep calibration metadata consistent with the transformed background shape.
    expected_hw = [int(background_bgr.shape[0]), int(background_bgr.shape[1])]
    if list(calibration.image_shape_hw) != expected_hw:
        raise ProcessingError(
            "Calibration dimensions do not match the transformed background. "
            f"Calibration HxW={calibration.image_shape_hw}, background HxW={expected_hw}."
        )

    return ProcessingContext(
        run_dir=run_dir,
        raw_video_path=raw_video_path,
        run_manifest_path=run_manifest_path if run_manifest_path.exists() else None,
        run_manifest=run_manifest,
        profile=profile,
        transform=transform,
        calibration=calibration,
        background_bgr=background_bgr,
        background_path=background_path,
        processing_dir=processing_dir,
    )


def _average_frames(frames: Sequence[np.ndarray]) -> np.ndarray:
    if not frames:
        raise ValueError("No frames were provided for averaging.")
    if len(frames) == 1:
        return frames[0].copy()
    stack = np.stack([frame.astype(np.float32) for frame in frames], axis=0)
    return np.clip(np.mean(stack, axis=0), 0, 255).astype(np.uint8)


def _make_unique_processing_dir(run_dir: Path) -> Path:
    processed_root = ensure_dir(run_dir / "processed")
    slug = f"proc_{timestamp_slug()}"
    candidate = processed_root / slug
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = processed_root / f"{slug}_{suffix:02d}"
    return ensure_dir(candidate)


def _greedy_match_pairs(cost_matrix: np.ndarray, max_cost: float) -> List[Tuple[int, int]]:
    matches: List[Tuple[int, int]] = []
    if cost_matrix.size == 0:
        return matches
    used_rows: set[int] = set()
    used_cols: set[int] = set()
    flat: List[Tuple[float, int, int]] = []
    rows, cols = cost_matrix.shape
    for r in range(rows):
        for c in range(cols):
            flat.append((float(cost_matrix[r, c]), r, c))
    flat.sort(key=lambda item: item[0])
    for cost, r, c in flat:
        if cost > max_cost:
            break
        if r in used_rows or c in used_cols:
            continue
        matches.append((r, c))
        used_rows.add(r)
        used_cols.add(c)
    return matches


def _save_snapshot_bundle(
    snapshots_dir: Path,
    frame_index: int,
    time_s: float,
    *,
    raw_bgr: np.ndarray,
    transformed_bgr: np.ndarray,
    aligned_bgr: np.ndarray,
    annotated_bgr: np.ndarray,
    mask_bgr: np.ndarray,
) -> Dict[str, str]:
    stem = f"frame_{int(frame_index):03d}_t{float(time_s):05.2f}s"
    outputs = {
        "snapshot_raw_path": snapshots_dir / f"{stem}_raw.png",
        "snapshot_transformed_path": snapshots_dir / f"{stem}_transformed.png",
        "snapshot_aligned_path": snapshots_dir / f"{stem}_aligned.png",
        "snapshot_annotated_path": snapshots_dir / f"{stem}_annotated.png",
        "snapshot_mask_path": snapshots_dir / f"{stem}_mask.png",
    }
    cv2.imwrite(str(outputs["snapshot_raw_path"]), raw_bgr)
    cv2.imwrite(str(outputs["snapshot_transformed_path"]), transformed_bgr)
    cv2.imwrite(str(outputs["snapshot_aligned_path"]), aligned_bgr)
    cv2.imwrite(str(outputs["snapshot_annotated_path"]), annotated_bgr)
    cv2.imwrite(str(outputs["snapshot_mask_path"]), mask_bgr)
    return {key: str(value.resolve()) for key, value in outputs.items()}


class SnapshotStitchTracker:
    """Minimal per-snapshot stitcher that links detections across adjacent frames only."""

    def __init__(
        self,
        calibration: AssayCalibration,
        *,
        max_flies_per_vial: int,
        max_gap_frames: int = 1,
        x_search_px: float = 26.0,
        y_search_px: float = 64.0,
        max_cost: float = 1.55,
    ) -> None:
        self.calibration = calibration
        self.max_flies_per_vial = max(1, int(max_flies_per_vial))
        self.max_gap_frames = max(0, int(max_gap_frames))
        self.x_search_px = float(x_search_px)
        self.y_search_px = float(y_search_px)
        self.max_cost = float(max_cost)
        self.active_tracks: Dict[int, List[Track]] = {int(v.physical_index): [] for v in calibration.enabled_vials}
        self.completed_tracks: List[Track] = []
        self.next_display_id: Dict[int, int] = {int(v.physical_index): 1 for v in calibration.enabled_vials}
        self.next_internal_id = 1

    def _cost_matrix(self, tracks: Sequence[Track], detections: Sequence[Detection]) -> np.ndarray:
        cost = np.full((len(tracks), len(detections)), fill_value=1e6, dtype=np.float32)
        for ti, track in enumerate(tracks):
            last = track.history[-1] if track.history else None
            area_ref = None
            if last is not None:
                area_ref = max(1.0, float(last.get("bbox_w", 1)) * float(last.get("bbox_h", 1)))
            for di, det in enumerate(detections):
                dx = abs(float(track.last_x) - float(det.center_xy_px[0])) / max(1.0, self.x_search_px)
                dy = abs(float(track.last_y) - float(det.center_xy_px[1])) / max(1.0, self.y_search_px)
                area_bias = 0.0 if area_ref is None else abs(float(det.area_px) - area_ref) / max(45.0, area_ref)
                cost[ti, di] = float(math.sqrt(dx * dx + dy * dy) + 0.08 * area_bias)
        return cost

    def _track_to_row(self, track: Track) -> Dict[str, Any]:
        hist = dict(track.history[-1])
        hist.update(
            {
                "internal_track_id": int(track.internal_id),
                "display_id": int(track.display_id),
                "physical_vial_index": int(track.physical_vial_index),
                "assay_tube_index": int(track.assay_tube_index),
                "label": f"fly ({track.assay_tube_index},{track.display_id})",
            }
        )
        return hist

    def _retire(self, track: Track, *, frame_index: int, time_s: float) -> None:
        track.active = False
        track.retired_frame_index = int(frame_index)
        track.retired_time_s = float(time_s)
        self.completed_tracks.append(track)

    def update(self, *, frame_index: int, time_s: float, detections: Sequence[Detection], dt: float) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        detections_by_vial: Dict[int, List[Detection]] = {}
        for det in detections:
            detections_by_vial.setdefault(int(det.physical_vial_index), []).append(det)

        for vial in self.calibration.enabled_vials:
            vial_index = int(vial.physical_index)
            vial_tracks = list(self.active_tracks.get(vial_index, []))
            vial_detections = list(detections_by_vial.get(vial_index, []))
            if vial_tracks and vial_detections:
                cost = self._cost_matrix(vial_tracks, vial_detections)
                matches = _greedy_match_pairs(cost, self.max_cost)
            else:
                matches = []

            matched_track_ids = {ti for ti, _ in matches}
            matched_det_ids = {di for _, di in matches}
            survivors: List[Track] = []

            for ti, track in enumerate(vial_tracks):
                if ti in matched_track_ids:
                    di = next(di for ti2, di in matches if ti2 == ti)
                    track.add_observation(vial_detections[di], dt=max(1e-6, float(dt)))
                    survivors.append(track)
                    rows.append(self._track_to_row(track))
                else:
                    track.missed_frames += 1
                    if track.missed_frames > self.max_gap_frames:
                        self._retire(track, frame_index=frame_index, time_s=time_s)
                    else:
                        survivors.append(track)

            available_slots = max(0, int(self.max_flies_per_vial) - len([t for t in survivors if t.missed_frames == 0]))
            for di, det in enumerate(vial_detections):
                if di in matched_det_ids or available_slots <= 0:
                    continue
                display_id = int(self.next_display_id[vial_index])
                self.next_display_id[vial_index] += 1
                track = Track(
                    internal_id=int(self.next_internal_id),
                    display_id=display_id,
                    physical_vial_index=vial_index,
                    assay_tube_index=int(det.assay_tube_index),
                    last_x=float(det.center_xy_px[0]),
                    last_y=float(det.center_xy_px[1]),
                )
                self.next_internal_id += 1
                track.add_observation(det, dt=max(1e-6, float(dt)))
                survivors.append(track)
                rows.append(self._track_to_row(track))
                available_slots -= 1

            self.active_tracks[vial_index] = survivors

        rows.sort(key=lambda row: (int(row["assay_tube_index"]), int(row.get("display_id", 0))))
        return rows

    def finish(self) -> None:
        for vial_index in list(self.active_tracks.keys()):
            for track in self.active_tracks[vial_index]:
                self._retire(track, frame_index=track.history[-1]["frame_index"] if track.history else -1, time_s=track.history[-1]["time_s"] if track.history else 0.0)
            self.active_tracks[vial_index] = []

    def all_tracks(self) -> List[Track]:
        return list(self.completed_tracks)


def _ensure_positive_fps(value: float, fallback: float) -> float:
    value = float(value)
    if value > 0:
        return value
    fallback = float(fallback)
    return fallback if fallback > 0 else 5.0


def _rolling_mean(series: pd.Series, window: int) -> pd.Series:
    window = max(1, int(window))
    if window <= 1:
        return series.astype(float)
    return series.astype(float).rolling(window=window, min_periods=1, center=True).mean()


def _deduplicated_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=bool)
    if "deduplicated" in df.columns:
        return df["deduplicated"].fillna(False).astype(bool)
    return pd.Series(False, index=df.index, dtype=bool)


def _compute_track_frame_metrics(track_frames_df: pd.DataFrame, smoothing_window: int) -> pd.DataFrame:
    if track_frames_df.empty:
        return track_frames_df.copy()

    out_groups: List[pd.DataFrame] = []
    for _, group in track_frames_df.groupby("internal_track_id", sort=True):
        g = group.sort_values("frame_index").copy()
        g["dt_s"] = g["time_s"].diff().replace(0, np.nan)
        g["dx_px"] = g["x_px"].diff()
        g["dy_px"] = g["y_px"].diff()
        g["displacement_px"] = np.sqrt(g["dx_px"].pow(2) + g["dy_px"].pow(2))
        g["speed_px_s_raw"] = g["displacement_px"] / g["dt_s"]
        g["vertical_velocity_px_s_raw"] = g["distance_from_base_px"].diff() / g["dt_s"]
        g["height_px_smoothed"] = _rolling_mean(g["distance_from_base_px"].interpolate(limit_direction="both"), smoothing_window)
        g["speed_px_s_smoothed"] = _rolling_mean(g["speed_px_s_raw"].fillna(0.0), smoothing_window)
        g["vertical_velocity_px_s_smoothed"] = _rolling_mean(g["vertical_velocity_px_s_raw"].fillna(0.0), smoothing_window)

        if "distance_from_base_mm" in g.columns and g["distance_from_base_mm"].notna().any():
            interp_mm = g["distance_from_base_mm"].interpolate(limit_direction="both")
            g["height_mm_smoothed"] = _rolling_mean(interp_mm, smoothing_window)
            g["vertical_velocity_mm_s_raw"] = interp_mm.diff() / g["dt_s"]
            g["vertical_velocity_mm_s_smoothed"] = _rolling_mean(g["vertical_velocity_mm_s_raw"].fillna(0.0), smoothing_window)
            if "x_from_left_mm" in g.columns and g["x_from_left_mm"].notna().any():
                dx_mm = g["x_from_left_mm"].interpolate(limit_direction="both").diff()
                dy_mm = interp_mm.diff()
                g["displacement_mm"] = np.sqrt(dx_mm.pow(2) + dy_mm.pow(2))
                g["speed_mm_s_raw"] = g["displacement_mm"] / g["dt_s"]
                g["speed_mm_s_smoothed"] = _rolling_mean(g["speed_mm_s_raw"].fillna(0.0), smoothing_window)
            else:
                g["displacement_mm"] = np.nan
                g["speed_mm_s_raw"] = np.nan
                g["speed_mm_s_smoothed"] = np.nan
        else:
            g["height_mm_smoothed"] = np.nan
            g["vertical_velocity_mm_s_raw"] = np.nan
            g["vertical_velocity_mm_s_smoothed"] = np.nan
            g["displacement_mm"] = np.nan
            g["speed_mm_s_raw"] = np.nan
            g["speed_mm_s_smoothed"] = np.nan
        out_groups.append(g)

    return pd.concat(out_groups, ignore_index=True)


@dataclass
class CrossingCandidate:
    internal_track_id: int
    assay_tube_index: int
    physical_vial_index: int
    display_id: int
    start_time_s: float
    end_time_s: float
    crossing_frame_index: int
    crossing_time_s: float
    crossing_x_px: float
    crossing_y_px: float
    threshold_distance_px: float
    threshold_distance_mm: Optional[float]


@dataclass
class AcceptedCrossing:
    unique_event_id: int
    candidate: CrossingCandidate


def _threshold_mm(vial) -> Optional[float]:
    if getattr(vial, "tube_height_mm", None) is None:
        return None
    return float(vial.threshold_distance_px) * float(vial.tube_height_mm) / max(1.0, float(vial.height_px))


def _build_crossing_candidates(
    track_frames_df: pd.DataFrame,
    calibration: AssayCalibration,
    hysteresis_px: float,
) -> Tuple[List[CrossingCandidate], Dict[int, Dict[str, Any]]]:
    candidates: List[CrossingCandidate] = []
    details_by_track: Dict[int, Dict[str, Any]] = {}
    vial_by_physical = {int(v.physical_index): v for v in calibration.vials}

    if track_frames_df.empty:
        return candidates, details_by_track

    for internal_track_id, group in track_frames_df.groupby("internal_track_id", sort=True):
        g = group.sort_values("frame_index").copy()
        physical_vial_index = int(g["physical_vial_index"].iloc[0])
        assay_tube_index = int(g["assay_tube_index"].iloc[0])
        display_id = int(g["display_id"].iloc[0])
        vial = vial_by_physical.get(physical_vial_index)
        if vial is None:
            continue
        threshold_px = float(vial.threshold_distance_px)
        threshold_mm = _threshold_mm(vial)
        heights_px = g["height_px_smoothed"].fillna(g["distance_from_base_px"].interpolate(limit_direction="both")).to_numpy(dtype=float)

        seen_below = False
        crossing_idx: Optional[int] = None
        for idx, height_px in enumerate(heights_px):
            if not math.isfinite(float(height_px)):
                continue
            if float(height_px) <= threshold_px - float(hysteresis_px):
                seen_below = True
                continue
            if seen_below and float(height_px) >= threshold_px + float(hysteresis_px):
                crossing_idx = idx
                break

        detail: Dict[str, Any] = {
            "internal_track_id": int(internal_track_id),
            "assay_tube_index": assay_tube_index,
            "physical_vial_index": physical_vial_index,
            "display_id": display_id,
            "threshold_distance_px": threshold_px,
            "threshold_distance_mm": threshold_mm,
            "crossing_detected": False,
            "first_crossing_frame_index": None,
            "first_crossing_time_s": None,
            "crossing_x_px": None,
            "crossing_y_px": None,
        }
        if crossing_idx is not None:
            row = g.iloc[int(crossing_idx)]
            candidate = CrossingCandidate(
                internal_track_id=int(internal_track_id),
                assay_tube_index=assay_tube_index,
                physical_vial_index=physical_vial_index,
                display_id=display_id,
                start_time_s=float(g["time_s"].iloc[0]),
                end_time_s=float(g["time_s"].iloc[-1]),
                crossing_frame_index=int(row["frame_index"]),
                crossing_time_s=float(row["time_s"]),
                crossing_x_px=float(row["x_px"]),
                crossing_y_px=float(row["y_px"]),
                threshold_distance_px=threshold_px,
                threshold_distance_mm=threshold_mm,
            )
            candidates.append(candidate)
            detail.update(
                {
                    "crossing_detected": True,
                    "first_crossing_frame_index": int(candidate.crossing_frame_index),
                    "first_crossing_time_s": float(candidate.crossing_time_s),
                    "crossing_x_px": float(candidate.crossing_x_px),
                    "crossing_y_px": float(candidate.crossing_y_px),
                }
            )
        details_by_track[int(internal_track_id)] = detail
    return candidates, details_by_track


def _deduplicate_crossings(candidates: Sequence[CrossingCandidate]) -> Tuple[pd.DataFrame, Dict[int, int]]:
    accepted_rows: List[Dict[str, Any]] = []
    track_to_unique: Dict[int, int] = {}

    next_event_id = 1
    for assay_tube_index in sorted({int(c.assay_tube_index) for c in candidates}):
        tube_candidates = sorted(
            [c for c in candidates if int(c.assay_tube_index) == assay_tube_index],
            key=lambda c: (float(c.crossing_time_s), float(c.crossing_x_px), int(c.internal_track_id)),
        )
        accepted: List[AcceptedCrossing] = []
        for candidate in tube_candidates:
            matched_event_id: Optional[int] = None
            for existing in reversed(accepted[-4:]):
                dt = abs(float(candidate.crossing_time_s) - float(existing.candidate.crossing_time_s))
                dx = abs(float(candidate.crossing_x_px) - float(existing.candidate.crossing_x_px))
                adjacent_tracks = float(candidate.start_time_s) <= float(existing.candidate.end_time_s) + 0.75
                if dt <= 0.35 and dx <= 18.0 and adjacent_tracks:
                    matched_event_id = int(existing.unique_event_id)
                    break
            if matched_event_id is None:
                matched_event_id = next_event_id
                next_event_id += 1
                accepted.append(AcceptedCrossing(unique_event_id=matched_event_id, candidate=candidate))
                accepted_rows.append(
                    {
                        "unique_event_id": int(matched_event_id),
                        "assay_tube_index": int(candidate.assay_tube_index),
                        "physical_vial_index": int(candidate.physical_vial_index),
                        "display_id": int(candidate.display_id),
                        "internal_track_id": int(candidate.internal_track_id),
                        "crossing_frame_index": int(candidate.crossing_frame_index),
                        "crossing_time_s": float(candidate.crossing_time_s),
                        "crossing_x_px": float(candidate.crossing_x_px),
                        "crossing_y_px": float(candidate.crossing_y_px),
                        "threshold_distance_px": float(candidate.threshold_distance_px),
                        "threshold_distance_mm": None if candidate.threshold_distance_mm is None else float(candidate.threshold_distance_mm),
                        "deduplicated": False,
                    }
                )
            else:
                accepted_rows.append(
                    {
                        "unique_event_id": int(matched_event_id),
                        "assay_tube_index": int(candidate.assay_tube_index),
                        "physical_vial_index": int(candidate.physical_vial_index),
                        "display_id": int(candidate.display_id),
                        "internal_track_id": int(candidate.internal_track_id),
                        "crossing_frame_index": int(candidate.crossing_frame_index),
                        "crossing_time_s": float(candidate.crossing_time_s),
                        "crossing_x_px": float(candidate.crossing_x_px),
                        "crossing_y_px": float(candidate.crossing_y_px),
                        "threshold_distance_px": float(candidate.threshold_distance_px),
                        "threshold_distance_mm": None if candidate.threshold_distance_mm is None else float(candidate.threshold_distance_mm),
                        "deduplicated": True,
                    }
                )
            track_to_unique[int(candidate.internal_track_id)] = int(matched_event_id)

    crossings_df = pd.DataFrame(accepted_rows, columns=THRESHOLD_CROSSING_COLUMNS)
    if not crossings_df.empty:
        crossings_df = crossings_df.sort_values(["assay_tube_index", "crossing_time_s", "internal_track_id"]).reset_index(drop=True)
    return crossings_df, track_to_unique


def _safe_jsonable(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def _build_track_summaries(
    track_frames_df: pd.DataFrame,
    calibration: AssayCalibration,
    crossing_details: Dict[int, Dict[str, Any]],
    unique_event_by_track: Dict[int, int],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if track_frames_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    vial_by_physical = {int(v.physical_index): v for v in calibration.vials}
    per_track_rows: List[Dict[str, Any]] = []
    timeseries_rows: List[Dict[str, Any]] = []

    for internal_track_id, group in track_frames_df.groupby("internal_track_id", sort=True):
        g = group.sort_values("frame_index").copy()
        physical_vial_index = int(g["physical_vial_index"].iloc[0])
        assay_tube_index = int(g["assay_tube_index"].iloc[0])
        display_id = int(g["display_id"].iloc[0])
        vial = vial_by_physical.get(physical_vial_index)
        crossing = crossing_details.get(int(internal_track_id), {})
        timeseries_rows.extend(
            {
                "internal_track_id": int(internal_track_id),
                "assay_tube_index": assay_tube_index,
                "physical_vial_index": physical_vial_index,
                "display_id": display_id,
                "frame_index": int(row["frame_index"]),
                "time_s": float(row["time_s"]),
                "height_px": None if pd.isna(row.get("distance_from_base_px")) else float(row.get("distance_from_base_px")),
                "height_px_smoothed": None if pd.isna(row.get("height_px_smoothed")) else float(row.get("height_px_smoothed")),
                "vertical_velocity_px_s_raw": None if pd.isna(row.get("vertical_velocity_px_s_raw")) else float(row.get("vertical_velocity_px_s_raw")),
                "vertical_velocity_px_s_smoothed": None if pd.isna(row.get("vertical_velocity_px_s_smoothed")) else float(row.get("vertical_velocity_px_s_smoothed")),
                "speed_px_s_raw": None if pd.isna(row.get("speed_px_s_raw")) else float(row.get("speed_px_s_raw")),
                "speed_px_s_smoothed": None if pd.isna(row.get("speed_px_s_smoothed")) else float(row.get("speed_px_s_smoothed")),
                "height_mm": None if pd.isna(row.get("distance_from_base_mm")) else float(row.get("distance_from_base_mm")),
                "height_mm_smoothed": None if pd.isna(row.get("height_mm_smoothed")) else float(row.get("height_mm_smoothed")),
                "vertical_velocity_mm_s_raw": None if pd.isna(row.get("vertical_velocity_mm_s_raw")) else float(row.get("vertical_velocity_mm_s_raw")),
                "vertical_velocity_mm_s_smoothed": None if pd.isna(row.get("vertical_velocity_mm_s_smoothed")) else float(row.get("vertical_velocity_mm_s_smoothed")),
                "speed_mm_s_raw": None if pd.isna(row.get("speed_mm_s_raw")) else float(row.get("speed_mm_s_raw")),
                "speed_mm_s_smoothed": None if pd.isna(row.get("speed_mm_s_smoothed")) else float(row.get("speed_mm_s_smoothed")),
            }
            for _, row in g.iterrows()
        )

        position_history = [
            {
                "frame_index": int(row["frame_index"]),
                "time_s": float(row["time_s"]),
                "x_px": float(row["x_px"]),
                "y_px": float(row["y_px"]),
                "height_px": None if pd.isna(row.get("distance_from_base_px")) else float(row.get("distance_from_base_px")),
                "height_mm": None if pd.isna(row.get("distance_from_base_mm")) else float(row.get("distance_from_base_mm")),
                "detected": bool(row.get("detected", False)),
            }
            for _, row in g.iterrows()
        ]
        velocity_history = [
            {
                "frame_index": int(row["frame_index"]),
                "time_s": float(row["time_s"]),
                "vertical_velocity_px_s_raw": None if pd.isna(row.get("vertical_velocity_px_s_raw")) else float(row.get("vertical_velocity_px_s_raw")),
                "vertical_velocity_px_s_smoothed": None if pd.isna(row.get("vertical_velocity_px_s_smoothed")) else float(row.get("vertical_velocity_px_s_smoothed")),
                "speed_px_s_raw": None if pd.isna(row.get("speed_px_s_raw")) else float(row.get("speed_px_s_raw")),
                "speed_px_s_smoothed": None if pd.isna(row.get("speed_px_s_smoothed")) else float(row.get("speed_px_s_smoothed")),
                "vertical_velocity_mm_s_raw": None if pd.isna(row.get("vertical_velocity_mm_s_raw")) else float(row.get("vertical_velocity_mm_s_raw")),
                "vertical_velocity_mm_s_smoothed": None if pd.isna(row.get("vertical_velocity_mm_s_smoothed")) else float(row.get("vertical_velocity_mm_s_smoothed")),
                "speed_mm_s_raw": None if pd.isna(row.get("speed_mm_s_raw")) else float(row.get("speed_mm_s_raw")),
                "speed_mm_s_smoothed": None if pd.isna(row.get("speed_mm_s_smoothed")) else float(row.get("speed_mm_s_smoothed")),
            }
            for _, row in g.iterrows()
        ]

        row: Dict[str, Any] = {
            "internal_track_id": int(internal_track_id),
            "display_id": display_id,
            "assay_tube_index": assay_tube_index,
            "physical_vial_index": physical_vial_index,
            "label": f"fly ({assay_tube_index},{display_id})",
            "start_frame_index": int(g["frame_index"].iloc[0]),
            "end_frame_index": int(g["frame_index"].iloc[-1]),
            "start_time_s": float(g["time_s"].iloc[0]),
            "end_time_s": float(g["time_s"].iloc[-1]),
            "duration_s": float(g["time_s"].iloc[-1] - g["time_s"].iloc[0]),
            "n_samples": int(len(g)),
            "n_detected_samples": int(g["detected"].fillna(False).sum()),
            "crossing_detected": bool(crossing.get("crossing_detected", False)),
            "unique_crossing_detected": bool(int(internal_track_id) in unique_event_by_track),
            "unique_crossing_event_id": unique_event_by_track.get(int(internal_track_id)),
            "first_crossing_frame_index": crossing.get("first_crossing_frame_index"),
            "first_crossing_time_s": crossing.get("first_crossing_time_s"),
            "threshold_distance_px": crossing.get("threshold_distance_px"),
            "max_height_px": float(g["distance_from_base_px"].max(skipna=True)),
            "mean_height_px": float(g["distance_from_base_px"].mean(skipna=True)),
            "max_vertical_velocity_px_s_raw": float(g["vertical_velocity_px_s_raw"].max(skipna=True)),
            "mean_vertical_velocity_px_s_raw": float(g["vertical_velocity_px_s_raw"].mean(skipna=True)),
            "max_vertical_velocity_px_s_smoothed": float(g["vertical_velocity_px_s_smoothed"].max(skipna=True)),
            "mean_vertical_velocity_px_s_smoothed": float(g["vertical_velocity_px_s_smoothed"].mean(skipna=True)),
            "max_speed_px_s_raw": float(g["speed_px_s_raw"].max(skipna=True)),
            "mean_speed_px_s_raw": float(g["speed_px_s_raw"].mean(skipna=True)),
            "max_speed_px_s_smoothed": float(g["speed_px_s_smoothed"].max(skipna=True)),
            "mean_speed_px_s_smoothed": float(g["speed_px_s_smoothed"].mean(skipna=True)),
            "position_history_json": json.dumps(position_history),
            "velocity_samples_json": json.dumps(velocity_history),
        }
        if "distance_from_base_mm" in g.columns and g["distance_from_base_mm"].notna().any():
            row.update(
                {
                    "threshold_distance_mm": crossing.get("threshold_distance_mm"),
                    "max_height_mm": float(g["distance_from_base_mm"].max(skipna=True)),
                    "mean_height_mm": float(g["distance_from_base_mm"].mean(skipna=True)),
                    "max_vertical_velocity_mm_s_raw": float(g["vertical_velocity_mm_s_raw"].max(skipna=True)),
                    "mean_vertical_velocity_mm_s_raw": float(g["vertical_velocity_mm_s_raw"].mean(skipna=True)),
                    "max_vertical_velocity_mm_s_smoothed": float(g["vertical_velocity_mm_s_smoothed"].max(skipna=True)),
                    "mean_vertical_velocity_mm_s_smoothed": float(g["vertical_velocity_mm_s_smoothed"].mean(skipna=True)),
                    "max_speed_mm_s_raw": float(g["speed_mm_s_raw"].max(skipna=True)),
                    "mean_speed_mm_s_raw": float(g["speed_mm_s_raw"].mean(skipna=True)),
                    "max_speed_mm_s_smoothed": float(g["speed_mm_s_smoothed"].max(skipna=True)),
                    "mean_speed_mm_s_smoothed": float(g["speed_mm_s_smoothed"].mean(skipna=True)),
                }
            )
        per_track_rows.append(row)

    track_summary_df = pd.DataFrame(per_track_rows).sort_values(["assay_tube_index", "display_id"]).reset_index(drop=True)
    per_fly_summary_df = track_summary_df.copy()
    velocity_timeseries_df = pd.DataFrame(timeseries_rows)
    return track_summary_df, per_fly_summary_df, velocity_timeseries_df


def _build_vial_summaries(
    track_frames_df: pd.DataFrame,
    track_summary_df: pd.DataFrame,
    crossings_df: pd.DataFrame,
    processed_duration_s: float,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if track_frames_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    vial_rows: List[Dict[str, Any]] = []
    timeseries_rows: List[Dict[str, Any]] = []

    for assay_tube_index, group in track_frames_df.groupby("assay_tube_index", sort=True):
        g = group.sort_values(["time_s", "internal_track_id"]).copy()
        summary_grp = track_summary_df[track_summary_df["assay_tube_index"] == assay_tube_index].copy()
        unique_crossings = crossings_df[
            (crossings_df["assay_tube_index"] == assay_tube_index) & (~crossings_df["deduplicated"].fillna(False))
        ].copy()
        per_time = (
            g.groupby("time_s", as_index=False)
            .agg(
                mean_height_px=("distance_from_base_px", "mean"),
                max_height_px=("distance_from_base_px", "max"),
                mean_vertical_velocity_px_s=("vertical_velocity_px_s_smoothed", "mean"),
                max_vertical_velocity_px_s=("vertical_velocity_px_s_smoothed", "max"),
                mean_speed_px_s=("speed_px_s_smoothed", "mean"),
                max_speed_px_s=("speed_px_s_smoothed", "max"),
            )
        )
        if g["distance_from_base_mm"].notna().any():
            extra = (
                g.groupby("time_s", as_index=False)
                .agg(
                    mean_height_mm=("distance_from_base_mm", "mean"),
                    max_height_mm=("distance_from_base_mm", "max"),
                    mean_vertical_velocity_mm_s=("vertical_velocity_mm_s_smoothed", "mean"),
                    max_vertical_velocity_mm_s=("vertical_velocity_mm_s_smoothed", "max"),
                    mean_speed_mm_s=("speed_mm_s_smoothed", "mean"),
                    max_speed_mm_s=("speed_mm_s_smoothed", "max"),
                )
            )
            per_time = per_time.merge(extra, on="time_s", how="left")

        timeseries_rows.extend(
            {"assay_tube_index": int(assay_tube_index), **{k: _safe_jsonable(v) for k, v in row.items()}}
            for row in per_time.to_dict(orient="records")
        )

        flies_detected = int(summary_grp["internal_track_id"].nunique())
        unique_crossing_count = int(unique_crossings["unique_event_id"].nunique())
        summary_row: Dict[str, Any] = {
            "assay_tube_index": int(assay_tube_index),
            "number_of_flies_detected": flies_detected,
            "flies_detected": flies_detected,
            "number_of_unique_threshold_crossings": unique_crossing_count,
            "unique_threshold_crossings": unique_crossing_count,
            "fraction_crossing_by_10s": float(unique_crossing_count) / max(1, flies_detected),
            "first_crossing_time_s": None if unique_crossings.empty else float(unique_crossings["crossing_time_s"].min()),
            "mean_height_px": float(g["distance_from_base_px"].mean(skipna=True)),
            "max_height_px": float(g["distance_from_base_px"].max(skipna=True)),
            "mean_vertical_velocity_px_s": float(g["vertical_velocity_px_s_smoothed"].mean(skipna=True)),
            "max_vertical_velocity_px_s": float(g["vertical_velocity_px_s_smoothed"].max(skipna=True)),
            "mean_speed_px_s": float(g["speed_px_s_smoothed"].mean(skipna=True)),
            "max_speed_px_s": float(g["speed_px_s_smoothed"].max(skipna=True)),
            "velocity_over_time_json": json.dumps(per_time.to_dict(orient="records")),
            "processed_duration_s": float(processed_duration_s),
        }
        if g["distance_from_base_mm"].notna().any():
            summary_row.update(
                {
                    "mean_height_mm": float(g["distance_from_base_mm"].mean(skipna=True)),
                    "max_height_mm": float(g["distance_from_base_mm"].max(skipna=True)),
                    "mean_vertical_velocity_mm_s": float(g["vertical_velocity_mm_s_smoothed"].mean(skipna=True)),
                    "max_vertical_velocity_mm_s": float(g["vertical_velocity_mm_s_smoothed"].max(skipna=True)),
                    "mean_speed_mm_s": float(g["speed_mm_s_smoothed"].mean(skipna=True)),
                    "max_speed_mm_s": float(g["speed_mm_s_smoothed"].max(skipna=True)),
                }
            )
        vial_rows.append(summary_row)

    per_vial_summary_df = pd.DataFrame(vial_rows).sort_values("assay_tube_index").reset_index(drop=True)
    vial_velocity_timeseries_df = pd.DataFrame(timeseries_rows)
    return per_vial_summary_df, vial_velocity_timeseries_df


def _build_frame_level_df(
    frame_rows: Sequence[Dict[str, Any]],
    calibration: AssayCalibration,
    crossings_df: pd.DataFrame,
) -> pd.DataFrame:
    if not frame_rows:
        return pd.DataFrame()
    df = pd.DataFrame(frame_rows).sort_values("frame_index").reset_index(drop=True)
    unique_crossings = crossings_df[~_deduplicated_mask(crossings_df)] if not crossings_df.empty else pd.DataFrame()
    if not unique_crossings.empty:
        df["unique_threshold_crossings_total"] = df["time_s"].apply(
            lambda t: int((unique_crossings["crossing_time_s"] <= float(t) + 1e-9).sum())
        )
    else:
        df["unique_threshold_crossings_total"] = 0
    enabled_vials = sorted(calibration.enabled_vials, key=lambda vial: int(vial.assay_index or vial.physical_index))
    for vial in enabled_vials:
        assay_tube_index = int(vial.assay_index or 0)
        df[f"tube_{assay_tube_index}_threshold_px"] = float(vial.threshold_distance_px)
    return df


def _sql_safe_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    safe = df.copy()
    for col in safe.columns:
        safe[col] = safe[col].map(
            lambda value: json.dumps(value) if isinstance(value, (list, tuple, dict)) else value
        )
    return safe


def _write_processing_outputs(
    context: ProcessingContext,
    *,
    frame_level_df: pd.DataFrame,
    detections_df: pd.DataFrame,
    track_frames_df: pd.DataFrame,
    track_summary_df: pd.DataFrame,
    per_fly_summary_df: pd.DataFrame,
    per_vial_summary_df: pd.DataFrame,
    threshold_crossings_df: pd.DataFrame,
    vial_velocity_timeseries_df: pd.DataFrame,
    processing_meta: Dict[str, Any],
) -> Dict[str, str]:
    run_dir = context.run_dir
    processed_dir = context.processing_dir

    frame_level_csv = processed_dir / "frame_level.csv"
    detections_csv = processed_dir / "detections.csv"
    track_frames_csv = processed_dir / "track_frames.csv"
    track_level_csv = processed_dir / "track_level.csv"
    per_fly_summary_csv = processed_dir / "per_fly_summary.csv"
    per_vial_summary_csv = processed_dir / "per_vial_summary.csv"
    threshold_crossings_csv = processed_dir / "threshold_crossings.csv"
    vial_velocity_timeseries_csv = processed_dir / "vial_velocity_timeseries.csv"
    processing_json = processed_dir / "processing_session.json"
    sqlite_db = processed_dir / "results.sqlite"

    frame_level_df.to_csv(frame_level_csv, index=False)
    detections_df.to_csv(detections_csv, index=False)
    track_frames_df.to_csv(track_frames_csv, index=False)
    track_summary_df.to_csv(track_level_csv, index=False)
    per_fly_summary_df.to_csv(per_fly_summary_csv, index=False)
    per_vial_summary_df.to_csv(per_vial_summary_csv, index=False)
    threshold_crossings_df.to_csv(threshold_crossings_csv, index=False)
    vial_velocity_timeseries_df.to_csv(vial_velocity_timeseries_csv, index=False)
    save_json(processing_json, processing_meta)

    with sqlite3.connect(sqlite_db) as conn:
        _sql_safe_dataframe(frame_level_df).to_sql("frame_level", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(detections_df).to_sql("detections", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(track_frames_df).to_sql("track_frames", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(track_summary_df).to_sql("track_level", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(per_fly_summary_df).to_sql("per_fly_summary", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(per_vial_summary_df).to_sql("per_vial_summary", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(threshold_crossings_df).to_sql("threshold_crossings", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(vial_velocity_timeseries_df).to_sql("vial_velocity_timeseries", conn, if_exists="replace", index=False)
        _sql_safe_dataframe(pd.DataFrame([processing_meta])).to_sql("processing_session", conn, if_exists="replace", index=False)

    graphs = generate_graphs_and_pdf(
        output_dir=processed_dir,
        tracks_df=track_frames_df,
        fly_summary_df=per_fly_summary_df,
        vial_summary_df=per_vial_summary_df,
        session_meta=processing_meta,
    )

    return {
        "frame_level_csv": str(frame_level_csv.resolve()),
        "detections_csv": str(detections_csv.resolve()),
        "track_frames_csv": str(track_frames_csv.resolve()),
        "track_level_csv": str(track_level_csv.resolve()),
        "per_fly_summary_csv": str(per_fly_summary_csv.resolve()),
        "per_vial_summary_csv": str(per_vial_summary_csv.resolve()),
        "threshold_crossings_csv": str(threshold_crossings_csv.resolve()),
        "vial_velocity_timeseries_csv": str(vial_velocity_timeseries_csv.resolve()),
        "processing_json": str(processing_json.resolve()),
        "sqlite_db": str(sqlite_db.resolve()),
        **graphs,
    }


def process_assay_run(
    run_dir_or_video: str | Path,
    *,
    profile_override: Optional[AssayProfile] = None,
    logger: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    upload_after: Optional[bool] = None,
) -> Dict[str, Any]:
    if logger is None:
        logger = lambda _msg: None
    context = load_processing_context(run_dir_or_video, profile_override=profile_override)
    profile = context.profile
    logger(f"Processing run in {context.run_dir}")
    logger(f"Using transform: {describe_transform(context.transform)}")

    cap = cv2.VideoCapture(str(context.raw_video_path))
    if cap is None or not cap.isOpened():
        raise ProcessingError(f"Could not open raw assay video: {context.raw_video_path}")

    record_fps_manifest = float(context.run_manifest.get("record_fps", profile.assay_camera.fps))
    record_fps_cap = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    record_fps = _ensure_positive_fps(record_fps_manifest, record_fps_cap)
    analysis_fps = _ensure_positive_fps(profile.analysis.analysis_fps, record_fps)
    analysis_fps = min(analysis_fps, record_fps)
    sample_interval_s = 1.0 / max(1e-6, float(analysis_fps))
    average_count = max(1, int(profile.analysis.frame_average_count))
    tracking_mode = str(getattr(profile.analysis, "tracking_mode", "stitch") or "stitch").strip().lower()
    if tracking_mode not in {"stitch", "full"}:
        tracking_mode = "stitch"
    use_mask_blobs_as_flies = bool(getattr(profile.analysis, "use_mask_blobs_as_flies", True))

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    full_tracker = None
    stitch_tracker = None
    if tracking_mode == "full":
        full_tracker = MultiVialTracker(
            calibration=context.calibration,
            memory_frames=max(2, int(round(record_fps / max(1.0, analysis_fps))) + 1),
            max_flies_per_vial=int(profile.detector.max_flies_per_vial),
        )
    else:
        stitch_tracker = SnapshotStitchTracker(
            calibration=context.calibration,
            max_flies_per_vial=int(profile.detector.max_flies_per_vial),
            max_gap_frames=1,
        )

    transformed_buffer: Deque[np.ndarray] = deque(maxlen=max(1, average_count))
    all_detections: List[Detection] = []
    frame_rows: List[Dict[str, Any]] = []
    snapshot_rows: List[Dict[str, Any]] = []
    annotated_writer = None
    annotated_video_path: Optional[Path] = None
    mask_writer = None
    mask_video_path: Optional[Path] = None
    snapshots_dir = ensure_dir(context.processing_dir / "snapshots")
    next_snapshot_t = 0.0
    next_sample_t = 0.0
    source_frame_index = 0
    processed_frame_index = 0
    last_processed_time_s: Optional[float] = None
    started_at = time.monotonic()

    try:
        while True:
            ok, raw_frame_bgr = cap.read()
            if not ok or raw_frame_bgr is None:
                break
            source_time_s = float(source_frame_index) / max(1e-6, float(record_fps))
            transformed_frame = apply_image_transform(raw_frame_bgr, context.transform)
            transformed_buffer.append(transformed_frame)

            if source_time_s + 1e-9 < next_sample_t:
                source_frame_index += 1
                continue

            analysis_frame_bgr = _average_frames(list(transformed_buffer)[-average_count:])
            detections, mask, aligned_bgr = detect_assay_frame(
                background_bgr=context.background_bgr,
                frame_bgr=analysis_frame_bgr,
                calibration=context.calibration,
                frame_index=processed_frame_index,
                time_s=source_time_s,
                min_area=int(profile.detector.min_area),
                max_area=int(profile.detector.max_area),
                min_threshold=float(profile.detector.min_threshold),
                inner_margin_px=int(profile.detector.inner_margin_px),
                no_align=not bool(profile.analysis.alignment_enabled),
            )
            dt = sample_interval_s if last_processed_time_s is None else max(1e-6, float(source_time_s - last_processed_time_s))
            if full_tracker is not None:
                full_tracker.update(
                    frame_index=processed_frame_index,
                    time_s=source_time_s,
                    detections=detections,
                    dt=dt,
                )
                current_rows = [row for row in full_tracker.active_rows() if bool(row.get("detected"))]
                track_overlay = None if use_mask_blobs_as_flies else full_tracker
            else:
                assert stitch_tracker is not None
                current_rows = stitch_tracker.update(
                    frame_index=processed_frame_index,
                    time_s=source_time_s,
                    detections=detections,
                    dt=dt,
                )
                track_overlay = None
            last_processed_time_s = source_time_s
            all_detections.extend(detections)
            frame_row: Dict[str, Any] = {
                "frame_index": int(processed_frame_index),
                "source_frame_index": int(source_frame_index),
                "time_s": float(source_time_s),
                "detection_count": int(len(detections)),
                "active_track_count": int(len(current_rows)),
            }
            for vial in context.calibration.enabled_vials:
                tube_index = int(vial.assay_index or 0)
                frame_row[f"tube_{tube_index}_detections"] = int(sum(1 for det in detections if int(det.assay_tube_index) == tube_index))
                frame_row[f"tube_{tube_index}_active_tracks"] = int(sum(1 for row in current_rows if int(row["assay_tube_index"]) == tube_index))
            frame_rows.append(frame_row)

            mask_bgr = assay_mask_to_bgr(mask, frame_bgr=aligned_bgr)
            annotated_bgr = annotate_assay_frame(
                aligned_bgr,
                context.calibration,
                detections,
                track_overlay,
                frame_index=processed_frame_index,
                time_s=source_time_s,
                show_positions=bool(profile.analysis.show_positions),
                frame_rows=current_rows if (use_mask_blobs_as_flies or track_overlay is None) else None,
                max_flies_per_vial=int(profile.detector.max_flies_per_vial),
            )
            if annotated_writer is None:
                h, w = annotated_bgr.shape[:2]
                annotated_writer, annotated_video_path = open_video_writer_with_path(
                    context.processing_dir / "annotated_video.mp4",
                    fps=float(analysis_fps),
                    frame_size=(w, h),
                )
                if bool(profile.analysis.save_mask_video):
                    mh, mw = mask_bgr.shape[:2]
                    mask_writer, mask_video_path = open_video_writer_with_path(
                        context.processing_dir / "mask_video.mp4",
                        fps=float(analysis_fps),
                        frame_size=(mw, mh),
                    )
            assert annotated_writer is not None
            annotated_writer.write(annotated_bgr)
            if mask_writer is not None:
                mask_writer.write(mask_bgr)

            if bool(profile.outputs.save_preview_snapshots) and source_time_s + 1e-9 >= next_snapshot_t:
                snapshot_info = _save_snapshot_bundle(
                    snapshots_dir,
                    processed_frame_index,
                    source_time_s,
                    raw_bgr=raw_frame_bgr,
                    transformed_bgr=analysis_frame_bgr,
                    aligned_bgr=aligned_bgr,
                    annotated_bgr=annotated_bgr,
                    mask_bgr=mask_bgr,
                )
                snapshot_rows.append(
                    {
                        "frame_index": int(processed_frame_index),
                        "source_frame_index": int(source_frame_index),
                        "time_s": float(source_time_s),
                        **snapshot_info,
                    }
                )
                next_snapshot_t += max(0.05, float(profile.outputs.snapshot_interval_s or 1.0))

            if progress_callback is not None:
                progress_callback(
                    {
                        "frame_index": int(processed_frame_index),
                        "source_frame_index": int(source_frame_index),
                        "time_s": float(source_time_s),
                        "preview_bgr": annotated_bgr,
                        "mask_bgr": mask_bgr,
                        "run_dir": str(context.run_dir),
                        "progress_fraction": None if total_frames <= 0 else float(source_frame_index + 1) / float(total_frames),
                    }
                )

            processed_frame_index += 1
            next_sample_t += sample_interval_s
            source_frame_index += 1
    finally:
        cap.release()
        if annotated_writer is not None:
            annotated_writer.release()
        if mask_writer is not None:
            mask_writer.release()

    if full_tracker is not None:
        full_tracker.finish()
        track_frames_df = tracks_to_dataframe(full_tracker.completed_tracks)
    else:
        assert stitch_tracker is not None
        stitch_tracker.finish()
        track_frames_df = tracks_to_dataframe(stitch_tracker.all_tracks())
    track_frames_df = _compute_track_frame_metrics(track_frames_df, smoothing_window=int(profile.analysis.smoothing_window))
    detections_df = detections_to_dataframe(all_detections)
    crossing_candidates, crossing_details = _build_crossing_candidates(
        track_frames_df,
        context.calibration,
        hysteresis_px=float(profile.detector.threshold_hysteresis_px),
    )
    threshold_crossings_df, unique_event_by_track = _deduplicate_crossings(crossing_candidates)
    track_summary_df, per_fly_summary_df, velocity_timeseries_df = _build_track_summaries(
        track_frames_df,
        context.calibration,
        crossing_details,
        unique_event_by_track,
    )
    per_vial_summary_df, vial_velocity_timeseries_df = _build_vial_summaries(
        track_frames_df,
        track_summary_df,
        threshold_crossings_df,
        processed_duration_s=0.0 if track_frames_df.empty else float(track_frames_df["time_s"].max()),
    )
    frame_level_df = _build_frame_level_df(frame_rows, context.calibration, threshold_crossings_df)
    snapshot_index_df = pd.DataFrame(snapshot_rows)

    processed_duration_s = 0.0 if frame_level_df.empty else float(frame_level_df["time_s"].max())
    processing_meta: Dict[str, Any] = {
        "schema_version": 2,
        "processed_at": timestamp_iso(),
        "run_dir": str(context.run_dir.resolve()),
        "raw_video_path": str(context.raw_video_path.resolve()),
        "background_path": str(context.background_path.resolve()),
        "calibration_path": str((context.run_dir / 'calibration_snapshot.json').resolve() if (context.run_dir / 'calibration_snapshot.json').exists() else ''),
        "record_fps": float(record_fps),
        "analysis_fps": float(analysis_fps),
        "frame_average_count": int(average_count),
        "frame_subsampling": str(profile.analysis.frame_subsampling),
        "smoothing_window": int(profile.analysis.smoothing_window),
        "alignment_enabled": bool(profile.analysis.alignment_enabled),
        "tracking_mode": tracking_mode,
        "use_mask_blobs_as_flies": bool(use_mask_blobs_as_flies),
        "transform_description": describe_transform(context.transform),
        "frames_in_video": int(total_frames),
        "frames_processed": int(len(frame_level_df)),
        "processed_duration_s": float(processed_duration_s),
        "assay_duration_s": float(profile.assay_duration_s),
        "snapshots_saved": int(len(snapshot_rows)),
        "snapshot_interval_s": float(profile.outputs.snapshot_interval_s),
        "snapshot_dir": str(snapshots_dir.resolve()),
        "unique_threshold_crossings_total": int(threshold_crossings_df[~_deduplicated_mask(threshold_crossings_df)]["unique_event_id"].nunique()) if not threshold_crossings_df.empty else 0,
        "tracks_detected_total": int(track_summary_df["internal_track_id"].nunique()) if not track_summary_df.empty else 0,
        "profile_name": str(profile.name),
        "profile_snapshot_path": str((context.run_dir / 'profile_snapshot.json').resolve()) if (context.run_dir / 'profile_snapshot.json').exists() else '',
        "transform_snapshot_path": str((context.run_dir / 'transform_snapshot.json').resolve()) if (context.run_dir / 'transform_snapshot.json').exists() else '',
        "processing_dir": str(context.processing_dir.resolve()),
    }

    output_paths = _write_processing_outputs(
        context,
        frame_level_df=frame_level_df,
        detections_df=detections_df,
        track_frames_df=track_frames_df,
        track_summary_df=track_summary_df,
        per_fly_summary_df=per_fly_summary_df,
        per_vial_summary_df=per_vial_summary_df,
        threshold_crossings_df=threshold_crossings_df,
        vial_velocity_timeseries_df=vial_velocity_timeseries_df,
        processing_meta=processing_meta,
    )
    processing_meta.update(output_paths)
    if not snapshot_index_df.empty:
        snapshot_index_csv = context.processing_dir / "snapshot_index.csv"
        snapshot_index_df.to_csv(snapshot_index_csv, index=False)
        processing_meta["snapshot_index_csv"] = str(snapshot_index_csv.resolve())
    if annotated_video_path is not None:
        processing_meta["annotated_video_path"] = str(annotated_video_path.resolve())
    if mask_video_path is not None:
        processing_meta["mask_video_path"] = str(mask_video_path.resolve())

    save_json(context.processing_dir / "processing_session.json", processing_meta)
    latest_processing = {
        "processed_at": processing_meta["processed_at"],
        "processing_dir": processing_meta["processing_dir"],
        "processing_session_json": processing_meta["processing_json"],
        "annotated_video_path": processing_meta.get("annotated_video_path", ""),
        "mask_video_path": processing_meta.get("mask_video_path", ""),
        "tracking_mode": processing_meta.get("tracking_mode", tracking_mode),
    }
    save_json(context.run_dir / "processed" / "latest_processing.json", latest_processing)
    if context.run_manifest_path is not None:
        updated_manifest = dict(context.run_manifest)
        history = list(updated_manifest.get("processing_history", []))
        history.append(latest_processing)
        updated_manifest.update(
            {
                "processed_at": processing_meta["processed_at"],
                "processing_dir": processing_meta["processing_dir"],
                "processing_session_json": processing_meta["processing_json"],
                "annotated_video_path": processing_meta.get("annotated_video_path", ""),
                "mask_video_path": processing_meta.get("mask_video_path", ""),
                "processing_history": history,
            }
        )
        save_json(context.run_manifest_path, updated_manifest)

    upload_requested = should_auto_upload(profile.box_upload, "processing")
    if upload_after is not None:
        upload_requested = bool(upload_after)
    if upload_requested:
        try:
            upload_result = upload_run_artifacts(
                context.run_dir,
                profile.box_upload,
                artifact_mode=None,
                logger=logger,
            )
            processing_meta["box_upload"] = upload_result
            save_json(context.processing_dir / "box_upload_result.json", upload_result)
        except BoxUploadError as exc:
            processing_meta["box_upload_error"] = str(exc)
            save_json(context.processing_dir / "box_upload_error.json", {"error": str(exc)})
            logger(f"Box upload failed: {exc}")

    elapsed_wall_s = float(time.monotonic() - started_at)
    processing_meta["elapsed_wall_s"] = elapsed_wall_s
    save_json(context.processing_dir / "processing_session.json", processing_meta)
    return processing_meta


def process_last_assay(
    profile: AssayProfile,
    project_root: str | Path,
    *,
    logger: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    if logger is None:
        logger = lambda _msg: None
    candidates: List[Path] = []
    if profile.last_run_dir:
        last_run = _resolve_path(profile.last_run_dir, Path(project_root))
        if last_run.exists():
            candidates.append(last_run)
    output_root = _resolve_path(profile.outputs.output_root, Path(project_root))
    newest = newest_child_dir(output_root, prefix="assay_")
    if newest is not None and newest not in candidates:
        candidates.append(newest)
    if not candidates:
        raise ProcessingError(f"No assay runs were found under {output_root}")
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return process_assay_run(
        candidates[0],
        profile_override=profile,
        logger=logger,
        progress_callback=progress_callback,
    )


def batch_process_folder(
    folder: str | Path,
    *,
    profile_override: Optional[AssayProfile] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    if logger is None:
        logger = lambda _msg: None
    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.exists():
        raise ProcessingError(f"Batch folder does not exist: {folder_path}")
    runs = sorted([path for path in folder_path.iterdir() if path.is_dir() and path.name.startswith("assay_")])
    if not runs:
        raise ProcessingError(f"No assay_* run folders were found in {folder_path}")
    results: List[Dict[str, Any]] = []
    for run_dir in runs:
        logger(f"Batch processing {run_dir.name}")
        try:
            results.append(process_assay_run(run_dir, profile_override=profile_override, logger=logger))
        except Exception as exc:
            error_payload = {"run_dir": str(run_dir), "error": str(exc)}
            save_json(ensure_dir(run_dir / "processed") / "processing_error.json", error_payload)
            results.append(error_payload)
    return results


def manual_upload_run(
    run_dir: str | Path,
    settings: Any,
    *,
    artifact_mode: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    if logger is None:
        logger = lambda _msg: None
    result = upload_run_artifacts(run_dir, settings, artifact_mode=artifact_mode, logger=logger)
    save_json(ensure_dir(Path(run_dir) / "processed") / "box_upload_result.json", result)
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline fruit fly assay recording and processing workflow.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_process = sub.add_parser("process", help="Process a recorded assay run directory or raw video path.")
    p_process.add_argument("run", help="Run directory or raw video path")
    p_process.add_argument("--profile", default=None, help="Optional profile JSON override")
    p_process.add_argument("--upload", action="store_true", help="Upload artifacts to Box after processing")

    p_batch = sub.add_parser("batch-process", help="Process all assay_* runs in a folder.")
    p_batch.add_argument("folder", help="Folder containing assay_* run directories")
    p_batch.add_argument("--profile", default=None, help="Optional profile JSON override")

    p_upload = sub.add_parser("upload", help="Upload an existing assay run folder to Box.")
    p_upload.add_argument("run", help="Run directory")
    p_upload.add_argument("--profile", required=True, help="Profile JSON with Box settings")
    p_upload.add_argument("--mode", default=None, help="Artifact mode override: summaries, summaries+videos, or full")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    profile_override = AssayProfile.from_dict(load_json(args.profile)) if getattr(args, "profile", None) else None

    if args.command == "process":
        result = process_assay_run(
            args.run,
            profile_override=profile_override,
            logger=print,
            upload_after=bool(args.upload),
        )
        print(pd.Series(result).to_json(indent=2))
        return

    if args.command == "batch-process":
        results = batch_process_folder(args.folder, profile_override=profile_override, logger=print)
        print(json.dumps(results, indent=2))
        return

    if args.command == "upload":
        if profile_override is None:
            parser.error("--profile is required for upload")
        result = manual_upload_run(args.run, profile_override.box_upload, artifact_mode=args.mode, logger=print)
        print(json.dumps(result, indent=2))
        return

    parser.error("Unknown command")


if __name__ == "__main__":
    main()
