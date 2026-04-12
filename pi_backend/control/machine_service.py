from __future__ import annotations

import logging

from pi_backend.adapters.detection_result_reader import DetectionResultReader
from pi_backend.adapters.motion_adapter import MotionAdapter
from pi_backend.adapters.vacuum_adapter import VacuumAdapter
from pi_backend.adapters.vibration_adapter import VibrationAdapter
from pi_backend.control.assay_service import AssayService
from pi_backend.control.classify_service import ClassifyService
from pi_backend.core.config_runtime import BackendRuntimeConfig
from pi_backend.core.logging_bridge import attach_runtime_log_handler
from pi_backend.core.runtime_state import DetectionSummary, RuntimeStateStore
from shared.state.state_enums import (
    BackendLifecycleState,
    ClientControllerState,
    OrchestratorState,
    TaskState,
)


class MachineService:
    """Pi-side service wrapper around the existing local hardware/domain modules."""

    def __init__(
        self,
        runtime_state: RuntimeStateStore,
        runtime_config: BackendRuntimeConfig,
        logger_name: str = "pi_backend.machine_service",
    ):
        self.runtime_state = runtime_state
        self.runtime_config = runtime_config
        self.logger = attach_runtime_log_handler(logging.getLogger(logger_name), runtime_state)
        self.motion = MotionAdapter()
        self.vacuum = VacuumAdapter()
        self.vibration = VibrationAdapter()
        self.detection_reader = DetectionResultReader(runtime_config.detection_result_path)
        self.assay_service = AssayService(runtime_state, self.vibration, self.logger)
        self.classify_service = ClassifyService(runtime_state, self.logger)
        self._initialize_runtime_state()

    def _initialize_runtime_state(self) -> None:
        self.runtime_state.set_backend_lifecycle_state(
            BackendLifecycleState.STARTING_BACKEND,
            "Initializing Pi machine service.",
        )
        self.set_vacuum(False)
        self.set_vibration(False)
        self.runtime_state.set_current_position_mm(self.motion.get_current_position())
        self.runtime_state.set_controller_state(
            ClientControllerState.CLIENT_DISCONNECTED,
            "Waiting for remote client.",
        )
        self.runtime_state.set_subsystem_health("motion_available", True)
        self.runtime_state.set_subsystem_health("vacuum_available", True)
        self.runtime_state.set_subsystem_health("vibration_available", True)
        self.runtime_state.set_subsystem_health("detection_reader_available", True)
        self.runtime_state.set_subsystem_health("classifier_available", True)
        self.runtime_state.set_subsystem_health("motion_simulation", self.motion.simulation_enabled)
        self.runtime_state.set_subsystem_health("vacuum_simulation", self.vacuum.simulation_enabled)
        self.runtime_state.set_subsystem_health("vibration_simulation", self.vibration.simulation_enabled)
        self.refresh_detection_summary()
        self.runtime_state.set_backend_lifecycle_state(
            BackendLifecycleState.WAITING_FOR_CLIENT,
            "Pi backend initialized and waiting for client.",
        )
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Machine service initialized.")

    def home(self) -> float:
        self.runtime_state.begin_task("home", TaskState.HOMING_RUNNING, "Homing gantry.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting home task.")
        self.logger.info("Home command received.")

        try:
            self.motion.home_to_zero()
        except Exception:
            self.runtime_state.fail_task(TaskState.HOMING_ERROR, "Homing failed.")
            self.logger.exception("Homing failed.")
            raise

        position = self.motion.get_current_position()
        self.runtime_state.set_current_position_mm(position)
        self.runtime_state.complete_task(TaskState.HOMING_COMPLETE, "Homing complete.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Homing complete at %.2f mm.", position)
        return position

    def move_absolute(self, target_mm: float, move_time: float | None = None) -> float:
        self.runtime_state.begin_task("move_absolute", TaskState.MOVE_RUNNING, f"Moving to {target_mm:.2f} mm.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting absolute move.")
        self.logger.info("Absolute move requested to %.2f mm.", target_mm)

        try:
            self.motion.move_absolute(target_mm, move_time)
        except Exception:
            self.runtime_state.fail_task(TaskState.MOVE_ERROR, "Absolute move failed.")
            self.logger.exception("Absolute move failed.")
            raise

        position = self.motion.get_current_position()
        self.runtime_state.set_current_position_mm(position)
        self.runtime_state.complete_task(TaskState.MOVE_COMPLETE, f"Reached {position:.2f} mm.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Absolute move complete at %.2f mm.", position)
        return position

    def move_relative(self, delta_mm: float, move_time: float | None = None) -> float:
        self.runtime_state.begin_task("move_relative", TaskState.MOVE_RUNNING, f"Moving by {delta_mm:.2f} mm.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting relative move.")
        self.logger.info("Relative move requested by %.2f mm.", delta_mm)

        try:
            self.motion.move_relative(delta_mm, move_time)
        except Exception:
            self.runtime_state.fail_task(TaskState.MOVE_ERROR, "Relative move failed.")
            self.logger.exception("Relative move failed.")
            raise

        position = self.motion.get_current_position()
        self.runtime_state.set_current_position_mm(position)
        self.runtime_state.complete_task(TaskState.MOVE_COMPLETE, f"Reached {position:.2f} mm.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Relative move complete at %.2f mm.", position)
        return position

    def set_vacuum(self, enabled: bool) -> bool:
        request_state = OrchestratorState.VACUUM_ON_REQUESTED if enabled else OrchestratorState.VACUUM_OFF_REQUESTED
        self.runtime_state.set_orchestrator_state(request_state, "Applying vacuum output.")
        self.logger.info("Setting vacuum to %s.", "ON" if enabled else "OFF")
        self.vacuum.set_enabled(enabled)
        self.runtime_state.set_vacuum_on(enabled)
        self.runtime_state.set_orchestrator_state(OrchestratorState.ACTUATOR_COMPLETE, "Vacuum state applied.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        return enabled

    def set_vibration(self, enabled: bool) -> bool:
        request_state = OrchestratorState.VIBRATION_ON_REQUESTED if enabled else OrchestratorState.VIBRATION_OFF_REQUESTED
        self.runtime_state.set_orchestrator_state(request_state, "Applying vibration output.")
        self.logger.info("Setting vibration to %s.", "ON" if enabled else "OFF")
        self.vibration.set_enabled(enabled)
        self.runtime_state.set_vibration_on(enabled)
        self.runtime_state.set_orchestrator_state(OrchestratorState.ACTUATOR_COMPLETE, "Vibration state applied.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        return enabled

    def run_assay(self) -> None:
        self.assay_service.run()

    def classify_fly(self) -> dict[str, object]:
        return self.classify_service.run()

    def refresh_detection_summary(self) -> DetectionSummary:
        summary = self.detection_reader.read_summary()
        self.runtime_state.set_detection_summary(summary)
        return summary
