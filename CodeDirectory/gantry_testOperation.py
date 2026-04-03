from motion import home_to_zero, move_to_absolute, get_current_position
from gpiozero import PWMOutputDevice, OutputDevice
import config
import time
import json
import os

# -----------------------------
# VACUUM SETUP
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
# POSITIONS
# -----------------------------
CAMERA_POS = config.CHANNEL_LOCATION_END + 15.0
CHAMBER_POS = config.CHAMBER_CENTER

TUBES = {
    "1": config.TUBE_1_CENTER,
    "2": config.TUBE_2_CENTER,
    "3": getattr(config, "TUBE_3_CENTER", config.TUBE_2_CENTER),
    "4": getattr(config, "TUBE_4_CENTER", config.TUBE_2_CENTER),
    "5": getattr(config, "TUBE_5_CENTER", config.TUBE_2_CENTER),
}


# -----------------------------
# HELPERS
# -----------------------------
def move_and_report(label, pos):
    print(f"\nMoving to {label} ({pos:.2f} mm)")
    move_to_absolute(pos)
    print(f"Reached {label}: {get_current_position():.2f} mm")
    time.sleep(0.5)


def load_pickup_from_json():
    path = os.path.expanduser("~/fin6/outputs/channel/last_channel_result.json")

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception as e:
        print(f"JSON error: {e}")
        return None

    if not data.get("fly_remaining", False):
        print("No flies remaining.")
        return None

    positions = data.get("x_positions_mm", [])

    if not positions:
        print("No positions found.")
        return None

    try:
        positions = [float(x) for x in positions]
    except:
        print("Invalid position data.")
        return None

    selected = max(positions)
    print(f"Selected pickup position: {selected:.2f} mm")
    return selected


def wait_for_user(msg):
    input(f"\n>>> {msg} (press ENTER)")


def choose_tube():
    while True:
        choice = input("Select tube (1-5): ").strip()
        if choice in TUBES:
            return choice, TUBES[choice]
        print("Invalid tube.")


# -----------------------------
# MAIN LOOP
# -----------------------------
def run():
    cycle = 0

    while True:
        cycle += 1
        print(f"\n===== TRIAL {cycle} =====")

        # HOME
        vacuum_off()
        wait_for_user("Home gantry")
        home_to_zero()

        # CAMERA POSITION
        wait_for_user("Move to camera position")
        move_and_report("Camera", CAMERA_POS)

        # LOAD JSON PICKUP
        wait_for_user("Load pickup from JSON")
        pickup = load_pickup_from_json()

        if pickup is None:
            print("Stopping.")
            break

        # MOVE TO PICKUP
        wait_for_user("Move to pickup")
        move_and_report("Pickup", pickup)

        # VACUUM ON
        wait_for_user("Turn vacuum ON")
        vacuum_on()

        # CHAMBER
        wait_for_user("Move to chamber")
        move_and_report("Chamber", CHAMBER_POS)

        # DROP
        wait_for_user("Drop fly")
        vacuum_off()

        # PICK BACK UP
        wait_for_user("Re-acquire fly")
        vacuum_on()

        # SELECT TUBE
        tube_label, tube_pos = choose_tube()

        # MOVE TO TUBE
        wait_for_user(f"Move to Tube {tube_label}")
        move_and_report(f"Tube {tube_label}", tube_pos)

        # DROP IN TUBE
        wait_for_user("Drop fly into tube")
        vacuum_off()

        # HOME AGAIN
        wait_for_user("Return home")
        home_to_zero()

        cont = input("\nContinue to next trial? (Y/N): ").strip().upper()
        if cont != "Y":
            break

    vacuum_off()
    print("\n=== DONE ===")


if __name__ == "__main__":
    run()
