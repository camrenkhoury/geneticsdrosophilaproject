#!/usr/bin/env python3
"""
Isolated vibration motor control for Raspberry Pi GPIO usage.

The assay workflow should not be tightly coupled to GPIO APIs, so this module
keeps hardware access behind a tiny abstraction layer with clear errors.

The repository already contains a dedicated vibration module outside fin6 on
some rigs. This file now auto-detects and uses that implementation first when
available, then falls back to direct GPIO pin control.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


_MODULE_CACHE: Dict[str, Any] = {}


class MotorError(RuntimeError):
    """Raised when the configured motor cannot be pulsed."""


@dataclass
class MotorSettings:
    enabled: bool = False
    gpio_pin: int = 18
    pulse_ms: int = 5000
    settle_delay_ms: int = 500
    active_high: bool = True
    backend: str = "auto"
    module_name: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MotorSettings":
        payload = dict(data or {})
        return cls(
            enabled=bool(payload.get("enabled", False)),
            gpio_pin=int(payload.get("gpio_pin", 18)),
            pulse_ms=int(payload.get("pulse_ms", payload.get("pulse_duration_ms", 5000))),
            settle_delay_ms=int(payload.get("settle_delay_ms", payload.get("settle_ms", 500))),
            active_high=bool(payload.get("active_high", True)),
            backend=str(payload.get("backend", "auto") or "auto"),
            module_name=str(payload.get("module_name", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _NullMotorBackend:
    def __init__(self, settings: MotorSettings):
        self.settings = settings
        self.backend_name = "null"

    def pulse(self) -> None:
        if self.settings.enabled:
            raise MotorError(
                "Motor control is enabled, but no usable vibration backend is available. "
                "The assay bundle expects the legacy vibration.py backend to be present, or a direct GPIO backend to be installed."
            )

    def close(self) -> None:
        return


class _RPiGPIOBackend:
    def __init__(self, settings: MotorSettings):
        import RPi.GPIO as GPIO  # type: ignore

        self.GPIO = GPIO
        self.settings = settings
        self.backend_name = "RPi.GPIO"
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
        self.backend_name = "gpiozero-output"
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


class _ExternalModuleBackend:
    def __init__(self, settings: MotorSettings):
        self.settings = settings
        self.module, module_name = _load_external_vibration_module(settings)
        self.backend_name = f"module:{module_name}"
        on_names = ("vibration_on", "eccentric_motor_on", "eccentric_on", "motor_on")
        off_names = ("vibration_off", "eccentric_motor_off", "eccentric_off", "motor_off")
        self._on = _resolve_module_callable(self.module, on_names)
        self._off = _resolve_module_callable(self.module, off_names)
        if self._on is None or self._off is None:
            raise MotorError(
                f"Module '{module_name}' was found, but it does not expose a supported on/off pair."
            )

    def pulse(self) -> None:
        self._on()
        try:
            time.sleep(max(0.0, float(self.settings.pulse_ms) / 1000.0))
        finally:
            self._off()

    def close(self) -> None:
        # External repo modules usually own long-lived global PWM devices.
        # Do not force cleanup here.
        return


def _resolve_module_callable(module: Any, candidate_names: Iterable[str]):
    for name in candidate_names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _module_matches_root(module: Any, root: Optional[Path]) -> bool:
    if root is None:
        return True
    module_file = getattr(module, "__file__", "")
    if not module_file:
        return False
    try:
        return Path(module_file).resolve().is_relative_to(root.resolve())
    except Exception:
        return False


def _candidate_import_roots() -> Iterable[Path]:
    here = Path(__file__).resolve()
    env_root = os.environ.get("FIN6_VIBRATION_PATH", "").strip()
    if env_root:
        yield Path(env_root).expanduser()
    yield here.parent.parent / "CodeDirectory"
    yield here.parent.parent
    yield here.parent


def _candidate_module_names(settings: MotorSettings) -> Iterable[str]:
    seen = set()
    explicit_names = [
        str(settings.module_name or "").strip(),
        os.environ.get("FIN6_VIBRATION_MODULE", "").strip(),
    ]
    for raw_name in explicit_names:
        if raw_name and raw_name not in seen:
            seen.add(raw_name)
            yield raw_name
    if "vibration" not in seen:
        seen.add("vibration")
        yield "vibration"


def _load_module_from_root(root: Path, module_name: str):
    if not root.exists():
        return None
    parts = [part for part in module_name.split(".") if part]
    if not parts:
        return None
    file_candidate = root.joinpath(*parts).with_suffix(".py")
    package_candidate = root.joinpath(*parts) / "__init__.py"
    target = file_candidate if file_candidate.exists() else package_candidate if package_candidate.exists() else None
    if target is None:
        return None
    alias = f"_fin6_vibration_backend_{'_'.join(parts)}_{abs(hash(str(target.resolve())))}"
    if alias in _MODULE_CACHE:
        return _MODULE_CACHE[alias]
    existing = sys.modules.get(alias)
    if existing is not None:
        _MODULE_CACHE[alias] = existing
        return existing
    spec = importlib.util.spec_from_file_location(alias, target)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[alias] = module
    return module


def _load_external_vibration_module(settings: MotorSettings):
    errors = []
    env_root_text = os.environ.get("FIN6_VIBRATION_PATH", "").strip()
    env_root = Path(env_root_text).expanduser() if env_root_text else None
    for module_name in _candidate_module_names(settings):
        existing_module = sys.modules.get(module_name)
        if existing_module is not None and _module_matches_root(existing_module, env_root):
            return existing_module, module_name
        for root in _candidate_import_roots():
            try:
                module = _load_module_from_root(root, module_name)
                if module is not None:
                    return module, module_name
            except Exception as exc:
                errors.append(f"{module_name} from {root}: {exc}")
        try:
            module = importlib.import_module(module_name)
            if _module_matches_root(module, env_root):
                return module, module_name
            errors.append(f"{module_name} from sys.path: loaded unexpected path {getattr(module, '__file__', '')}")
        except Exception as exc:
            errors.append(f"{module_name} from sys.path: {exc}")
    raise ImportError("; ".join(errors) if errors else "No external vibration module candidates were found.")


class VibrationMotor:
    def __init__(self, settings: Optional[MotorSettings | Dict[str, Any]] = None):
        if settings is None:
            settings = MotorSettings()
        if not isinstance(settings, MotorSettings):
            settings = MotorSettings.from_dict(dict(settings))
        self.settings = settings
        self._backend = self._build_backend(settings)
        self.backend_name = getattr(self._backend, "backend_name", self._backend.__class__.__name__)

    def _build_backend(self, settings: MotorSettings):
        requested = str(settings.backend or "auto").strip().lower()
        backends = []
        if requested in {"auto", "module", "python-module", "external"} or settings.module_name:
            backends.append(_ExternalModuleBackend)
        if requested in {"auto", "rpi", "rpigpio", "rpi.gpio"}:
            backends.append(_RPiGPIOBackend)
        if requested in {"auto", "gpiozero", "pin", "output"}:
            backends.append(_GpioZeroBackend)
        if not backends:
            backends = [_ExternalModuleBackend, _RPiGPIOBackend, _GpioZeroBackend]
        for backend_cls in backends:
            try:
                return backend_cls(settings)
            except Exception:
                continue
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


def pulse_vibration_motor(settings: Optional[MotorSettings | Dict[str, Any]]) -> Dict[str, Any]:
    motor = VibrationMotor(settings)
    try:
        motor.pulse()
        return {"backend_name": getattr(motor, "backend_name", "")}
    finally:
        motor.close()
