from __future__ import annotations

from shared.config.machine_paths import ensure_code_directory_on_path

ensure_code_directory_on_path()

import motion  # type: ignore  # noqa: E402


class MotionAdapter:
    @property
    def simulation_enabled(self) -> bool:
        return not bool(motion.GPIO_AVAILABLE)

    def home_to_zero(self) -> None:
        motion.home_to_zero()

    def move_absolute(self, position_mm: float, move_time: float | None = None) -> None:
        motion.move_to_absolute(position_mm, move_time)

    def move_relative(self, delta_mm: float, move_time: float | None = None) -> None:
        motion.move_relative(delta_mm, move_time)

    def get_current_position(self) -> float:
        return float(motion.get_current_position())

    def get_operational_max_mm(self) -> float:
        return float(motion.get_operational_max_mm())
