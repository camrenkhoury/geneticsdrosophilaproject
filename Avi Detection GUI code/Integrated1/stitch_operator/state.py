from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class WorkflowStage(str, Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    READY = "ready"
    CHANNEL = "channel"
    ROUTING = "routing"
    ASSAY = "assay"
    PROCESSING = "processing"
    RESULTS = "results"
    ERROR = "error"


@dataclass
class ReadinessState:
    homed: bool = False
    model_ready: bool = False
    channel_background_ready: bool = False
    channel_calibration_ready: bool = False
    assay_background_ready: bool = False
    assay_calibration_ready: bool = False
    active_profile: str = ""
    channel_camera: str = "unknown"
    assay_camera: str = "unknown"


@dataclass
class ChannelState:
    captured_at: str = ""
    count: int = 0
    fly_remaining: bool = False
    x_positions_mm: List[float] = field(default_factory=list)
    raw_image_path: str = ""
    annotated_image_path: str = ""
    mask_image_path: str = ""
    result_json_path: str = ""
    stale: bool = True


@dataclass
class SexingState:
    captured_at: str = ""
    label: str = "--"
    confidence: float = 0.0
    image_path: str = ""
    detail: str = ""
    uncertain: bool = False
    model_path: str = ""
    count: int = 0
    errors: List[str] = field(default_factory=list)
    debug_image_path: str = ""
    occupancy_score: float = 0.0
    occupancy_detail: str = ""


@dataclass
class AssayRunState:
    run_dir: str = ""
    preview_image_path: str = ""
    processed_dir: str = ""
    processed_at: str = ""
    pdf_path: str = ""
    processing_json: str = ""
    summary_csv_path: str = ""
    upload_status: str = ""
    unique_crossings_total: int = 0
    duration_s: float = 0.0
    per_vial_summary: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class VialState:
    vial_id: str
    label: str
    target_sex: str
    position_mm: float
    max_count: int
    current_count: int = 0
    status: str = "READY"
    last_routed_at: str = ""


@dataclass
class OperatorState:
    stage: WorkflowStage = WorkflowStage.IDLE
    stage_label: str = "Idle"
    next_action: str = "Initialize"
    status_message: str = "System idle."
    brief_error: str = ""
    error_detail: str = ""
    busy: bool = False
    active_task: str = ""
    hardware_position_mm: float = 0.0
    selected_destination: str = ""
    current_target: str = ""
    started_monotonic: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.time)
    readiness: ReadinessState = field(default_factory=ReadinessState)
    channel: ChannelState = field(default_factory=ChannelState)
    sexing: SexingState = field(default_factory=SexingState)
    assay: AssayRunState = field(default_factory=AssayRunState)
    vials: List[VialState] = field(default_factory=list)
    recent_logs: List[str] = field(default_factory=list)

    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def to_debug_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["stage"] = self.stage.value
        payload["uptime_seconds"] = self.uptime_seconds()
        return payload

    def to_debug_json(self) -> str:
        return json.dumps(self.to_debug_dict(), indent=2, default=str)
