#!/usr/bin/env python3
"""
Isolated vibration motor control for Raspberry Pi GPIO usage.

The assay workflow should not be tightly coupled to GPIO APIs, so this module
keeps hardware access behind a tiny abstraction layer with clear errors.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


class MotorError(RuntimeError):
    """Raised when the configured motor cannot be pulsed."""


@dataclass
class MotorSettings:
    enabled: bool = False
    gpio_pin: int = 18
    pulse_ms: int = 250
    settle_delay_ms: int = 500
    active_high: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MotorSettings":
        payload = dict(data or {})
        return cls(
            enabled=bool(payload.get("enabled", False)),
            gpio_pin=int(payload.get("gpio_pin", 18)),
            pulse_ms=int(payload.get("pulse_ms", payload.get("pulse_duration_ms", 250))),
            settle_delay_ms=int(payload.get("settle_delay_ms", payload.get("settle_ms", 500))),
            active_high=bool(payload.get("active_high", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _NullMotorBackend:
    def __init__(self, settings: MotorSettings):
        self.settings = settings

    def pulse(self) -> None:
        if self.settings.enabled:
            raise MotorError(
                "Motor control is enabled, but no Raspberry Pi GPIO backend is available. "
                "Install RPi.GPIO or gpiozero on the Pi."
            )

    def close(self) -> None:
        return


class _RPiGPIOBackend:
    def __init__(self, settings: MotorSettings):
        import RPi.GPIO as GPIO  # type: ignore

        self.GPIO = GPIO
        self.settings = settings
        self.GPIO.setwarnings(False)
        self.GPIO.setmode(GPIO.BCM)
        self.GPIO.setup(int(settings.gpio_pin), GPIO.OUT)
        idle_value = GPIO.LOW if settings.active_high else GPIO.HIGH
        self.GPIO.output(int(settings.gpio_pin), idle_value)

    def pulse(self) -> None:
        active_value = self.GPIO.HIGH if self.settings.active_high else self.GPIO.LOW
        idle_value = self.GPIO.LOW if self.settings.active_high else self.GPIO.HIGH
        self.GPIO.output(int(self.settings.gpio_pin), active_value)
        time.sleep(max(0.0, float(self.settings.pulse_ms) / 1000.0))
        self.GPIO.output(int(self.settings.gpio_pin), idle_value)

    def close(self) -> None:
        try:
            self.GPIO.cleanup(int(self.settings.gpio_pin))
        except Exception:
            pass


class _GpioZeroBackend:
    def __init__(self, settings: MotorSettings):
        from gpiozero import OutputDevice  # type: ignore

        self.settings = settings
        self.device = OutputDevice(int(settings.gpio_pin), active_high=bool(settings.active_high), initial_value=False)

    def pulse(self) -> None:
        self.device.on()
        time.sleep(max(0.0, float(self.settings.pulse_ms) / 1000.0))
        self.device.off()

    def close(self) -> None:
        try:
            self.device.close()
        except Exception:
            pass


class VibrationMotor:
    def __init__(self, settings: Optional[MotorSettings | Dict[str, Any]] = None):
        if settings is None:
            settings = MotorSettings()
        if not isinstance(settings, MotorSettings):
            settings = MotorSettings.from_dict(dict(settings))
        self.settings = settings
        self._backend = self._build_backend(settings)

    def _build_backend(self, settings: MotorSettings):
        try:
            return _RPiGPIOBackend(settings)
        except Exception:
            pass
        try:
            return _GpioZeroBackend(settings)
        except Exception:
            return _NullMotorBackend(settings)

    def pulse(self) -> None:
        if not self.settings.enabled:
            return
        self._backend.pulse()
        settle_s = max(0.0, float(self.settings.settle_delay_ms) / 1000.0)
        if settle_s > 0:
            time.sleep(settle_s)

    def test(self) -> None:
        if not self.settings.enabled:
            raise MotorError("The vibration motor is disabled in the current profile.")
        self._backend.pulse()

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> "VibrationMotor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def pulse_vibration_motor(settings: Optional[MotorSettings | Dict[str, Any]]) -> None:
    motor = VibrationMotor(settings)
    try:
        motor.pulse()
    finally:
        motor.close()
