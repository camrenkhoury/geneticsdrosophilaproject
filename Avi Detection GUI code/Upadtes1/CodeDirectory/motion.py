"""Low-level gantry motion helpers.

The original repository expects a very small functional API from this module.
This version preserves that API while adding a couple of helpers that the new
operator application can query for richer status.
"""

from __future__ import annotations

from time import sleep
from typing import Dict

import config

try:
    from gpiozero import DigitalInputDevice, OutputDevice

    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

    class MockOutputDevice:
        def __init__(self, pin, *args, **kwargs):
            self.pin = pin
            self.state = False

        def on(self):
            self.state = True

        def off(self):
            self.state = False

    class MockDigitalInputDevice:
        def __init__(self, pin, pull_up=True, *args, **kwargs):
            self.pin = pin
            self.pull_up = pull_up
            self.state = False

        @property
        def value(self):
            return bool(self.state)

    OutputDevice = MockOutputDevice
    DigitalInputDevice = MockDigitalInputDevice


STEP = OutputDevice(config.STEP_PIN)
DIR = OutputDevice(config.DIR_PIN)
EN = OutputDevice(config.EN_PIN)

Limit_Min = DigitalInputDevice(config.LIMIT_MIN_PIN, pull_up=bool(config.LIMIT_SWITCH_PULLUP))
Limit_Max = DigitalInputDevice(config.LIMIT_MAX_PIN, pull_up=bool(config.LIMIT_SWITCH_PULLUP))

current_position_mm = 0.0
_is_homed = False


def enable_motor() -> None:
    EN.off()
    sleep(0.01)


def disable_motor() -> None:
    EN.on()
    sleep(0.01)


def set_direction(forward: bool) -> None:
    if forward:
        DIR.on()
    else:
        DIR.off()
    sleep(0.005)


def step_once(step_delay: float) -> None:
    STEP.on()
    sleep(step_delay)
    STEP.off()
    sleep(step_delay)


def get_operational_min_mm() -> float:
    return float(config.SOFTWARE_POSITION_MIN_MM)


def get_operational_max_mm() -> float:
    return float(config.SOFTWARE_POSITION_MAX_MM)


def clamp_operational(position_mm: float) -> float:
    return max(get_operational_min_mm(), min(get_operational_max_mm(), float(position_mm)))


def get_limit_states() -> Dict[str, bool]:
    return {"min": bool(Limit_Min.value), "max": bool(Limit_Max.value)}


def is_homed() -> bool:
    return bool(_is_homed)


def mark_unhomed() -> None:
    global _is_homed
    _is_homed = False


def move_relative(distance_mm, move_time=None):
    global current_position_mm, _is_homed

    distance_mm = float(distance_mm)
    if distance_mm == 0.0:
        return

    target_position = clamp_operational(current_position_mm + distance_mm)
    actual_distance = target_position - current_position_mm
    if actual_distance == 0.0:
        print("Move blocked by operational limits.")
        return

    if not GPIO_AVAILABLE:
        sleep(abs(actual_distance) * 0.01)
        current_position_mm = target_position
        return

    forward = actual_distance > 0.0
    total_steps = int(abs(actual_distance) / config.MM_PER_STEP)
    if total_steps <= 0:
        print("Move too small.")
        return

    if move_time is not None and float(move_time) > 0.0:
        ideal_delay = float(move_time) / (2.0 * total_steps)
        step_delay = ideal_delay * config.TIMING_FACTOR
    else:
        step_delay = float(config.DEFAULT_STEP_DELAY)

    enable_motor()
    set_direction(forward)
    final_position = current_position_mm

    try:
        for _ in range(total_steps):
            if (not forward) and bool(Limit_Min.value):
                print("Minimum physical limit reached.")
                final_position = get_operational_min_mm()
                break
            if forward and bool(Limit_Max.value):
                print("Maximum physical limit reached.")
                final_position = get_operational_max_mm()
                break
            step_once(step_delay)
        else:
            final_position = target_position

        current_position_mm = clamp_operational(final_position)
    finally:
        disable_motor()



def move_to_absolute(target_mm, move_time=None):
    target_mm = clamp_operational(float(target_mm))
    distance = target_mm - current_position_mm
    move_relative(distance, move_time)


def home_to_zero(max_steps=50000):
    global current_position_mm, _is_homed

    print("Homing to zero...")
    if not GPIO_AVAILABLE:
        sleep(1.0)
        current_position_mm = 0.0
        _is_homed = True
        print("Homed (simulated). Usable position set to 0.0 mm.")
        return

    enable_motor()
    set_direction(False)
    steps_taken = 0

    try:
        while not bool(Limit_Min.value):
            step_once(float(config.HOME_STEP_DELAY))
            steps_taken += 1
            if steps_taken >= int(max_steps):
                print("Homing aborted: max steps reached.")
                break

        if bool(Limit_Min.value):
            current_position_mm = 0.0
            _is_homed = True
            print("Homed. Usable position set to 0.0 mm.")
        else:
            _is_homed = False
            print("Home switch not reached. Position not trusted.")
    finally:
        disable_motor()


def get_current_position():
    return float(current_position_mm)


def print_position() -> None:
    print(f"Current usable vacuum position: {current_position_mm:.2f} mm")


def get_hardware_controller():
    return {
        "gpio_available": bool(GPIO_AVAILABLE),
        "position_mm": get_current_position(),
        "homed": is_homed(),
        "limits": get_limit_states(),
    }
