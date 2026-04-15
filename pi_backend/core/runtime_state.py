from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from shared.state.state_enums import (
    BackendLifecycleState,
    ClientControllerState,
    OrchestratorState,
    TaskState,
)


@dataclass(slots=True, frozen=True)
class LogEntry:
    created_at: datetime
    level: str
    message: str


@dataclass(slots=True)
class ClassifierResultSummary:
    result_class: str = "UNCERTAIN"
    confidence: float = 0.0
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DetectionSummary:
    source_path: str = ""
    source_exists: bool = False
    source_mtime: float | None = None
    preview_path: str = ""
    preview_exists: bool = False
    preview_mtime: float | None = None
    status: str = "unknown"
    fly_remaining: bool | None = None
    x_positions_mm: list[float] = field(default_factory=list)
    corrected_positions_mm: list[float] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeStateSnapshot:
    status_revision: int = 0
    backend_lifecycle_state: BackendLifecycleState = BackendLifecycleState.STARTING_BACKEND
    backend_boot_degraded: bool = False
    controller_state: ClientControllerState = ClientControllerState.CLIENT_DISCONNECTED
    orchestrator_state: OrchestratorState = OrchestratorState.SYSTEM_IDLE
    task_state: TaskState | None = None
    current_task: str | None = None
    current_position_mm: float = 0.0
    vacuum_on: bool = False
    vibration_on: bool = False
    stop_requested: bool = False
    latest_message: str = "Runtime state initialized."
    recent_logs: list[LogEntry] = field(default_factory=list)
    classifier_result: ClassifierResultSummary | None = None
    detection_summary: DetectionSummary = field(default_factory=DetectionSummary)
    subsystem_health: dict[str, bool | str | float] = field(default_factory=dict)
    subsystem_errors: dict[str, str] = field(default_factory=dict)


