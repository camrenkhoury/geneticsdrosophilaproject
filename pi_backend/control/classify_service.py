from __future__ import annotations

import logging

from pi_backend.core.runtime_state import ClassifierResultSummary, RuntimeStateStore
from shared.config.machine_paths import ensure_code_directory_on_path
from shared.state.state_enums import OrchestratorState, TaskState

ensure_code_directory_on_path()

from fly_classifier import classify_fly  # type: ignore  # noqa: E402


class ClassifyService:
    def __init__(self, runtime_state: RuntimeStateStore, logger: logging.Logger):
        self.runtime_state = runtime_state
        self.logger = logger

    def run(self) -> dict[str, object]:
        self.runtime_state.begin_task("classify", TaskState.CLASSIFY_RUNNING, "Running classify workflow.")
        self.runtime_state.set_orchestrator_state(OrchestratorState.TASK_STARTING, "Starting classify task.")
        self.logger.info("Classification started.")

        try:
            result = classify_fly()
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
