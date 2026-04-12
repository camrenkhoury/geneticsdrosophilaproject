from __future__ import annotations

import logging

from pi_backend.adapters.vibration_adapter import VibrationAdapter
from pi_backend.core.runtime_state import RuntimeStateStore
from shared.config.machine_paths import ensure_code_directory_on_path
from shared.state.state_enums import OrchestratorState, TaskState

ensure_code_directory_on_path()

from assay import assay  # type: ignore  # noqa: E402


class AssayService:
    def __init__(
        self,
        runtime_state: RuntimeStateStore,
        vibration_adapter: VibrationAdapter,
        logger: logging.Logger,
    ):
        self.runtime_state = runtime_state
        self.vibration_adapter = vibration_adapter
        self.logger = logger

    def run(self) -> None:
        self.runtime_state.begin_task("assay", TaskState.ASSAY_RUNNING, "Running assay workflow.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting assay task.")
        self.runtime_state.set_vibration_on(True)
        self.logger.info("Assay started.")

        try:
            assay()
        except Exception:
            self.runtime_state.set_vibration_on(False)
            self.runtime_state.fail_task(TaskState.ASSAY_ERROR, "Assay failed.")
            self.logger.exception("Assay failed.")
            raise

        self.runtime_state.set_vibration_on(False)
        self.runtime_state.complete_task(TaskState.ASSAY_COMPLETE, "Assay completed.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Assay completed.")
