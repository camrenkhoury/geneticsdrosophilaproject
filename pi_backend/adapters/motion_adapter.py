from __future__ import annotations

from types import ModuleType

from pi_backend.core.subsystem_support import (
    SubsystemUnavailableError,
    format_exception_message,
    import_legacy_module,
)


class MotionAdapter:
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
            module = import_legacy_module("motion")
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
    def initialized(self) -> bool:
        return self._initialized

    @property
    def deferred(self) -> bool:
        return not self._initialized

    @property
    def available(self) -> bool:
        return self._available

    @property
    def simulation_enabled(self) -> bool:
        return self._simulation_enabled

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def status(self) -> str:
        if self.deferred:
            return "deferred"
        if not self.available:
            return "unavailable"
        if self.simulation_enabled:
            return "simulation"
        return "available"

    def _require_module(self) -> ModuleType:
        self.initialize()
        if self._module is None:
            raise SubsystemUnavailableError(
                "motion",
                self._last_error or "legacy motion module failed to initialize.",
            )
        return self._module

    def home_to_zero(self) -> None:
        self._require_module().home_to_zero()

    def move_absolute(self, position_mm: float, move_time: float | None = None) -> None:
        self._require_module().move_to_absolute(position_mm, move_time)

    def move_relative(self, delta_mm: float, move_time: float | None = None) -> None:
        self._require_module().move_relative(delta_mm, move_time)

    def get_current_position(self) -> float:
        return float(self._require_module().get_current_position())

    def get_operational_max_mm(self) -> float:
        return float(self._require_module().get_operational_max_mm())
