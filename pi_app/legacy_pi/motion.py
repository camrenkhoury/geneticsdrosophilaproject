from __future__ import annotations

from time import monotonic, sleep

from shared.config.project_paths import ensure_code_directory_on_path
from shared.debug.operation_trace import append_operation_trace

ensure_code_directory_on_path()

try:
    from gpiozero import DigitalInputDevice, OutputDevice

    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

    class MockOutputDevice:
        def __init__(self, pin):
            self.pin = pin
            self.state = False

        def on(self):
            self.state = True

        def off(self):
            self.state = False

    class MockDigitalInputDevice:
        def __init__(self, pin, pull_up=True):
            self.pin = pin
            self.pull_up = pull_up
            self.state = False

        @property
        def value(self):
            return self.state

    OutputDevice = MockOutputDevice
    DigitalInputDevice = MockDigitalInputDevice

import config

PI_OPERATION_TRACE_FILENAME = ".pi_operation_trace.log"

STEP = None
DIR = None
EN = None
Limit_Min = None
Limit_Max = None

current_position_mm = 0.0


def _trace_motion(event: str, **fields):
    try:
        append_operation_trace(
            PI_OPERATION_TRACE_FILENAME,
            "legacy_motion",
            event,
            current_position_mm=current_position_mm,
            **fields,
        )
    except Exception:
        pass


def _ensure_devices():
    global STEP, DIR, EN, Limit_Min, Limit_Max
    if STEP is not None and DIR is not None and EN is not None and Limit_Min is not None and Limit_Max is not None:
        return STEP, DIR, EN, Limit_Min, Limit_Max

    STEP = OutputDevice(config.STEP_PIN)
    DIR = OutputDevice(config.DIR_PIN)
    EN = OutputDevice(config.EN_PIN)
    Limit_Min = DigitalInputDevice(config.LIMIT_MIN_PIN)
    Limit_Max = DigitalInputDevice(config.LIMIT_MAX_PIN)
    return STEP, DIR, EN, Limit_Min, Limit_Max


def enable_motor():
    if not GPIO_AVAILABLE:
        return
    _, _, enable_device, _, _ = _ensure_devices()
    enable_device.off()
    sleep(0.01)


def disable_motor():
    if not GPIO_AVAILABLE:
        return
    _, _, enable_device, _, _ = _ensure_devices()
    enable_device.on()
    sleep(0.01)


def set_direction(forward: bool):
    if not GPIO_AVAILABLE:
        return
    _, direction_device, _, _, _ = _ensure_devices()
    if forward:
        direction_device.on()
    else:
        direction_device.off()
    sleep(0.005)


def step_once(step_delay):
    if not GPIO_AVAILABLE:
        sleep(step_delay * 2)
        return
    step_device, _, _, _, _ = _ensure_devices()
    step_device.on()
    sleep(step_delay)
    step_device.off()
    sleep(step_delay)


def get_operational_max_mm():
    return config.GANTRY_MAX_MM - (2.0 * config.VACUUM_CENTER_OFFSET_MM)


def _steps_for_distance(distance_mm: float) -> int:
    return max(1, int(round(abs(float(distance_mm)) / config.MM_PER_STEP)))


def move_relative(distance_mm, move_time=None):
    global current_position_mm

    if distance_mm == 0:
        return

    target_position = current_position_mm + distance_mm

    if target_position < 0.0:
        target_position = 0.0
    if target_position > get_operational_max_mm():
        target_position = get_operational_max_mm()

    actual_distance = target_position - current_position_mm
    if actual_distance == 0:
        print("Move blocked by operational limits.")
        return

    if not GPIO_AVAILABLE:
        sleep(abs(actual_distance) * 0.01)
        current_position_mm = target_position
        print(f"Simulated move to {current_position_mm:.2f} mm")
        return

    _, _, _, limit_min_device, limit_max_device = _ensure_devices()

    forward = actual_distance > 0
    total_steps = int(abs(actual_distance) / config.MM_PER_STEP)

    if total_steps == 0:
        print("Move too small.")
        return

    if move_time is not None and move_time > 0:
        ideal_delay = move_time / (2.0 * total_steps)
        step_delay = ideal_delay * config.TIMING_FACTOR
    else:
        step_delay = config.DEFAULT_STEP_DELAY

    enable_motor()
    set_direction(forward)

    final_position = current_position_mm

    try:
        for _ in range(total_steps):
            if not forward and limit_min_device.value:
                print("Minimum physical limit reached.")
                final_position = 0.0
                break

            if forward and limit_max_device.value:
                print("Maximum physical limit reached.")
                final_position = get_operational_max_mm()
                break

            step_once(step_delay)
        else:
            final_position = target_position

        current_position_mm = final_position
    finally:
        disable_motor()


def move_to_absolute(target_mm, move_time=None):
    if target_mm < 0.0:
        target_mm = 0.0
    if target_mm > get_operational_max_mm():
        target_mm = get_operational_max_mm()

    distance = target_mm - current_position_mm
    move_relative(distance, move_time)


