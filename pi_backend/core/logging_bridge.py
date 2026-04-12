from __future__ import annotations

import logging

from pi_backend.core.runtime_state import RuntimeStateStore


class RuntimeStateLogHandler(logging.Handler):
    """Logging handler that mirrors backend logs into the runtime state store."""

    def __init__(self, runtime_state: RuntimeStateStore):
        super().__init__()
        self.runtime_state = runtime_state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.runtime_state.append_log(record.levelname, message)
        except Exception:
            self.handleError(record)


def attach_runtime_log_handler(
    logger: logging.Logger,
    runtime_state: RuntimeStateStore,
    level: int = logging.INFO,
) -> logging.Logger:
    logger.setLevel(level)

    if not any(isinstance(handler, RuntimeStateLogHandler) for handler in logger.handlers):
        handler = RuntimeStateLogHandler(runtime_state)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)

    return logger