class RuntimeStateStore:
    """Thread-safe source of truth for backend machine state."""

    def __init__(self, recent_log_limit: int = 200):
        self._lock = RLock()
        self._recent_log_limit = recent_log_limit
        self._recent_logs: deque[LogEntry] = deque(maxlen=recent_log_limit)
        self._snapshot = RuntimeStateSnapshot()

    def snapshot(self) -> RuntimeStateSnapshot:
        with self._lock:
            snapshot_copy = deepcopy(self._snapshot)
            snapshot_copy.recent_logs = list(self._recent_logs)
            return snapshot_copy

    def _bump_status_revision_locked(self) -> None:
        self._snapshot.status_revision += 1

    def append_log(self, level: str, message: str) -> LogEntry:
        entry = LogEntry(created_at=datetime.now(timezone.utc), level=level.upper(), message=message)
        with self._lock:
            self._recent_logs.append(entry)
            if self._snapshot.latest_message != message:
                self._snapshot.latest_message = message
            self._bump_status_revision_locked()
        return entry

    def set_backend_lifecycle_state(
        self,
        state: BackendLifecycleState,
        message: str | None = None,
    ) -> None:
        with self._lock:
            changed = False
            if self._snapshot.backend_lifecycle_state != state:
                self._snapshot.backend_lifecycle_state = state
                changed = True
            if message is not None and self._snapshot.latest_message != message:
                self._snapshot.latest_message = message
                changed = True
            if changed:
                self._bump_status_revision_locked()

    def set_controller_state(
        self,
        state: ClientControllerState,
        message: str | None = None,
    ) -> None:
        with self._lock:
            changed = False
            if self._snapshot.controller_state != state:
                self._snapshot.controller_state = state
                changed = True
            if message is not None and self._snapshot.latest_message != message:
                self._snapshot.latest_message = message
                changed = True
            if changed:
                self._bump_status_revision_locked()

    def set_backend_boot_degraded(self, degraded: bool) -> None:
        with self._lock:
            if self._snapshot.backend_boot_degraded == degraded:
                return
            self._snapshot.backend_boot_degraded = degraded
            self._bump_status_revision_locked()

    def set_orchestrator_state(
        self,
        state: OrchestratorState,
        message: str | None = None,
    ) -> None:
        with self._lock:
            changed = False
            if self._snapshot.orchestrator_state != state:
                self._snapshot.orchestrator_state = state
                changed = True
            if message is not None and self._snapshot.latest_message != message:
                self._snapshot.latest_message = message
                changed = True
            if changed:
                self._bump_status_revision_locked()

    def begin_task(self, task_name: str, task_state: TaskState, message: str) -> None:
        with self._lock:
            changed = (
                self._snapshot.current_task != task_name
                or self._snapshot.task_state != task_state
                or self._snapshot.orchestrator_state != OrchestratorState.TASK_STARTING
                or self._snapshot.latest_message != message
            )
            self._snapshot.current_task = task_name
            self._snapshot.task_state = task_state
            self._snapshot.orchestrator_state = OrchestratorState.TASK_STARTING
            self._snapshot.latest_message = message
            if changed:
                self._bump_status_revision_locked()

    def complete_task(self, task_state: TaskState, message: str) -> None:
        with self._lock:
            changed = (
                self._snapshot.task_state != task_state
                or self._snapshot.current_task is not None
                or self._snapshot.orchestrator_state != OrchestratorState.TASK_COMPLETE
                or self._snapshot.latest_message != message
            )
            self._snapshot.task_state = task_state
            self._snapshot.current_task = None
            self._snapshot.orchestrator_state = OrchestratorState.TASK_COMPLETE
            self._snapshot.latest_message = message
            if changed:
                self._bump_status_revision_locked()

    def fail_task(self, task_state: TaskState, message: str) -> None:
        with self._lock:
            changed = (
                self._snapshot.task_state != task_state
                or self._snapshot.current_task is not None
                or self._snapshot.orchestrator_state != OrchestratorState.TASK_ERROR
                or self._snapshot.latest_message != message
            )
            self._snapshot.task_state = task_state
            self._snapshot.current_task = None
            self._snapshot.orchestrator_state = OrchestratorState.TASK_ERROR
            self._snapshot.latest_message = message
            if changed:
                self._bump_status_revision_locked()

    def set_current_position_mm(self, position_mm: float) -> None:
        with self._lock:
            if self._snapshot.current_position_mm == position_mm:
                return
            self._snapshot.current_position_mm = position_mm
            self._bump_status_revision_locked()

    def set_vacuum_on(self, enabled: bool) -> None:
        with self._lock:
            if self._snapshot.vacuum_on == enabled:
                return
            self._snapshot.vacuum_on = enabled
            self._bump_status_revision_locked()

    def set_vibration_on(self, enabled: bool) -> None:
        with self._lock:
            if self._snapshot.vibration_on == enabled:
                return
            self._snapshot.vibration_on = enabled
            self._bump_status_revision_locked()

    def set_stop_requested(self, requested: bool) -> None:
        with self._lock:
            if self._snapshot.stop_requested == requested:
                return
            self._snapshot.stop_requested = requested
            self._bump_status_revision_locked()

    def set_classifier_result(self, result: ClassifierResultSummary) -> None:
        with self._lock:
            if self._snapshot.classifier_result == result:
                return
            self._snapshot.classifier_result = deepcopy(result)
            self._bump_status_revision_locked()

    def set_detection_summary(self, summary: DetectionSummary) -> None:
        with self._lock:
            if self._snapshot.detection_summary == summary:
                return
            self._snapshot.detection_summary = deepcopy(summary)
            self._bump_status_revision_locked()

    def set_subsystem_health(self, name: str, value: bool | str | float) -> None:
        with self._lock:
            if self._snapshot.subsystem_health.get(name) == value:
                return
            self._snapshot.subsystem_health[name] = value
            self._bump_status_revision_locked()

    def set_subsystem_error(self, name: str, error: str | None) -> None:
        with self._lock:
            if error is None:
                if name not in self._snapshot.subsystem_errors:
                    return
                self._snapshot.subsystem_errors.pop(name, None)
                self._bump_status_revision_locked()
                return
            if self._snapshot.subsystem_errors.get(name) == error:
                return
            self._snapshot.subsystem_errors[name] = error
            self._bump_status_revision_locked()
