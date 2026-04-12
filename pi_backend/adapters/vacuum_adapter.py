from __future__ import annotations

from types import ModuleType

from pi_backend.core.subsystem_support import (
    SubsystemUnavailableError,
    format_exception_message,
    import_legacy_module,
)


class VacuumAdapter:
    def __init__(self):
        self._module: ModuleType | None = None
        self._initialized = False
        self._available = False
        self._simulation_enabled = False
        self._last_error: str | None = None

    def initialize(self) -> None:
        if self._initialized:
            return

        self._initialized = True
        try:
            module = import_legacy_module("vacuum")
        except Exception as exc:
            self._module = None
            self._available = False
            self._simulation_enabled = False
            self._last_error = format_exception_message(exc)
            return

        self._module = module
        self._available = True
        self._simulation_enabled = not bool(getattr(module, "GPIO_AVAILABLE", False))
        self._last_error = None

    @property
    def available(self) -> bool:
        self.initialize()
        return self._available

    @property
    def simulation_enabled(self) -> bool:
        self.initialize()
        return self._simulation_enabled

    @property
    def last_error(self) -> str | None:
        self.initialize()
        return self._last_error

    @property
    def status(self) -> str:
        if not self.available:
            return "unavailable"
        if self.simulation_enabled:
            return "simulation"
        return "available"

    def _require_module(self) -> ModuleType:
        self.initialize()
        if self._module is None:
            raise SubsystemUnavailableError(
                "vacuum",
                self._last_error or "legacy vacuum module failed to initialize.",
            )
        return self._module

    def set_enabled(self, enabled: bool) -> None:
        module = self._require_module()
        if enabled:
            module.vacuum_on()
            return
        module.vacuum_off()
