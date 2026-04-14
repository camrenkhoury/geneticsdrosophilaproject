from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bootstrap import PROJECT_ROOT, ensure_repo_paths

ensure_repo_paths()
import config  # noqa: E402


@dataclass
class VialDefinition:
    vial_id: str
    label: str
    target_sex: str
    position_mm: float
    max_count: int = config.DEFAULT_VIAL_CAPACITY

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VialDefinition":
        payload = dict(data or {})
        return cls(
            vial_id=str(payload.get("vial_id", payload.get("id", "V1"))),
            label=str(payload.get("label", payload.get("vial_id", "V1"))),
            target_sex=str(payload.get("target_sex", payload.get("sex", "male"))).lower(),
            position_mm=float(payload.get("position_mm", 0.0)),
            max_count=int(payload.get("max_count", payload.get("capacity", config.DEFAULT_VIAL_CAPACITY))),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OperatorSettings:
    active_assay_profile: str = "stitch_operator"
    sexing_model_path: str = "models/best.pt"
    sexing_capture_dir: str = "outputs/sexing"
    sexing_capture_command: str = "/usr/bin/rpicam-still"
    sexing_uncertain_threshold: float = 0.70

    channel_background_path: str = "fin6/backgrounds/channel_bg.png"
    channel_calibration_path: str = "fin6/calibrations/channel_calibration.json"
    channel_output_dir: str = "fin6/outputs/channel"
    channel_device: str = "auto:channel"
    channel_preferred_hint: str = ""
    channel_width: int = 1920
    channel_height: int = 1080
    channel_fps: int = 30
    channel_mm: float = config.CHANNEL_LENGTH
    channel_score_thresh: int = 20
    channel_band_half_width: int = 35
    channel_no_align: bool = False

    pickup_offset_mm: float = config.CHANNEL_PICKUP_OFFSET_MM
    channel_camera_position_mm: float = config.CHANNEL_CAMERA_POSITION_MM
    chamber_position_mm: float = config.CHAMBER_CENTER
    chamber_clear_offset_mm: float = config.CHAMBER_REPOSITION_OFFSET_MM
    vacuum_pick_delay_s: float = config.VACUUM_PICK_DELAY
    vacuum_drop_delay_s: float = config.VACUUM_DROP_DELAY
    classification_delay_s: float = config.CLASSIFICATION_DELAY

    confirmation_timeout_s: float = 300.0
    vial_definitions: List[VialDefinition] = field(default_factory=list)

    @classmethod
    def default(cls) -> "OperatorSettings":
        vial_defs = [
            VialDefinition(
                vial_id=vial_id,
                label=vial_id,
                target_sex=str(spec["sex"]),
                position_mm=float(spec["position_mm"]),
                max_count=int(spec["capacity"]),
            )
            for vial_id, spec in config.DEFAULT_VIAL_TARGETS.items()
        ]
        return cls(vial_definitions=vial_defs)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "OperatorSettings":
        base = cls.default()
        payload = dict(data or {})
        vial_defs = payload.get("vial_definitions")
        if vial_defs:
            base.vial_definitions = [VialDefinition.from_dict(item) for item in vial_defs]
        for field_name in [
            "active_assay_profile",
            "sexing_model_path",
            "sexing_capture_dir",
            "sexing_capture_command",
            "sexing_uncertain_threshold",
            "channel_background_path",
            "channel_calibration_path",
            "channel_output_dir",
            "channel_device",
            "channel_preferred_hint",
            "channel_width",
            "channel_height",
            "channel_fps",
            "channel_mm",
            "channel_score_thresh",
            "channel_band_half_width",
            "channel_no_align",
            "pickup_offset_mm",
            "channel_camera_position_mm",
            "chamber_position_mm",
            "chamber_clear_offset_mm",
            "vacuum_pick_delay_s",
            "vacuum_drop_delay_s",
            "classification_delay_s",
            "confirmation_timeout_s",
        ]:
            if field_name in payload:
                setattr(base, field_name, payload[field_name])
        return base

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["vial_definitions"] = [item.to_dict() for item in self.vial_definitions]
        return payload


class OperatorSettingsStore:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path or (PROJECT_ROOT / "stitch_operator" / "operator_settings.json")).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> OperatorSettings:
        if not self.path.exists():
            settings = OperatorSettings.default()
            self.save(settings)
            return settings
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return OperatorSettings.from_dict(data)

    def save(self, settings: OperatorSettings) -> Path:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(settings.to_dict(), handle, indent=2)
        return self.path


def resolve_repo_path(path_text: str | Path) -> Path:
    path = Path(str(path_text)).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()
