from __future__ import annotations

from shared.config.machine_paths import ensure_code_directory_on_path

ensure_code_directory_on_path()

import vacuum  # type: ignore  # noqa: E402


class VacuumAdapter:
    @property
    def simulation_enabled(self) -> bool:
        return not bool(vacuum.GPIO_AVAILABLE)

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            vacuum.vacuum_on()
            return
        vacuum.vacuum_off()
