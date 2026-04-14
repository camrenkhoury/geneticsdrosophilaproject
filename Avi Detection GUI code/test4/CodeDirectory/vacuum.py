"""Vacuum motor helpers with simulation-safe fallbacks."""

from __future__ import annotations

import config

try:
    from gpiozero import OutputDevice, PWMOutputDevice

    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

    class MockPWMOutputDevice:
        def __init__(self, pin, frequency=1000, *args, **kwargs):
            self.pin = pin
            self.frequency = frequency
            self.value = 0.0

    class MockOutputDevice:
        def __init__(self, pin, *args, **kwargs):
            self.pin = pin
            self.state = False

        def on(self):
            self.state = True

        def off(self):
            self.state = False

    PWMOutputDevice = MockPWMOutputDevice
    OutputDevice = MockOutputDevice


motor_pwm = PWMOutputDevice(config.VAC_PWM, frequency=1000)
motor_dir = OutputDevice(config.VAC_DIR)
motor_dir.on()
_is_on = False


def vacuum_on(power: float = 1.0) -> None:
    global _is_on
    level = max(0.0, min(1.0, float(power)))
    motor_pwm.value = level
    _is_on = level > 0.0
    print(f"Vacuum ON ({level:.2f})")


def vacuum_off() -> None:
    global _is_on
    motor_pwm.value = 0.0
    _is_on = False
    print("Vacuum OFF")


def is_vacuum_on() -> bool:
    return bool(_is_on)
