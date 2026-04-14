#!/usr/bin/env python3
"""
Persistent assay profile management.

Profiles capture reusable configuration for a physical assay setup so new team
members can load one profile and work through the staged workflow without
re-entering dozens of settings each session.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from motor_control import MotorSettings
from shared_utils import ensure_dir, load_json, save_json
from transform_utils import TransformSettings


PROFILE_SCHEMA_VERSION = 1


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(text).strip())
    slug = slug.strip("._-")
    return slug or "profile"


@dataclass
class CameraSettings:
    backend: str = "opencv"
    device: str = "auto:assay"
    width: int = 1536
    height: int = 864
    fps: float = 30.0
    camera_index: int = 0
    preferred_hint: str = ""
    description: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]], *, default_device: str) -> "CameraSettings":
        payload = dict(data or {})
        return cls(
            backend=str(payload.get("backend", payload.get("camera_backend", "opencv"))),
            device=str(payload.get("device", payload.get("camera_device", default_device))),
            width=int(payload.get("width", 1536)),
            height=int(payload.get("height", 864)),
            fps=float(payload.get("fps", 30.0)),
            camera_index=int(payload.get("camera_index", 0)),
            preferred_hint=str(payload.get("preferred_hint", "") or ""),
            description=str(payload.get("description", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DetectorSettings:
    min_area: int = 10
    max_area: int = 250
    min_threshold: float = 12.0
    inner_margin_px: int = 8
    max_flies_per_vial: int = 10
    threshold_hysteresis_px: float = 1.5
    blob_split_max_parts: int = 4
    allow_blob_split: bool = True
    motion_weight: float = 0.30

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "DetectorSettings":
        payload = dict(data or {})
        return cls(
            min_area=int(payload.get("min_area", 10)),
            max_area=int(payload.get("max_area", 250)),
            min_threshold=float(payload.get("min_threshold", 12.0)),
            inner_margin_px=int(payload.get("inner_margin_px", 8)),
            max_flies_per_vial=int(payload.get("max_flies_per_vial", 10)),
            threshold_hysteresis_px=float(payload.get("threshold_hysteresis_px", 1.5)),
            blob_split_max_parts=int(payload.get("blob_split_max_parts", 4)),
            allow_blob_split=bool(payload.get("allow_blob_split", True)),
            motion_weight=float(payload.get("motion_weight", 0.30)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisSettings:
    analysis_fps: float = 5.0
    frame_subsampling: str = "nearest"
    frame_average_count: int = 1
    smoothing_window: int = 3
    auto_process_after_recording: bool = False
    alignment_enabled: bool = False
    save_mask_video: bool = False
    show_positions: bool = False

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "AnalysisSettings":
        payload = dict(data or {})
        return cls(
            analysis_fps=float(payload.get("analysis_fps", 5.0)),
            frame_subsampling=str(payload.get("frame_subsampling", "nearest")),
            frame_average_count=int(payload.get("frame_average_count", 1)),
            smoothing_window=int(payload.get("smoothing_window", 3)),
            auto_process_after_recording=bool(payload.get("auto_process_after_recording", payload.get("processing_automatic", False))),
            alignment_enabled=bool(payload.get("alignment_enabled", False)),
            save_mask_video=bool(payload.get("save_mask_video", False)),
            show_positions=bool(payload.get("show_positions", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OutputSettings:
    output_root: str = "outputs/assay"
    background_root: str = "backgrounds"
    calibration_root: str = "calibrations"
    upload_artifacts: str = "summaries"
    save_preview_snapshots: bool = True
    snapshot_interval_s: float = 1.0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "OutputSettings":
        payload = dict(data or {})
        return cls(
            output_root=str(payload.get("output_root", "outputs/assay")),
            background_root=str(payload.get("background_root", "backgrounds")),
            calibration_root=str(payload.get("calibration_root", "calibrations")),
            upload_artifacts=str(payload.get("upload_artifacts", payload.get("box_upload_mode", "summaries"))),
            save_preview_snapshots=bool(payload.get("save_preview_snapshots", True)),
            snapshot_interval_s=float(payload.get("snapshot_interval_s", 1.0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BoxUploadSettings:
    enabled: bool = False
    parent_folder_id: str = ""
    tokens_file: str = ""
    config_file: str = ""
    upload_after_processing: bool = False
    upload_after_recording: bool = False
    upload_backgrounds: bool = False
    artifact_mode: str = "summaries"
    folder_prefix: str = "fly_assay"

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "BoxUploadSettings":
        payload = dict(data or {})
        return cls(
            enabled=bool(payload.get("enabled", False)),
            parent_folder_id=str(payload.get("parent_folder_id", "") or ""),
            tokens_file=str(payload.get("tokens_file", "") or ""),
            config_file=str(payload.get("config_file", "") or ""),
            upload_after_processing=bool(payload.get("upload_after_processing", False)),
            upload_after_recording=bool(payload.get("upload_after_recording", False)),
            upload_backgrounds=bool(payload.get("upload_backgrounds", False)),
            artifact_mode=str(payload.get("artifact_mode", payload.get("upload_mode", "summaries"))),
            folder_prefix=str(payload.get("folder_prefix", "fly_assay") or "fly_assay"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AssayProfile:
    name: str
    schema_version: int = PROFILE_SCHEMA_VERSION
    description: str = ""
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    channel_camera: CameraSettings = field(default_factory=lambda: CameraSettings(device="auto:channel", width=1920, height=1080, fps=30.0, description="Brio channel camera"))
    assay_camera: CameraSettings = field(default_factory=lambda: CameraSettings(device="auto:assay", width=1536, height=864, fps=30.0, description="Assay camera"))
    assay_duration_s: float = 10.0
    record_preroll_s: float = 0.0
    threshold_default_fraction: float = 0.50
    transform: TransformSettings = field(default_factory=TransformSettings)
    detector: DetectorSettings = field(default_factory=DetectorSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    motor: MotorSettings = field(default_factory=MotorSettings)
    outputs: OutputSettings = field(default_factory=OutputSettings)
    box_upload: BoxUploadSettings = field(default_factory=BoxUploadSettings)
    calibration_path: str = "calibrations/assay_calibration.json"
    background_meta_path: str = ""
    current_background_path: str = ""
    previous_background_path: str = ""
    last_run_dir: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssayProfile":
        payload = dict(data or {})
        return cls(
            name=str(payload.get("name", "default")),
            schema_version=int(payload.get("schema_version", PROFILE_SCHEMA_VERSION)),
            description=str(payload.get("description", "") or ""),
            created_at=str(payload.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))),
            updated_at=str(payload.get("updated_at", time.strftime("%Y-%m-%dT%H:%M:%S"))),
            channel_camera=CameraSettings.from_dict(payload.get("channel_camera"), default_device="auto:channel"),
            assay_camera=CameraSettings.from_dict(payload.get("assay_camera"), default_device="auto:assay"),
            assay_duration_s=float(payload.get("assay_duration_s", payload.get("assay_duration", 10.0))),
            record_preroll_s=float(payload.get("record_preroll_s", 0.0)),
            threshold_default_fraction=float(payload.get("threshold_default_fraction", 0.50)),
            transform=TransformSettings.from_dict(payload.get("transform")),
            detector=DetectorSettings.from_dict(payload.get("detector")),
            analysis=AnalysisSettings.from_dict(payload.get("analysis")),
            motor=MotorSettings.from_dict(payload.get("motor")),
            outputs=OutputSettings.from_dict(payload.get("outputs")),
            box_upload=BoxUploadSettings.from_dict(payload.get("box_upload")),
            calibration_path=str(payload.get("calibration_path", "calibrations/assay_calibration.json")),
            background_meta_path=str(payload.get("background_meta_path", "") or ""),
            current_background_path=str(payload.get("current_background_path", "") or ""),
            previous_background_path=str(payload.get("previous_background_path", "") or ""),
            last_run_dir=str(payload.get("last_run_dir", "") or ""),
            notes=str(payload.get("notes", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["transform"] = self.transform.to_dict()
        payload["channel_camera"] = self.channel_camera.to_dict()
        payload["assay_camera"] = self.assay_camera.to_dict()
        payload["detector"] = self.detector.to_dict()
        payload["analysis"] = self.analysis.to_dict()
        payload["motor"] = self.motor.to_dict()
        payload["outputs"] = self.outputs.to_dict()
        payload["box_upload"] = self.box_upload.to_dict()
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return payload

    def copy(self, *, new_name: Optional[str] = None) -> "AssayProfile":
        payload = copy.deepcopy(self.to_dict())
        payload["name"] = str(new_name or self.name)
        payload["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        payload["updated_at"] = payload["created_at"]
        payload["last_run_dir"] = ""
        return AssayProfile.from_dict(payload)

    @property
    def slug(self) -> str:
        return _slugify(self.name)


class ProfileStore:
    def __init__(self, root_dir: str | Path):
        self.root_dir = ensure_dir(root_dir)
        self.last_used_path = self.root_dir / ".last_profile.json"

    def profile_path(self, name: str) -> Path:
        return self.root_dir / f"{_slugify(name)}.json"

    def list_profiles(self) -> List[Path]:
        return sorted(self.root_dir.glob("*.json"))

    def list_profile_names(self) -> List[str]:
        names: List[str] = []
        for path in self.list_profiles():
            try:
                names.append(AssayProfile.from_dict(load_json(path)).name)
            except Exception:
                names.append(path.stem)
        return names

    def load_profile(self, name_or_path: str | Path) -> AssayProfile:
        path = Path(name_or_path)
        if path.suffix.lower() != ".json":
            path = self.profile_path(str(name_or_path))
        return AssayProfile.from_dict(load_json(path))

    def save_profile(self, profile: AssayProfile) -> Path:
        path = self.profile_path(profile.name)
        save_json(path, profile.to_dict())
        self.set_last_used(path)
        return path

    def duplicate_profile(self, existing: str | Path, new_name: str) -> Path:
        profile = self.load_profile(existing)
        return self.save_profile(profile.copy(new_name=new_name))

    def create_profile(self, name: str) -> AssayProfile:
        return AssayProfile(name=str(name))

    def set_last_used(self, name_or_path: str | Path) -> None:
        path = Path(name_or_path)
        if path.suffix.lower() != ".json":
            path = self.profile_path(str(name_or_path))
        save_json(self.last_used_path, {"path": str(path.resolve())})

    def get_last_used_path(self) -> Optional[Path]:
        if not self.last_used_path.exists():
            return None
        try:
            data = load_json(self.last_used_path)
        except Exception:
            return None
        raw = str(data.get("path", "") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.exists() else None

    def load_last_used(self) -> Optional[AssayProfile]:
        path = self.get_last_used_path()
        if path is None:
            return None
        try:
            return self.load_profile(path)
        except Exception:
            return None
