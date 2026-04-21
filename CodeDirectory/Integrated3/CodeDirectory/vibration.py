"""Vibration motor helpers with simulation-safe fallbacks."""

from __future__ import annotations

import time

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


motor_pwm = PWMOutputDevice(config.VIB_PWM, frequency=1000)
motor_dir = OutputDevice(config.VIB_DIR)
motor_dir.on()
_is_on = False


def vibration_on(power: float = 1.0) -> None:
    global _is_on
    level = max(0.0, min(1.0, float(power)))
    motor_pwm.value = level
    _is_on = level > 0.0
    print(f"Vibration Motor ON ({level:.2f})")


def vibration_off() -> None:
    global _is_on
    motor_pwm.value = 0.0
    _is_on = False
    print("Vibration Motor OFF")


def pulse(duration_s: float | None = None, power: float = 1.0) -> None:
    vibration_on(power=power)
    time.sleep(max(0.0, float(duration_s if duration_s is not None else config.VIBRATION_TIME)))
    vibration_off()


def is_vibration_on() -> bool:
    return bool(_is_on)
