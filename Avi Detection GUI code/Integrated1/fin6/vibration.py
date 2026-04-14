#!/usr/bin/env python3
"""Legacy assay vibration backend.

This preserves the old working behaviour used by the prior rig-control code:
- PWM output on BCM pin 12
- direction pin on BCM 24 held active

The module exposes ``vibration_on`` / ``vibration_off`` so the assay workflow's
``motor_control`` module can reuse it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


_GPIO_ERROR: Optional[Exception] = None


class _MockPWMOutputDevice:
    def __init__(self, pin: int, frequency: int = 1000):
        self.pin = int(pin)
        self.frequency = int(frequency)
        self.value = 0.0

    def close(self) -> None:
        return


class _MockOutputDevice:
    def __init__(self, pin: int):
        self.pin = int(pin)
        self.state = False

    def on(self) -> None:
        self.state = True

    def off(self) -> None:
        self.state = False

    def close(self) -> None:
        return


try:  # pragma: no cover - exercised indirectly on Pi hardware
    from gpiozero import OutputDevice, PWMOutputDevice  # type: ignore
except Exception as exc:  # pragma: no cover - safe fallback for non-Pi CI
    _GPIO_ERROR = exc
    PWMOutputDevice = _MockPWMOutputDevice  # type: ignore[assignment]
    OutputDevice = _MockOutputDevice  # type: ignore[assignment]


@dataclass
class _LegacyVibrationBackend:
    pwm_pin: int = 12
    dir_pin: int = 24
    frequency_hz: int = 1000
    duty_cycle: float = 1.0

    def __post_init__(self) -> None:
        self.motor_pwm = PWMOutputDevice(int(self.pwm_pin), frequency=int(self.frequency_hz))
        self.motor_dir = OutputDevice(int(self.dir_pin))
        self.motor_dir.on()
        try:
            self.motor_pwm.value = 0.0
        except Exception:
            pass

    @property
    def backend_name(self) -> str:
        return "legacy-pwm"

    def on(self) -> None:
        try:
            self.motor_dir.on()
        except Exception:
            pass
        self.motor_pwm.value = float(self.duty_cycle)

    def off(self) -> None:
        try:
            self.motor_pwm.value = 0.0
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.off()
        finally:
            for device in (self.motor_pwm, self.motor_dir):
                try:
                    device.close()
                except Exception:
                    pass


_BACKEND: Optional[_LegacyVibrationBackend] = None


def _get_backend() -> _LegacyVibrationBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _LegacyVibrationBackend()
    return _BACKEND


def vibration_on() -> None:
    _get_backend().on()


def vibration_off() -> None:
    _get_backend().off()


def close_vibration() -> None:
    global _BACKEND
    if _BACKEND is None:
        return
    try:
        _BACKEND.close()
    finally:
        _BACKEND = None


if __name__ == "__main__":  # pragma: no cover - manual diagnostic entrypoint
    try:
        while True:
            cmd = input("s=ON, x=OFF, q=quit: ").strip().lower()
            if cmd == "s":
                vibration_on()
                print("Vibration Motor ON (legacy PWM backend)")
            elif cmd == "x":
                vibration_off()
                print("Vibration Motor OFF")
            elif cmd == "q":
                break
            else:
                print("Invalid command")
    finally:
        close_vibration()
