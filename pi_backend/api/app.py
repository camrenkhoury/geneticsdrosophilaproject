from __future__ import annotations

import logging
from threading import Lock, Thread
from typing import Any, Callable

from fastapi import FastAPI

from pi_backend.api.models import CommandResponse, HealthResponse
from pi_backend.api.routes import router
from pi_backend.core.config_runtime import BackendRuntimeConfig, build_backend_runtime_config
from pi_backend.core.runtime_state import RuntimeStateSnapshot, RuntimeStateStore
from pi_backend.core.subsystem_support import SubsystemUnavailableError
from pi_backend.control.machine_service import MachineService
from shared.debug.operation_trace import append_operation_trace
from shared.state.state_enums import BackendLifecycleState, OrchestratorState, TaskState

PI_OPERATION_TRACE_FILENAME = ".pi_operation_trace.log"


class BackendApiContext:
    def __init__(
        self,
        runtime_config: BackendRuntimeConfig,
        runtime_state: RuntimeStateStore,
        machine_service: MachineService,
    ):
        self.runtime_config = runtime_config
        self.runtime_state = runtime_state
        self.machine_service = machine_service
        self.logger = logging.getLogger("pi_backend.api")
        self._worker_lock = Lock()
        self._active_worker: Thread | None = None
        self._active_command: str | None = None

    @staticmethod
    def _failure_task_state_for_command(command: str) -> TaskState:
        normalized = str(command or "").strip().lower()
        if normalized == "home":
            return TaskState.HOMING_ERROR
        if normalized in {"move_absolute", "move_relative"}:
            return TaskState.MOVE_ERROR
        if normalized == "detect_channel":
            return TaskState.DETECT_CHANNEL_ERROR
        if normalized == "classify":
            return TaskState.CLASSIFY_ERROR
        if normalized == "run_assay":
            return TaskState.ASSAY_ERROR
        return TaskState.AUTOMATED_ERROR

    def _finalize_failed_worker_state(self, command: str, exc: Exception) -> None:
        snapshot = self.runtime_state.snapshot()
        if snapshot.current_task is None and (
            snapshot.task_state is None or str(snapshot.task_state).endswith("_ERROR")
        ):
            return
        failure_state = self._failure_task_state_for_command(command)
        failure_message = f"{command} failed: {exc}"
        self._trace(
            "worker_wrapper_force_fail_state",
            command=command,
            failure_state=str(failure_state),
            failure_message=failure_message,
            snapshot_current_task=snapshot.current_task,
            snapshot_task_state=None if snapshot.task_state is None else str(snapshot.task_state),
            snapshot_orchestrator_state=str(snapshot.orchestrator_state),
        )
        self.runtime_state.fail_task(failure_state, failure_message)

    def _trace(self, event: str, **fields: Any) -> None:
        snapshot = self.runtime_state.snapshot()
        trace_fields = {
            "active_command": self._active_command,
            "busy": self.is_busy(),
            "status_revision": snapshot.status_revision,
            "orchestrator_state": str(snapshot.orchestrator_state),
            "task_state": None if snapshot.task_state is None else str(snapshot.task_state),
            "current_task": snapshot.current_task,
            "stop_requested": snapshot.stop_requested,
            "latest_message": snapshot.latest_message,
        }
        trace_fields.update(fields)
        append_operation_trace(
            PI_OPERATION_TRACE_FILENAME,
            "backend_api",
            event,
            **trace_fields,
        )

    @staticmethod
    def _snapshot_has_stale_task_error(snapshot: RuntimeStateSnapshot) -> bool:
        task_state = "" if snapshot.task_state is None else str(snapshot.task_state)
        return (
            snapshot.current_task is None
            and (
                task_state.endswith("_ERROR")
                or snapshot.orchestrator_state == OrchestratorState.TASK_ERROR
            )
        )

    def _clear_stale_error_state_if_idle(self, *, reason: str) -> None:
        snapshot = self.runtime_state.snapshot()
        if self.is_busy() or not self._snapshot_has_stale_task_error(snapshot):
            return
        self._trace(
            "clear_stale_error_state",
            reason=reason,
            previous_task_state=None if snapshot.task_state is None else str(snapshot.task_state),
            previous_orchestrator_state=str(snapshot.orchestrator_state),
            previous_latest_message=snapshot.latest_message,
        )
        self.runtime_state.reset_to_idle("Machine idle.")

    def is_busy(self) -> bool:
        worker = self._active_worker
        return worker is not None and worker.is_alive()

    def build_health_response(self, snapshot: RuntimeStateSnapshot) -> HealthResponse:
        subsystem_health = snapshot.subsystem_health
        return HealthResponse(
            ok=snapshot.backend_lifecycle_state not in {
                BackendLifecycleState.BACKEND_FAILED,
                BackendLifecycleState.PERSISTENT_BACKEND_FAILURE,
                BackendLifecycleState.PROCESS_DOWN,
            },
            backend_lifecycle_state=str(snapshot.backend_lifecycle_state),
            backend_boot_degraded=snapshot.backend_boot_degraded,
            api_alive=True,
            motion_available=bool(subsystem_health.get("motion_available", False)),
            vacuum_available=bool(subsystem_health.get("vacuum_available", False)),
            vibration_available=bool(subsystem_health.get("vibration_available", False)),
            detection_reader_available=bool(subsystem_health.get("detection_reader_available", False)),
            classifier_available=bool(subsystem_health.get("classifier_available", False)),
            subsystem_errors=dict(snapshot.subsystem_errors),
            message=snapshot.latest_message,
        )

    def get_fin6_setup_status(self) -> dict[str, Any]:
        return self.machine_service.get_fin6_setup_status()

    def launch_fin6_setup(self) -> dict[str, Any]:
        with self._worker_lock:
            if self.is_busy():
                self.runtime_state.append_log(
                    "WARNING",
                    f"Rejected fin6 setup launch: machine busy with {self._active_command}.",
                )
                return {
                    "ok": False,
                    "message": f"Machine is busy running {self._active_command}. Wait for it to finish before opening fin6 setup.",
                }
        return self.machine_service.launch_fin6_setup()

    def submit_machine_task(
        self,
        command: str,
        worker: Callable[[], Any],
        precheck: Callable[[], str | None] | None = None,
    ) -> CommandResponse:
        with self._worker_lock:
            if self.is_busy():
                self._trace("submit_machine_task_rejected_busy", command=command)
                self.runtime_state.append_log("WARNING", f"Rejected {command}: machine busy with {self._active_command}.")
                return CommandResponse.from_snapshot(
                    self.runtime_state.snapshot(),
                    ok=False,
                    accepted=False,
                    command=command,
                    message=f"Machine is busy running {self._active_command}.",
                )

            if precheck is not None:
                error_message = precheck()
                if error_message is not None:
                    self._trace("submit_machine_task_rejected_precheck", command=command, error=error_message)
                    self.runtime_state.append_log("WARNING", f"Rejected {command}: {error_message}")
                    return CommandResponse.from_snapshot(
                        self.runtime_state.snapshot(),
                        ok=False,
                        accepted=False,
                        command=command,
                        message=error_message,
                    )

            self.runtime_state.set_stop_requested(False)
            self.runtime_state.clear_task_tracking(f"Accepted {command} request.")
            self.runtime_state.set_orchestrator_state(
                OrchestratorState.TASK_VALIDATING,
                f"Accepted {command} request.",
            )
            self._trace("submit_machine_task_accepted", command=command)

            thread = Thread(
                target=self._run_worker_wrapper,
                name=f"pi-backend-{command}",
                args=(command, worker),
                daemon=True,
            )
            self._active_worker = thread
            self._active_command = command
            thread.start()

        return CommandResponse.from_snapshot(
            self.runtime_state.snapshot(),
            ok=True,
            accepted=True,
            command=command,
            message=f"{command} accepted.",
        )

    def apply_actuator_command(
        self,
        command: str,
        action: Callable[[], Any],
        precheck: Callable[[], str | None] | None = None,
    ) -> CommandResponse:
        with self._worker_lock:
            if self.is_busy():
                self._trace("apply_actuator_rejected_busy", command=command)
                self.runtime_state.append_log("WARNING", f"Rejected {command}: machine busy with {self._active_command}.")
                return CommandResponse.from_snapshot(
                    self.runtime_state.snapshot(),
                    ok=False,
                    accepted=False,
                    command=command,
                    message=f"Cannot change {command} while {self._active_command} is active.",
                )

            if precheck is not None:
                error_message = precheck()
                if error_message is not None:
                    self._trace("apply_actuator_rejected_precheck", command=command, error=error_message)
                    self.runtime_state.append_log("WARNING", f"Rejected {command}: {error_message}")
                    return CommandResponse.from_snapshot(
                        self.runtime_state.snapshot(),
                        ok=False,
                        accepted=False,
                    command=command,
                    message=error_message,
                )

        self._clear_stale_error_state_if_idle(reason=f"actuator:{command}")

        try:
            self._trace("apply_actuator_enter", command=command)
            action()
        except Exception as exc:
            self._trace("apply_actuator_exception", command=command, error=str(exc))
            self.runtime_state.append_log("ERROR", f"{command} failed: {exc}")
            return CommandResponse.from_snapshot(
                self.runtime_state.snapshot(),
                ok=False,
                accepted=False,
                command=command,
                message=f"{command} failed: {exc}",
            )

        return CommandResponse.from_snapshot(
            self.runtime_state.snapshot(),
            ok=True,
            accepted=True,
            command=command,
            message=f"{command} applied.",
        )

    def request_stop(self) -> CommandResponse:
        busy = self.is_busy()
        self._trace("request_stop_enter", busy=busy)
        self.runtime_state.set_stop_requested(True)
        self.machine_service.emergency_stop()

        if busy:
            self.runtime_state.set_orchestrator_state(
                OrchestratorState.STOP_REQUESTED,
                f"Stop requested for {self._active_command}.",
            )
            self.runtime_state.append_log(
                "INFO",
                f"Stop requested while {self._active_command} is active.",
            )
            message = (
                f"Emergency stop requested for {self._active_command}. "
                "Motion drive and outputs were cut immediately and the active task is being cancelled."
            )
        else:
            self.runtime_state.append_log("INFO", "Stop requested while machine is idle.")
            self.runtime_state.reset_to_idle("Machine idle.", clear_stop_requested=True)
            message = "Emergency stop acknowledged. Motion drive and outputs were forced to a safe state."
        self._trace("request_stop_exit", busy=busy, message=message)
        return CommandResponse.from_snapshot(
            self.runtime_state.snapshot(),
            ok=True,
            accepted=True,
            command="stop",
            message=message,
        )

    def shutdown(self) -> None:
        self.runtime_state.set_backend_lifecycle_state(
            BackendLifecycleState.SHUTTING_DOWN,
            "API shutdown in progress.",
        )
        try:
            self._safe_shutdown_actuator("vacuum", lambda: self.machine_service.set_vacuum(False))
            self._safe_shutdown_actuator("vibration", lambda: self.machine_service.set_vibration(False))
        finally:
            self.runtime_state.set_stop_requested(True)

    def _safe_shutdown_actuator(self, name: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception as exc:
            if self._is_expected_shutdown_condition(exc):
                self.logger.warning(
                    "Skipping %s safe-off during shutdown: %s",
                    name,
                    exc,
                )
                return
            self.logger.exception("Failed to set %s safe-off during shutdown.", name)

    @staticmethod
    def _is_expected_shutdown_condition(exc: Exception) -> bool:
        if isinstance(exc, SubsystemUnavailableError):
            return True

        message = str(exc).strip().lower()
        busy_tokens = (
            "busy",
            "in use",
            "already in use",
            "gpio busy",
            "resource busy",
            "device or resource busy",
            "cannot determine soc peripheral base address",
        )
        return any(token in message for token in busy_tokens)

    def _run_worker_wrapper(self, command: str, worker: Callable[[], Any]) -> None:
        try:
            self._trace("worker_wrapper_enter", command=command)
            worker()
        except Exception as exc:
            self._trace("worker_wrapper_exception", command=command, error=str(exc))
            self._finalize_failed_worker_state(command, exc)
            self.logger.exception("Background command %s failed.", command)
            self.runtime_state.append_log("ERROR", f"Background command {command} failed.")
        finally:
            with self._worker_lock:
                self._active_worker = None
                self._active_command = None
            snapshot = self.runtime_state.snapshot()
            if snapshot.stop_requested:
                self._trace(
                    "worker_wrapper_reset_after_stop",
                    command=command,
                    final_task_state=None if snapshot.task_state is None else str(snapshot.task_state),
                    final_orchestrator_state=str(snapshot.orchestrator_state),
                )
                self.runtime_state.reset_to_idle("Machine idle.", clear_stop_requested=True)
            else:
                self.runtime_state.set_stop_requested(False)
            self._trace("worker_wrapper_exit", command=command)


def create_app() -> FastAPI:
    runtime_config = build_backend_runtime_config()
    runtime_state = RuntimeStateStore(recent_log_limit=runtime_config.recent_log_limit)
    machine_service = MachineService(runtime_state=runtime_state, runtime_config=runtime_config)
    backend_context = BackendApiContext(
        runtime_config=runtime_config,
        runtime_state=runtime_state,
        machine_service=machine_service,
    )

    app = FastAPI(title="Drosophila Pi Backend", version="0.2.0")
    app.state.backend_context = backend_context
    app.include_router(router)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        backend_context.shutdown()

    return app


app = create_app()
