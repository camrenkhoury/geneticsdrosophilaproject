from __future__ import annotations

from time import sleep

from shared.config.project_paths import ensure_code_directory_on_path

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


STEP = OutputDevice(config.STEP_PIN)
DIR = OutputDevice(config.DIR_PIN)
EN = OutputDevice(config.EN_PIN)

Limit_Min = DigitalInputDevice(config.LIMIT_MIN_PIN)
Limit_Max = DigitalInputDevice(config.LIMIT_MAX_PIN)

current_position_mm = 0.0


def enable_motor():
    EN.off()
    sleep(0.01)


def disable_motor():
    EN.on()
    sleep(0.01)


def set_direction(forward: bool):
    if forward:
        DIR.on()
    else:
        DIR.off()
    sleep(0.005)


def step_once(step_delay):
    STEP.on()
    sleep(step_delay)
    STEP.off()
    sleep(step_delay)


def get_operational_max_mm():
    return config.GANTRY_MAX_MM - (2.0 * config.VACUUM_CENTER_OFFSET_MM)


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
            if not forward and Limit_Min.value:
                print("Minimum physical limit reached.")
                final_position = 0.0
                break

            if forward and Limit_Max.value:
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


def home_to_zero(max_steps=50000):
    global current_position_mm

    print("Homing to zero...")
    if not GPIO_AVAILABLE:
        sleep(2)
        current_position_mm = 0.0
        print("Homed (simulated). Usable position set to 0.0 mm.")
        return

    enable_motor()
    set_direction(False)

    steps_taken = 0

    try:
        while not Limit_Min.value:
            step_once(config.HOME_STEP_DELAY)
            steps_taken += 1

            if steps_taken >= max_steps:
                print("Homing aborted: max steps reached.")
                break

        if Limit_Min.value:
            current_position_mm = 0.0
            print("Homed. Usable position set to 0.0 mm.")
        else:
            print("Home switch not reached. Position not trusted.")
    finally:
        disable_motor()


def get_current_position():
    return current_position_mm


def print_position():
    print(f"Current usable vacuum position: {current_position_mm:.2f} mm")

