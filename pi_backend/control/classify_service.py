from __future__ import annotations

import logging
import time
from typing import Callable

from pi_backend.core.subsystem_support import (
    SubsystemUnavailableError,
    format_exception_message,
    import_legacy_module,
)
from pi_backend.core.runtime_state import ClassifierResultSummary, RuntimeStateStore
from shared.state.state_enums import OrchestratorState, TaskState


class ClassifyService:
    def __init__(self, runtime_state: RuntimeStateStore, logger: logging.Logger):
        self.runtime_state = runtime_state
        self.logger = logger
        self._classify_callable: Callable[[], dict[str, object]] | None = None
        self._initialized = False
        self._available = False
        self._last_error: str | None = None
        self._last_initialize_attempt_s = 0.0

    def initialize(self) -> None:
        if self._initialized and self._available:
            return
        now_s = time.monotonic()
        if self._initialized and now_s - self._last_initialize_attempt_s < 5.0:
            return

        self._last_initialize_attempt_s = now_s
        self._initialized = True
        try:
            module = import_legacy_module("fly_classifier1")
            self._classify_callable = getattr(module, "classify_fly")
        except Exception as exc:
            self._classify_callable = None
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

    def _require_callable(self) -> Callable[[], dict[str, object]]:
        self.initialize()
        if self._classify_callable is None:
            raise SubsystemUnavailableError(
                "classifier",
                self._last_error or "legacy classifier module failed to initialize.",
            )
        return self._classify_callable

    def run(self) -> dict[str, object]:
        classify_callable = self._require_callable()
        self.runtime_state.begin_task("classify", TaskState.CLASSIFY_RUNNING, "Running classify workflow.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting classify task.")
        self.logger.info("Classification started.")

        try:
            result = classify_callable()
        except Exception:
            self.runtime_state.fail_task(TaskState.CLASSIFY_ERROR, "Classification failed.")
            self.logger.exception("Classification failed.")
            raise

        summary = ClassifierResultSummary(
            result_class=str(result.get("class", "UNCERTAIN")),
            confidence=float(result.get("confidence", 0.0)),
            errors=[str(error) for error in result.get("errors", [])],
            raw=dict(result),
        )
        self.runtime_state.set_classifier_result(summary)
        self.runtime_state.complete_task(TaskState.CLASSIFY_COMPLETE, "Classification complete.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.SYSTEM_IDLE, "Machine idle.")
        self.logger.info("Classification completed with class=%s confidence=%.4f", summary.result_class, summary.confidence)
        return result
