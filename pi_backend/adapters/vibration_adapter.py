from __future__ import annotations

from shared.config.machine_paths import ensure_code_directory_on_path

ensure_code_directory_on_path()

import vibration  # type: ignore  # noqa: E402


class VibrationAdapter:
    @property
    def simulation_enabled(self) -> bool:
        return not bool(vibration.GPIO_AVAILABLE)

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            vibration.vibration_on()
            return
        vibration.vibration_off()
