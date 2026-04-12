from pathlib import Path
import sys
from motion import home_to_zero, move_to_absolute, get_current_position
from gpiozero import PWMOutputDevice, OutputDevice
from assay import assay
import config
import time
import json
import os

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.config.project_paths import DETECTION_RESULT_PATH


# -----------------------------
# VACUUM MOTOR SETUP
# -----------------------------
PWM_PIN = 13
DIR_PIN = 25

motor_pwm = PWMOutputDevice(PWM_PIN, frequency=1000)
motor_dir = OutputDevice(DIR_PIN)

motor_dir.on()


def vacuum_on():
    print("Vacuum ON")
    motor_pwm.value = 1.0


def vacuum_off():
    print("Vacuum OFF")
    motor_pwm.value = 0.0


# -----------------------------
# HELPERS
# -----------------------------
def move_and_report(label: str, position_mm: float, settle_s: float = 0.5):
    print(f"\nMoving to {label} at {position_mm:.2f} mm...")
    move_to_absolute(position_mm)
    print(f"Reached {label}. Software position: {get_current_position():.2f} mm")
    time.sleep(settle_s)


def clamp_operational(position_mm: float) -> float:
    if position_mm < 0.0:
        return 0.0

    max_mm = config.GANTRY_MAX_MM - (2.0 * config.VACUUM_CENTER_OFFSET_MM)
    if position_mm > max_mm:
        return max_mm

    return position_mm

def load_x_positions_from_json():
    """
    Loads x positions from the shared channel-detection output JSON.

    Uses:
      data["x_positions_mm"]

    Returns:
      - list of floats when flies remain
      - "done" when no flies remain
      - [] for malformed/missing data that should retry
    """
    json_path = str(DETECTION_RESULT_PATH)

    try:
        with open(json_path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"JSON file not found: {json_path}")
        return []
    except json.JSONDecodeError:
        print(f"Invalid JSON format in: {json_path}")
        return []

    # If detector says nothing remains, end operation
    if not data.get("fly_remaining", False):
        print("JSON says no flies remain.")
        return "done"

    if "x_positions_mm" not in data:
        print("JSON does not contain key: 'x_positions_mm'")
        return []

    raw_positions = data["x_positions_mm"]

    if not isinstance(raw_positions, list):
        print("'x_positions_mm' is not a list.")
        return []

    try:
        positions = [float(x) for x in raw_positions]
    except (TypeError, ValueError):
        print("One or more x_positions_mm values are not numeric.")
        return []

    # If detector says flies remain but list is empty, treat as done too
    if len(positions) == 0:
        print("JSON contains no x_positions_mm entries.")
        return "done"

    return positions

def get_next_pickup_position():
    """
    Wait for user confirmation that channel detection is finished,
    then pull x positions from the JSON file and choose the largest one.

    Returns:
      - float pickup position
      - "done" when no flies remain
    """
    while True:
        ready = input("Channel Detection Finished (Y/N)? ").strip().upper()

        if ready == "N":
            print("Waiting for channel detection to finish...")
            continue

        if ready != "Y":
            print("Invalid input. Enter Y or N.")
            continue

        parsed = load_x_positions_from_json()

        if parsed == "done":
            return "done"

        if len(parsed) == 0:
            print("No valid X positions found in JSON. Re-run detection and then enter Y again.")
            continue

        pickup_positions = sorted(
            [clamp_operational(x) for x in parsed],
            reverse=True
        )

        print("\nLoaded pickup candidates from JSON (largest to smallest):")
        for idx, pos in enumerate(pickup_positions, start=1):
            print(f"  {idx}: {pos:.2f} mm")

        selected = pickup_positions[0]
        print(f"Selected pickup position for this cycle: {selected:.2f} mm")
        return selected

# -----------------------------
# MAIN OPERATION
# -----------------------------
def run_operation():
    print("\n=== STARTING GANTRY OPERATION ===")

    chamber_drop_s = 2.0
    chamber_identify_s = 6.0
    chamber_pickup_s = 2.0
    tube_drop_s = 2.0

    camera_photo_position = clamp_operational(config.CHANNEL_LOCATION_END + 15.0)

    cycle_index = 0

    try:
        while True:
            cycle_index += 1
            tube_label = "Tube 1" if (cycle_index - 1) % 2 == 0 else "Tube 2"
            tube_position = config.TUBE_1_CENTER if (cycle_index - 1) % 2 == 0 else config.TUBE_2_CENTER

            print(f"\n--- Cycle {cycle_index} ---")

            # Home first
            print("Homing gantry...")
            vacuum_off()
            home_to_zero()
            print(f"Homed. Software position: {get_current_position():.2f} mm")

            # Move to offset/photo location
            move_and_report("Channel Photo Position", camera_photo_position)

            # Wait for detection completion, then load fresh x-coordinates from JSON
            pickup_position = get_next_pickup_position()

            if pickup_position == "done":
                print("No more flies remaining. Ending operation.")
                break

            # Move inward to chosen pickup point
            move_and_report("Channel Pickup Position", pickup_position)

            # Pick up fly
            print("At pickup location: picking up fly...")
            vacuum_on()
            time.sleep(2)

            # Move to chamber while holding
            move_and_report("Chamber Center", config.CHAMBER_CENTER)

            # Chamber sequence
            print(f"At Chamber Center: dropping for {chamber_drop_s:.1f} s...")
            vacuum_off()
            time.sleep(chamber_drop_s)

            print(f"Identification window for {chamber_identify_s:.1f} s...")
            time.sleep(chamber_identify_s)

            print(f"Picking fly back up for {chamber_pickup_s:.1f} s...")
            vacuum_on()
            time.sleep(chamber_pickup_s)

            # Move to alternating tube
            move_and_report(tube_label, tube_position)

            # Drop into tube
            print(f"At {tube_label}: dropping fly for {tube_drop_s:.1f} s...")
            vacuum_off()
            time.sleep(tube_drop_s)

            # Return home before next image/check
            print("Returning home and resetting with vacuum OFF...")
            vacuum_off()
            home_to_zero()
            print(f"Reset complete. Software position: {get_current_position():.2f} mm")

        print("\n=== OPERATION COMPLETE ===")
        print(f"Final software position: {get_current_position():.2f} mm")

    finally:
        vacuum_off()

    print("\n=== ASSAY STARTING SOON ===")
    for i in range(10, 0, -1):
        print(f"Starting in {i}...")
        time.sleep(1)

    assay()

def main():
    print("=== Gantry Operation with JSON-based Channel Detection ===")
    print("Per cycle:")
    print("1. Home")
    print("2. Go to CHANNEL_LOCATION_END + 15")
    print("3. Ask if channel detection is finished")
    print("4. When Y, load x coordinates from:")
    print(f"   {DETECTION_RESULT_PATH}")
    print("5. Sort descending and choose the largest X")
    print("6. Move to pickup position")
    print("7. Pick up fly")
    print("8. Move to chamber")
    print("9. Drop 2 s, identify 6 s, pick up 2 s")
    print("10. Move to Tube 1 / Tube 2")
    print("11. Drop into tube")
    print("12. Home")
    print("13. Repeat with freshly updated JSON file")

    run_operation()


if __name__ == "__main__":
    main()
