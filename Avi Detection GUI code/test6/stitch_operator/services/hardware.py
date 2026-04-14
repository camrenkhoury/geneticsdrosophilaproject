from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List

from ..bootstrap import ensure_repo_paths

ensure_repo_paths()
import config  # noqa: E402
import motion  # noqa: E402
import vacuum  # noqa: E402
import vibration  # noqa: E402


class HardwareError(RuntimeError):
    pass


@dataclass
class Destination:
    label: str
    position_mm: float


class HardwareService:
    def __init__(self):
        self.available = bool(getattr(motion, "GPIO_AVAILABLE", False))
        self._homed = bool(getattr(motion, "is_homed", lambda: False)())

    @staticmethod
    def clamp(position_mm: float) -> float:
        return float(motion.clamp_operational(position_mm))

    def home(self) -> float:
        motion.home_to_zero()
        self._homed = True
        return self.position_mm

    def reset_outputs(self) -> None:
        vacuum.vacuum_off()
        vibration.vibration_off()

    @property
    def position_mm(self) -> float:
        return float(motion.get_current_position())

    @property
    def homed(self) -> bool:
        self._homed = bool(getattr(motion, "is_homed", lambda: self._homed)())
        return self._homed

    def limit_states(self):
        return dict(getattr(motion, "get_limit_states", lambda: {"min": False, "max": False})())

    def move_absolute(self, position_mm: float, settle_s: float = 0.25) -> float:
        motion.move_to_absolute(self.clamp(position_mm))
        if settle_s > 0:
            time.sleep(float(settle_s))
        return self.position_mm

    def move_relative(self, delta_mm: float, settle_s: float = 0.25) -> float:
        motion.move_relative(float(delta_mm))
        if settle_s > 0:
            time.sleep(float(settle_s))
        return self.position_mm

    def move_to_channel_camera(self) -> float:
        return self.move_absolute(float(config.CHANNEL_CAMERA_POSITION_MM))

    def park_for_channel_capture(self, *, vacuum_release_settle_s: float = 0.0) -> float:
        self.vacuum_off()
        if float(vacuum_release_settle_s) > 0.0:
            time.sleep(float(vacuum_release_settle_s))
        return self.move_to_channel_camera()

    def move_to_chamber(self) -> float:
        return self.move_absolute(float(config.CHAMBER_CENTER))

    def move_to_destination(self, destination: Destination | float, settle_s: float = 0.25) -> float:
        if isinstance(destination, Destination):
            target = float(destination.position_mm)
        else:
            target = float(destination)
        return self.move_absolute(target, settle_s=settle_s)

    def chamber_clear_position(self) -> float:
        return self.clamp(float(config.CHAMBER_CENTER) + float(config.CHAMBER_REPOSITION_OFFSET_MM))

    def pickup_position_from_channel(self, positions_mm: Iterable[float], pickup_offset_mm: float | None = None) -> float:
        values: List[float] = [float(v) for v in positions_mm]
        if not values:
            raise HardwareError("No pickup positions were supplied.")
        offset = float(config.CHANNEL_PICKUP_OFFSET_MM if pickup_offset_mm is None else pickup_offset_mm)
        return self.clamp(max(values) + offset)

    def vacuum_on(self, power: float = 1.0) -> None:
        vacuum.vacuum_on(power=power)

    def vacuum_off(self) -> None:
        vacuum.vacuum_off()

    def is_vacuum_on(self) -> bool:
        return bool(getattr(vacuum, "is_vacuum_on", lambda: False)())

    def vibration_pulse(self, duration_s: float | None = None) -> None:
        vibration.pulse(duration_s=duration_s if duration_s is not None else config.VIBRATION_TIME)

    def stop(self) -> None:
        self.reset_outputs()

    # ------------------------------------------------------------------
    # Guided loading sequence helpers
    # ------------------------------------------------------------------
    def move_to_pickup(
        self,
        pickup_position_mm: float,
        *,
        vacuum_pick_delay_s: float,
        vacuum_release_settle_s: float = 0.0,
    ) -> float:
        self.vacuum_off()
        if float(vacuum_release_settle_s) > 0.0:
            time.sleep(float(vacuum_release_settle_s))
        self.move_to_channel_camera()
        self.move_absolute(pickup_position_mm)
        self.vacuum_on()
        time.sleep(max(0.0, float(vacuum_pick_delay_s)))
        return self.position_mm

    def drop_in_chamber_and_clear(
        self,
        *,
        vacuum_drop_delay_s: float,
        vacuum_release_settle_s: float = 0.0,
    ) -> float:
        self.move_to_chamber()
        self.vacuum_off()
        time.sleep(max(0.0, float(vacuum_drop_delay_s)))
        if float(vacuum_release_settle_s) > 0.0:
            time.sleep(float(vacuum_release_settle_s))
        return self.move_absolute(self.chamber_clear_position())

    def reacquire_from_chamber(
        self,
        *,
        vacuum_pick_delay_s: float,
        vacuum_release_settle_s: float = 0.0,
    ) -> float:
        self.vacuum_off()
        if float(vacuum_release_settle_s) > 0.0:
            time.sleep(float(vacuum_release_settle_s))
        self.move_to_chamber()
        self.vacuum_on()
        time.sleep(max(0.0, float(vacuum_pick_delay_s)))
        return self.position_mm

    def drop_into_vial(
        self,
        destination_mm: float,
        *,
        vacuum_drop_delay_s: float,
        vacuum_release_settle_s: float = 0.0,
    ) -> float:
        self.move_absolute(destination_mm)
        self.vacuum_off()
        time.sleep(max(0.0, float(vacuum_drop_delay_s)))
        if float(vacuum_release_settle_s) > 0.0:
            time.sleep(float(vacuum_release_settle_s))
        return self.position_mm

    def snapshot(self) -> dict:
        return {
            "position_mm": self.position_mm,
            "homed": self.homed,
            "vacuum_on": self.is_vacuum_on(),
            "limits": self.limit_states(),
            "gpio_available": self.available,
        }
