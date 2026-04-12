from __future__ import annotations

import importlib
import sys

from shared.config.machine_paths import ensure_code_directory_on_path


class SubsystemUnavailableError(RuntimeError):
    def __init__(self, subsystem: str, detail: str):
        self.subsystem = subsystem
        self.detail = detail
        super().__init__(f"{subsystem} subsystem unavailable: {detail}")


def format_exception_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def import_legacy_module(module_name: str):
    ensure_code_directory_on_path()
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)
