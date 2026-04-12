from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from host_app.controllers.base_controller import BaseController, ControllerPayload
from shared.config.machine_paths import DETECTION_RESULT_JSON, ensure_code_directory_on_path
from shared.state.state_enums import BackendLifecycleState, ClientControllerState, OrchestratorState

ensure_code_directory_on_path()


class LocalController(BaseController):
    def __init__(self):
        self._vacuum_on = False
        self._vibration_on = False
        self._stop_requested = False
        self._latest_message = "Local controller ready."
        self._runtime_cache: dict[str, Any] | None = None

    def _runtime(self) -> dict[str, Any]:
        if self._runtime_cache is not None:
            return self._runtime_cache

        motion = importlib.import_module("motion")
        vacuum = importlib.import_module("vacuum")
        vibration = importlib.import_module("vibration")
        self._runtime_cache = {
            "motion": motion,
            "vacuum": vacuum,
            "vibration": vibration,
            "assay": importlib.import_module("assay"),
            "fly_classifier": importlib.import_module("fly_classifier"),
            "gpio_available": bool(getattr(motion, "GPIO_AVAILABLE", False)),
        }
        return self._runtime_cache

    def home(self) -> ControllerPayload:
        self._runtime()["motion"].home_to_zero()
        self._stop_requested = False
        self._latest_message = "Local homing complete."
        return self._command_payload("home", "Local homing complete.")

    def move_absolute(self, mm: float) -> ControllerPayload:
        runtime = self._runtime()
        runtime["motion"].move_to_absolute(mm)
        self._stop_requested = False
        self._latest_message = f"Local move complete at {runtime['motion'].get_current_position():.2f} mm."
        return self._command_payload("move_absolute", self._latest_message)

    def move_relative(self, mm: float) -> ControllerPayload:
        runtime = self._runtime()
        runtime["motion"].move_relative(mm)
        self._stop_requested = False
        self._latest_message = f"Local relative move complete at {runtime['motion'].get_current_position():.2f} mm."
        return self._command_payload("move_relative", self._latest_message)

    def set_vacuum(self, enabled: bool) -> ControllerPayload:
        runtime = self._runtime()
        if enabled:
            runtime["vacuum"].vacuum_on()
        else:
            runtime["vacuum"].vacuum_off()
        self._vacuum_on = enabled
        self._latest_message = f"Vacuum {'on' if enabled else 'off'}."
        return self._command_payload("vacuum", self._latest_message)

    def set_vibration(self, enabled: bool) -> ControllerPayload:
        runtime = self._runtime()
        if enabled:
            runtime["vibration"].vibration_on()
        else:
            runtime["vibration"].vibration_off()
        self._vibration_on = enabled
        self._latest_message = f"Vibration {'on' if enabled else 'off'}."
        return self._command_payload("vibration", self._latest_message)

    def stop(self) -> ControllerPayload:
        self._stop_requested = True
        self._latest_message = "Local stop requested."
        return self._command_payload("stop", self._latest_message)

    def classify_fly(self) -> ControllerPayload:
        runtime = self._runtime()
        result = runtime["fly_classifier"].classify_fly()
        self._latest_message = "Local classification complete."
        return {
            **self._command_payload("classify", self._latest_message),
            "result": result,
        }

    def run_assay(self) -> ControllerPayload:
        runtime = self._runtime()
        runtime["assay"].assay()
        self._latest_message = "Local assay complete."
        return self._command_payload("run_assay", self._latest_message)

    def get_status(self) -> ControllerPayload:
        runtime = self._runtime()
        gpio_available = bool(runtime["gpio_available"])
        return {
            "backend_lifecycle_state": str(BackendLifecycleState.BACKEND_READY),
            "backend_boot_degraded": not gpio_available,
            "controller_state": str(ClientControllerState.LOCAL_CONTROLLER_MODE),
            "orchestrator_state": str(OrchestratorState.SYSTEM_IDLE),
            "task_state": None,
            "current_task": None,
            "current_position_mm": float(runtime["motion"].get_current_position()),
            "vacuum_on": self._vacuum_on,
            "vibration_on": self._vibration_on,
            "stop_requested": self._stop_requested,
            "latest_message": self._latest_message,
            "recent_logs": [],
            "classification_result": None,
            "detection_summary": self._build_detection_summary(DETECTION_RESULT_JSON),
            "subsystem_health": {
                "motion_available": True,
                "motion_simulation": not gpio_available,
                "motion_status": "simulation" if not gpio_available else "available",
                "vacuum_available": True,
                "vacuum_simulation": not gpio_available,
                "vacuum_status": "simulation" if not gpio_available else "available",
                "vibration_available": True,
                "vibration_simulation": not gpio_available,
                "vibration_status": "simulation" if not gpio_available else "available",
                "detection_reader_available": True,
                "detection_reader_status": "available",
            },
            "subsystem_errors": {},
        }

    def get_health(self) -> ControllerPayload:
        status = self.get_status()
        return {
            "ok": True,
            "backend_lifecycle_state": status["backend_lifecycle_state"],
            "backend_boot_degraded": status["backend_boot_degraded"],
            "api_alive": True,
            "motion_available": True,
            "vacuum_available": True,
            "vibration_available": True,
            "detection_reader_available": True,
            "classifier_available": True,
            "subsystem_errors": {},
            "message": self._latest_message,
        }

    def _command_payload(self, command: str, message: str) -> ControllerPayload:
        return {
            "ok": True,
            "accepted": True,
            "command": command,
            "message": message,
            "backend_state": str(BackendLifecycleState.BACKEND_READY),
            "orchestrator_state": str(OrchestratorState.SYSTEM_IDLE),
            "task_state": None,
            "current_task": None,
        }

    def _build_detection_summary(self, path: Path) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "source_path": str(path),
            "source_exists": path.exists(),
            "source_mtime": path.stat().st_mtime if path.exists() else None,
            "status": "missing",
            "fly_remaining": None,
            "x_positions_mm": [],
            "corrected_positions_mm": [],
        }

        if not path.exists():
            return summary

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary["status"] = "invalid_json"
            return summary

        raw_positions = payload.get("x_positions_mm")
        positions = raw_positions if isinstance(raw_positions, list) else []
        summary["status"] = "ready" if positions else "empty_positions"
        summary["fly_remaining"] = bool(payload.get("fly_remaining", False))
        summary["x_positions_mm"] = [float(value) for value in positions if isinstance(value, (int, float))]
        summary["corrected_positions_mm"] = list(summary["x_positions_mm"])
        if not summary["fly_remaining"]:
            summary["status"] = "done"
        return summary