def home_to_zero(max_steps=None):
    global current_position_mm

    print("Homing to zero...")
    if not GPIO_AVAILABLE:
        sleep(2)
        current_position_mm = 0.0
        print("Homed (simulated). Usable position set to 0.0 mm.")
        return

    _, _, _, limit_min_device, _ = _ensure_devices()
    fast_step_delay = float(getattr(config, "HOME_STEP_DELAY", config.DEFAULT_STEP_DELAY))
    fine_step_delay = float(getattr(config, "HOME_FINE_STEP_DELAY", fast_step_delay))
    max_seconds = float(getattr(config, "HOME_MAX_SECONDS", 15.0) or 15.0)
    if max_steps is None:
        full_travel_steps = _steps_for_distance(get_operational_max_mm())
        max_steps = max(1, int(round(full_travel_steps * 1.2)))
    backoff_steps = _steps_for_distance(float(getattr(config, "HOME_BACKOFF_MM", 2.0)))
    steps_taken = 0
    phase_started_at = monotonic()

    _trace_motion(
        "home_to_zero_enter",
        fast_step_delay=fast_step_delay,
        fine_step_delay=fine_step_delay,
        max_steps=max_steps,
        max_seconds=max_seconds,
        backoff_steps=backoff_steps,
        limit_min_initial=bool(limit_min_device.value),
    )

    enable_motor()

    try:
        set_direction(False)
        while not limit_min_device.value:
            step_once(fast_step_delay)
            steps_taken += 1
            if steps_taken % 1000 == 0:
                _trace_motion(
                    "home_fast_progress",
                    steps_taken=steps_taken,
                    limit_min=bool(limit_min_device.value),
                )

            if steps_taken >= max_steps:
                message = f"Homing failed: exceeded {max_steps} steps on fast approach without reaching home switch."
                _trace_motion("home_fast_fail_max_steps", steps_taken=steps_taken, limit_min=bool(limit_min_device.value))
                raise RuntimeError(message)
            if monotonic() - phase_started_at >= max_seconds:
                message = f"Homing failed: exceeded {max_seconds:.1f}s on fast approach without reaching home switch."
                _trace_motion("home_fast_fail_timeout", steps_taken=steps_taken, limit_min=bool(limit_min_device.value))
                raise RuntimeError(message)

        if not limit_min_device.value:
            message = "Homing failed: home switch not reached on fast approach."
            _trace_motion("home_fast_fail_unreached", steps_taken=steps_taken, limit_min=bool(limit_min_device.value))
            raise RuntimeError(message)

        _trace_motion("home_fast_limit_hit", steps_taken=steps_taken, limit_min=bool(limit_min_device.value))

        # Back off the switch, then re-approach slowly to improve zero repeatability.
        set_direction(True)
        backoff_count = 0
        phase_started_at = monotonic()
        while limit_min_device.value and backoff_count < backoff_steps:
            step_once(fine_step_delay)
            backoff_count += 1

        if limit_min_device.value:
            message = "Homing failed: home switch remained active after backoff."
            _trace_motion(
                "home_backoff_fail_stuck_switch",
                backoff_count=backoff_count,
                backoff_steps=backoff_steps,
                limit_min=bool(limit_min_device.value),
            )
            raise RuntimeError(message)
        if monotonic() - phase_started_at >= max_seconds:
            message = f"Homing failed: exceeded {max_seconds:.1f}s during backoff."
            _trace_motion(
                "home_backoff_fail_timeout",
                backoff_count=backoff_count,
                backoff_steps=backoff_steps,
                limit_min=bool(limit_min_device.value),
            )
            raise RuntimeError(message)

        _trace_motion(
            "home_backoff_complete",
            backoff_count=backoff_count,
            backoff_steps=backoff_steps,
            limit_min=bool(limit_min_device.value),
        )

        set_direction(False)
        fine_steps_taken = 0
        phase_started_at = monotonic()
        while not limit_min_device.value:
            step_once(fine_step_delay)
            fine_steps_taken += 1
            if fine_steps_taken % 500 == 0:
                _trace_motion(
                    "home_fine_progress",
                    fine_steps_taken=fine_steps_taken,
                    limit_min=bool(limit_min_device.value),
                )
            if fine_steps_taken >= max_steps:
                message = f"Homing failed: exceeded {max_steps} steps on fine approach without reaching home switch."
                _trace_motion(
                    "home_fine_fail_max_steps",
                    fine_steps_taken=fine_steps_taken,
                    limit_min=bool(limit_min_device.value),
                )
                raise RuntimeError(message)
            if monotonic() - phase_started_at >= max_seconds:
                message = f"Homing failed: exceeded {max_seconds:.1f}s on fine approach without reaching home switch."
                _trace_motion(
                    "home_fine_fail_timeout",
                    fine_steps_taken=fine_steps_taken,
                    limit_min=bool(limit_min_device.value),
                )
                raise RuntimeError(message)

        if limit_min_device.value:
            current_position_mm = 0.0
            print("Homed. Usable position set to 0.0 mm.")
            _trace_motion("home_to_zero_exit", fine_steps_taken=fine_steps_taken, limit_min=bool(limit_min_device.value))
        else:
            message = "Homing failed: home switch not reached on fine approach."
            _trace_motion(
                "home_fine_fail_unreached",
                fine_steps_taken=fine_steps_taken,
                limit_min=bool(limit_min_device.value),
            )
            raise RuntimeError(message)
    finally:
        disable_motor()


def get_current_position():
    return current_position_mm


def print_position():
    print(f"Current usable vacuum position: {current_position_mm:.2f} mm")
