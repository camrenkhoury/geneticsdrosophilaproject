from __future__ import annotations

import logging
from typing import Callable

from pi_backend.adapters.vibration_adapter import VibrationAdapter
from pi_backend.core.subsystem_support import (
    SubsystemUnavailableError,
    format_exception_message,
    import_legacy_module,
)
from pi_backend.core.runtime_state import RuntimeStateStore
from shared.state.state_enums import OrchestratorState, TaskState


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
        self._assay_callable: Callable[[], None] | None = None
        self._initialized = False
        self._available = False
        self._last_error: str | None = None

    def initialize(self) -> None:
        if self._initialized:
            return

        self._initialized = True
        try:
            module = import_legacy_module("assay")
            self._assay_callable = getattr(module, "assay")
        except Exception as exc:
            self._assay_callable = None
            self._available = False
            self._last_error = format_exception_message(exc)
            return

        self._available = True
        self._last_error = None

    @property
    def available(self) -> bool:
        self.initialize()
        return self._available

    @property
    def last_error(self) -> str | None:
        self.initialize()
        return self._last_error

    def _require_callable(self) -> Callable[[], None]:
        self.initialize()
        if self._assay_callable is None:
            raise SubsystemUnavailableError(
                "assay",
                self._last_error or "legacy assay module failed to initialize.",
            )
        return self._assay_callable

    def run(self) -> None:
        assay_callable = self._require_callable()
        self.runtime_state.begin_task("assay", TaskState.ASSAY_RUNNING, "Running assay workflow.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting assay task.")
        self.runtime_state.set_vibration_on(True)
        self.logger.info("Assay started.")

        try:
            assay_callable()
        except Exception:
            self.runtime_state.set_vibration_on(False)
            self.runtime_state.fail_task(TaskState.ASSAY_ERROR, "Assay failed.")
            self.logger.exception("Assay failed.")
            raise

        self.runtime_state.set_vibration_on(False)
        self.runtime_state.complete_task(TaskState.ASSAY_COMPLETE, "Assay completed.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Assay completed.")
