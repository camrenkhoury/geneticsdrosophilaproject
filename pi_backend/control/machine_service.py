from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from pi_backend.adapters.detection_result_reader import DetectionResultReader
from pi_backend.adapters.motion_adapter import MotionAdapter
from pi_backend.adapters.vacuum_adapter import VacuumAdapter
from pi_backend.adapters.vibration_adapter import VibrationAdapter
from pi_backend.control.assay_service import AssayService
from pi_backend.control.classify_service import ClassifyService
from pi_backend.core.config_runtime import BackendRuntimeConfig
from pi_backend.core.logging_bridge import attach_runtime_log_handler
from pi_backend.core.runtime_state import DetectionSummary, RuntimeStateStore
from pi_backend.core.subsystem_support import SubsystemUnavailableError
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
        self._fin6_bridge = None
        self._initialize_runtime_state()

    def _initialize_runtime_state(self) -> None:
        self.runtime_state.set_backend_lifecycle_state(
            BackendLifecycleState.STARTING_BACKEND,
            "Initializing Pi machine service.",
        )
        self._initialize_subsystems()
        self.runtime_state.set_current_position_mm(0.0)
        self.runtime_state.set_controller_state(
            ClientControllerState.CLIENT_DISCONNECTED,
            "Waiting for remote client.",
        )
        self.refresh_detection_summary()
        self.runtime_state.set_backend_lifecycle_state(
            BackendLifecycleState.WAITING_FOR_CLIENT,
            "Pi backend initialized and waiting for client.",
        )
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Machine service initialized.")

    def _initialize_subsystems(self) -> None:
        self._record_component_state("motion", deferred=True)
        self._record_component_state("vacuum", deferred=True)
        self._record_component_state("vibration", deferred=True)
        self.classify_service.initialize()
        self.assay_service.initialize()
        self._record_component_state(
            "classifier",
            available=self.classify_service.available,
            simulation_enabled=False,
            last_error=self.classify_service.last_error,
        )
        self._record_component_state(
            "assay",
            available=self.assay_service.available,
            simulation_enabled=False,
            last_error=self.assay_service.last_error,
        )

    def _record_component_state(
        self,
        name: str,
        *,
        available: bool = False,
        simulation_enabled: bool = False,
        last_error: str | None = None,
        deferred: bool = False,
    ) -> None:
        if deferred:
            status = "deferred"
            available = False
            simulation_enabled = False
            last_error = None
        else:
            status = "unavailable"
            if available:
                status = "simulation" if simulation_enabled else "available"

        self.runtime_state.set_subsystem_health(f"{name}_deferred", deferred)
        self.runtime_state.set_subsystem_health(f"{name}_available", available)
        self.runtime_state.set_subsystem_health(f"{name}_simulation", simulation_enabled)
        self.runtime_state.set_subsystem_health(f"{name}_status", status)
        self.runtime_state.set_subsystem_error(name, last_error)

        if deferred:
            return

        boot_degraded = self.runtime_state.snapshot().backend_boot_degraded
        if simulation_enabled or not available:
            self.runtime_state.set_backend_boot_degraded(True)
            if last_error:
                self.logger.warning("%s subsystem degraded: %s", name, last_error)
            else:
                self.logger.warning("%s subsystem running in simulation mode.", name)
            return

        self.runtime_state.set_backend_boot_degraded(boot_degraded)

    def _subsystem_status(self, subsystem: str) -> str:
        snapshot = self.runtime_state.snapshot()
        return str(snapshot.subsystem_health.get(f"{subsystem}_status", "unavailable"))

    def _ensure_motion_ready(self) -> None:
        if self._subsystem_status("motion") == "deferred":
            self.motion.initialize()
            self._record_component_state(
                "motion",
                available=self.motion.available,
                simulation_enabled=self.motion.simulation_enabled,
                last_error=self.motion.last_error,
            )
            if self.motion.available:
                self.runtime_state.set_current_position_mm(self.motion.get_current_position())
        error = self._unavailable_message("motion")
        if error is not None:
            raise SubsystemUnavailableError("motion", self._unavailable_detail(error, "motion"))

    def _ensure_vacuum_ready(self) -> None:
        if self._subsystem_status("vacuum") == "deferred":
            self.vacuum.initialize()
            self._record_component_state(
                "vacuum",
                available=self.vacuum.available,
                simulation_enabled=self.vacuum.simulation_enabled,
                last_error=self.vacuum.last_error,
            )
        error = self._unavailable_message("vacuum")
        if error is not None:
            raise SubsystemUnavailableError("vacuum", self._unavailable_detail(error, "vacuum"))

    def _ensure_vibration_ready(self) -> None:
        if self._subsystem_status("vibration") == "deferred":
            self.vibration.initialize()
            self._record_component_state(
                "vibration",
                available=self.vibration.available,
                simulation_enabled=self.vibration.simulation_enabled,
                last_error=self.vibration.last_error,
            )
        error = self._unavailable_message("vibration")
        if error is not None:
            raise SubsystemUnavailableError("vibration", self._unavailable_detail(error, "vibration"))

    def _ensure_assay_ready(self) -> None:
        self._ensure_vibration_ready()
        error = self._unavailable_message("assay")
        if error is not None:
            raise SubsystemUnavailableError("assay", self._unavailable_detail(error, "assay"))

    def _skip_deferred_safe_off(self, subsystem: str, enabled: bool) -> bool:
        return not enabled and self._subsystem_status(subsystem) == "deferred"

    @staticmethod
    def _unavailable_detail(message: str, subsystem: str) -> str:
        prefix = f"{subsystem} subsystem unavailable: "
        if message.startswith(prefix):
            return message[len(prefix) :]
        fallback = f"{subsystem} subsystem unavailable."
        if message == fallback:
            return "no additional detail"
        return message

    def _unavailable_message(self, subsystem: str) -> str | None:
        snapshot = self.runtime_state.snapshot()
        available = bool(snapshot.subsystem_health.get(f"{subsystem}_available", False))
        if available:
            return None

        error_detail = snapshot.subsystem_errors.get(subsystem)
        if error_detail:
            return f"{subsystem} subsystem unavailable: {error_detail}"
        return f"{subsystem} subsystem unavailable."

    def validate_motion_command(self) -> str | None:
        try:
            self._ensure_motion_ready()
        except SubsystemUnavailableError as exc:
            return str(exc)
        return None

    def validate_vacuum_command(self) -> str | None:
        try:
            self._ensure_vacuum_ready()
        except SubsystemUnavailableError as exc:
            return str(exc)
        return None

    def validate_vibration_command(self) -> str | None:
        try:
            self._ensure_vibration_ready()
        except SubsystemUnavailableError as exc:
            return str(exc)
        return None

    def validate_classifier_command(self) -> str | None:
        return self._unavailable_message("classifier")

    def validate_assay_command(self) -> str | None:
        try:
            self._ensure_assay_ready()
        except SubsystemUnavailableError as exc:
            return str(exc)
        try:
            fin6_bridge = self._load_fin6_bridge()
        except Exception as exc:
            return f"Assay setup integration is unavailable: {exc}"
        status = fin6_bridge.get_setup_status()
        if status.assay_ready:
            return None
        return (
            "Assay Setup is missing on the Pi.\n"
            "Open Assay Setup on the Pi, capture the saved assay background, run assay calibration, and save the setup before running the assay."
        )

    def _load_fin6_bridge(self):
        if self._fin6_bridge is not None:
            return self._fin6_bridge
        self._fin6_bridge = importlib.import_module("host_app.operator_bridge")
        return self._fin6_bridge

    def _channel_output_dir(self) -> Path:
        try:
            fin6_bridge = self._load_fin6_bridge()
            status = fin6_bridge.get_setup_status()
            return Path(status.channel.output_dir)
        except Exception:
            return Path(self.runtime_config.channel_output_directory)

    def _channel_result_path(self) -> Path:
        return self._channel_output_dir() / "last_channel_result.json"

    def get_channel_annotated_preview_path(self) -> Path:
        return self._channel_output_dir() / "last_channel_annotated.png"

    def get_fin6_setup_status(self) -> dict[str, Any]:
        fin6_bridge = self._load_fin6_bridge()
        return fin6_bridge.setup_status_to_dict(fin6_bridge.get_setup_status())

    def launch_fin6_setup(self) -> dict[str, Any]:
        fin6_bridge = self._load_fin6_bridge()
        process = fin6_bridge.launch_fin6_gui()
        pid = getattr(process, "pid", None)
        message = "Opened fin6 setup GUI on the Pi."
        if pid is not None:
            message = f"Opened fin6 setup GUI on the Pi (pid {pid})."
        self.runtime_state.append_log("INFO", message)
        return {
            "ok": True,
            "message": message,
            "pid": pid,
        }

    def validate_detect_channel_command(self) -> str | None:
        try:
            fin6_bridge = self._load_fin6_bridge()
        except Exception as exc:
            return f"Channel Detection Setup integration is unavailable: {exc}"
        status = fin6_bridge.get_setup_status()
        if status.channel_ready:
            return None
        return (
            "Channel Detection Setup is missing on the Pi.\n"
            "Preparation steps:\n"
            "1. Empty the channel.\n"
            "2. Move the nozzle out of the camera view.\n"
            "3. Open Channel Detection Setup on the Pi.\n"
            "4. Capture a clean channel background.\n"
            "5. Run channel calibration.\n"
            "6. Save the setup."
        )

    def home(self) -> float:
        self._ensure_motion_ready()
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
        self._ensure_motion_ready()
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
        self._ensure_motion_ready()
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
        if self._skip_deferred_safe_off("vacuum", enabled):
            return False
        self._ensure_vacuum_ready()
        request_state = OrchestratorState.VACUUM_ON_REQUESTED if enabled else OrchestratorState.VACUUM_OFF_REQUESTED
        self.runtime_state.set_orchestrator_state(request_state, "Applying vacuum output.")
        self.logger.info("Setting vacuum to %s.", "ON" if enabled else "OFF")
        try:
            self.vacuum.set_enabled(enabled)
        except Exception:
            self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
            self.logger.exception("Vacuum command failed.")
            raise
        self.runtime_state.set_vacuum_on(enabled)
        self.runtime_state.set_orchestrator_state(OrchestratorState.ACTUATOR_COMPLETE, "Vacuum state applied.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        return enabled

    def set_vibration(self, enabled: bool) -> bool:
        if self._skip_deferred_safe_off("vibration", enabled):
            return False
        self._ensure_vibration_ready()
        request_state = OrchestratorState.VIBRATION_ON_REQUESTED if enabled else OrchestratorState.VIBRATION_OFF_REQUESTED
        self.runtime_state.set_orchestrator_state(request_state, "Applying vibration output.")
        self.logger.info("Setting vibration to %s.", "ON" if enabled else "OFF")
        try:
            self.vibration.set_enabled(enabled)
        except Exception:
            self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
            self.logger.exception("Vibration command failed.")
            raise
        self.runtime_state.set_vibration_on(enabled)
        self.runtime_state.set_orchestrator_state(OrchestratorState.ACTUATOR_COMPLETE, "Vibration state applied.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        return enabled

    def run_assay(self) -> None:
        self._ensure_assay_ready()
        self.assay_service.run()

    def classify_fly(self) -> dict[str, object]:
        return self.classify_service.run()

    def detect_channel(self) -> dict[str, Any]:
        fin6_bridge = self._load_fin6_bridge()
        self.runtime_state.begin_task(
            "detect_channel",
            TaskState.AUTO_WAIT_FOR_DETECTION,
            "Running fin6 channel detection on the Pi.",
        )
        self.runtime_state.set_orchestrator_state(
            OrchestratorState.TASK_STARTING,
            "Starting Pi-side fin6 channel detection.",
        )
        self.logger.info("Running fin6 channel detection on the Pi.")
        try:
            result = fin6_bridge.detect_channel_once_from_saved_settings()
            summary = self.refresh_detection_summary()
        except Exception:
            self.runtime_state.fail_task(TaskState.AUTOMATED_ERROR, "Pi-side fin6 channel detection failed.")
            self.logger.exception("Pi-side fin6 channel detection failed.")
            raise

        count = len(summary.corrected_positions_mm) or len(summary.x_positions_mm)
        self.runtime_state.complete_task(
            TaskState.AUTOMATED_COMPLETE,
            f"Pi-side fin6 channel detection complete. count={count}.",
        )
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Pi-side fin6 channel detection complete with %d positions.", count)
        return result

    def refresh_detection_summary(self) -> DetectionSummary:
        result_path = self._channel_result_path()
        if self.detection_reader.result_path != result_path:
            self.detection_reader = DetectionResultReader(result_path)
        try:
            summary = self.detection_reader.read_summary()
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            self.runtime_state.set_subsystem_health("detection_reader_available", False)
            self.runtime_state.set_subsystem_health("detection_reader_status", "unavailable")
            self.runtime_state.set_subsystem_error("detection_reader", error_message)
            self.runtime_state.set_backend_boot_degraded(True)
            self.logger.exception("Detection reader failed.")
            summary = DetectionSummary(
                source_path=str(self.runtime_config.detection_result_path),
                source_exists=False,
                status="error",
            )
            self.runtime_state.set_detection_summary(summary)
            return summary

        self.runtime_state.set_subsystem_health("detection_reader_available", True)
        self.runtime_state.set_subsystem_health("detection_reader_status", "available")
        self.runtime_state.set_subsystem_error("detection_reader", None)
        self.runtime_state.set_detection_summary(summary)
        return summary
