from __future__ import annotations

import logging
from threading import Lock, Thread
from typing import Any, Callable

from fastapi import FastAPI

from pi_backend.api.models import CommandResponse, HealthResponse
from pi_backend.api.routes import router
from pi_backend.core.config_runtime import BackendRuntimeConfig, build_backend_runtime_config
from pi_backend.core.runtime_state import RuntimeStateSnapshot, RuntimeStateStore
from pi_backend.control.machine_service import MachineService
from shared.state.state_enums import BackendLifecycleState, OrchestratorState


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

    def submit_machine_task(
        self,
        command: str,
        worker: Callable[[], Any],
        precheck: Callable[[], str | None] | None = None,
    ) -> CommandResponse:
        with self._worker_lock:
            if self.is_busy():
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
                    self.runtime_state.append_log("WARNING", f"Rejected {command}: {error_message}")
                    return CommandResponse.from_snapshot(
                        self.runtime_state.snapshot(),
                        ok=False,
                        accepted=False,
                        command=command,
                        message=error_message,
                    )

            self.runtime_state.set_stop_requested(False)
            self.runtime_state.set_orchestrator_state(
                OrchestratorState.TASK_VALIDATING,
                f"Accepted {command} request.",
            )

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
                    self.runtime_state.append_log("WARNING", f"Rejected {command}: {error_message}")
                    return CommandResponse.from_snapshot(
                        self.runtime_state.snapshot(),
                        ok=False,
                        accepted=False,
                        command=command,
                        message=error_message,
                    )

        try:
            action()
        except Exception as exc:
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
        self.runtime_state.set_stop_requested(True)

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
                f"Stop requested for {self._active_command}. "
                "Phase 2 stop is cooperative and will be fully enforced once task orchestration is added."
            )
        else:
            self.runtime_state.append_log("INFO", "Stop requested while machine is idle.")
            message = "Stop acknowledged. Machine is already idle."

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
            try:
                self.machine_service.set_vacuum(False)
            except Exception:
                self.logger.exception("Failed to set vacuum safe-off during shutdown.")
            try:
                self.machine_service.set_vibration(False)
            except Exception:
                self.logger.exception("Failed to set vibration safe-off during shutdown.")
        finally:
            self.runtime_state.set_stop_requested(True)

    def _run_worker_wrapper(self, command: str, worker: Callable[[], Any]) -> None:
        try:
            worker()
        except Exception:
            self.logger.exception("Background command %s failed.", command)
            self.runtime_state.append_log("ERROR", f"Background command {command} failed.")
        finally:
            self.runtime_state.set_stop_requested(False)
            with self._worker_lock:
                self._active_worker = None
                self._active_command = None


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
