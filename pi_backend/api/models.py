from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from pi_backend.core.runtime_state import (
    ClassifierResultSummary,
    DetectionSummary,
    LogEntry,
    RuntimeStateSnapshot,
)


class MoveAbsoluteRequest(BaseModel):
    target_mm: float = Field(..., description="Absolute nozzle-center target position in mm.")
    move_time: float | None = Field(default=None, ge=0.0, description="Optional target move duration in seconds.")


class MoveRelativeRequest(BaseModel):
    distance_mm: float = Field(..., description="Relative move distance in mm.")
    move_time: float | None = Field(default=None, ge=0.0, description="Optional target move duration in seconds.")


class VacuumRequest(BaseModel):
    enabled: bool


class VibrationRequest(BaseModel):
    enabled: bool


class LogEntryModel(BaseModel):
    created_at: datetime
    level: str
    message: str

    @classmethod
    def from_entry(cls, entry: LogEntry) -> "LogEntryModel":
        return cls(
            created_at=entry.created_at,
            level=entry.level,
            message=entry.message,
        )


class ClassificationResultModel(BaseModel):
    result_class: str
    confidence: float
    errors: list[str]
    raw: dict[str, Any]

    @classmethod
    def from_summary(cls, summary: ClassifierResultSummary | None) -> "ClassificationResultModel | None":
        if summary is None:
            return None
        return cls(
            result_class=summary.result_class,
            confidence=summary.confidence,
            errors=list(summary.errors),
            raw=dict(summary.raw),
        )


class DetectionSummaryModel(BaseModel):
    source_path: str
    source_exists: bool
    source_mtime: float | None
    status: str
    fly_remaining: bool | None
    x_positions_mm: list[float]
    corrected_positions_mm: list[float]

    @classmethod
    def from_summary(cls, summary: DetectionSummary) -> "DetectionSummaryModel":
        return cls(
            source_path=summary.source_path,
            source_exists=summary.source_exists,
            source_mtime=summary.source_mtime,
            status=summary.status,
            fly_remaining=summary.fly_remaining,
            x_positions_mm=list(summary.x_positions_mm),
            corrected_positions_mm=list(summary.corrected_positions_mm),
        )


class HealthResponse(BaseModel):
    ok: bool
    backend_lifecycle_state: str
    backend_boot_degraded: bool
    api_alive: bool
    motion_available: bool
    vacuum_available: bool
    vibration_available: bool
    detection_reader_available: bool
    classifier_available: bool
    subsystem_errors: dict[str, str]
    message: str


class StatusResponse(BaseModel):
    backend_lifecycle_state: str
    backend_boot_degraded: bool
    controller_state: str
    orchestrator_state: str
    task_state: str | None
    current_task: str | None
    current_position_mm: float
    vacuum_on: bool
    vibration_on: bool
    stop_requested: bool
    latest_message: str
    recent_logs: list[LogEntryModel]
    classification_result: ClassificationResultModel | None
    detection_summary: DetectionSummaryModel
    subsystem_health: dict[str, bool | str | float]
    subsystem_errors: dict[str, str]

    @classmethod
    def from_snapshot(cls, snapshot: RuntimeStateSnapshot) -> "StatusResponse":
        return cls(
            backend_lifecycle_state=str(snapshot.backend_lifecycle_state),
            backend_boot_degraded=snapshot.backend_boot_degraded,
            controller_state=str(snapshot.controller_state),
            orchestrator_state=str(snapshot.orchestrator_state),
            task_state=str(snapshot.task_state) if snapshot.task_state is not None else None,
            current_task=snapshot.current_task,
            current_position_mm=snapshot.current_position_mm,
            vacuum_on=snapshot.vacuum_on,
            vibration_on=snapshot.vibration_on,
            stop_requested=snapshot.stop_requested,
            latest_message=snapshot.latest_message,
            recent_logs=[LogEntryModel.from_entry(entry) for entry in snapshot.recent_logs],
            classification_result=ClassificationResultModel.from_summary(snapshot.classifier_result),
            detection_summary=DetectionSummaryModel.from_summary(snapshot.detection_summary),
            subsystem_health=dict(snapshot.subsystem_health),
            subsystem_errors=dict(snapshot.subsystem_errors),
        )


class CommandResponse(BaseModel):
    ok: bool
    accepted: bool
    command: str
    message: str
    backend_state: str
    orchestrator_state: str
    task_state: str | None
    current_task: str | None

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RuntimeStateSnapshot,
        *,
        ok: bool,
        accepted: bool,
        command: str,
        message: str,
    ) -> "CommandResponse":
        return cls(
            ok=ok,
            accepted=accepted,
            command=command,
            message=message,
            backend_state=str(snapshot.backend_lifecycle_state),
            orchestrator_state=str(snapshot.orchestrator_state),
            task_state=str(snapshot.task_state) if snapshot.task_state is not None else None,
            current_task=snapshot.current_task,
        )


class ErrorResponse(BaseModel):
    ok: bool = False
    message: str
