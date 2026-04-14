from motion import home_to_zero, move_to_absolute, move_relative, get_current_position
import config

def print_locations():
    print("\n=== CONFIGURED POSITIONS ===")
    print(f"Operational Range : {config.OPERATIONAL_MIN_POS_MM:.2f} to {config.OPERATIONAL_MAX_POS_MM:.2f} mm")
    print(f"Channel Start     : {config.CHANNEL_LOCATION_START:.2f} mm")
    print(f"Channel End       : {config.CHANNEL_LOCATION_END:.2f} mm")
    print(f"Channel Center    : {config.CHANNEL_CENTER:.2f} mm")
    print(f"Chamber Start     : {config.CHAMBER_LOCATION_START:.2f} mm")
    print(f"Chamber End       : {config.CHAMBER_LOCATION_END:.2f} mm")
    print(f"Chamber Center    : {config.CHAMBER_CENTER:.2f} mm")
    print(f"Tube 1 Center     : {config.TUBE_1_CENTER:.2f} mm")
    print(f"Tube 2 Center     : {config.TUBE_2_CENTER:.2f} mm")
    print(f"Tube 3 Center     : {config.TUBE_3_CENTER:.2f} mm")
    print(f"Tube 4 Center     : {config.TUBE_4_CENTER:.2f} mm")
    print(f"Tube 5 Center     : {config.TUBE_5_CENTER:.2f} mm")


def calibration_help():
    print("\n=== DISTANCE CALIBRATION ===")
    print("Recommended procedure:")
    print("1. Home the gantry")
    print("2. Move to a known distance, for example 100 mm")
    print("3. Physically measure actual travel")
    print("4. Enter commanded and measured values")
    print("5. Use the suggested corrected MM_PER_REV")


def compute_new_mm_per_rev():
    try:
        commanded = float(input("Enter commanded distance in mm: "))
        measured = float(input("Enter measured actual distance in mm: "))

        if commanded <= 0 or measured <= 0:
            print("Both values must be positive.")
            return

        new_mm_per_rev = config.MM_PER_REV * (measured / commanded)
        new_mm_per_step = new_mm_per_rev / config.STEPS_PER_REV

        print("\n=== CALIBRATION RESULT ===")
        print(f"Current MM_PER_REV : {config.MM_PER_REV:.6f}")
        print(f"Suggested MM_PER_REV: {new_mm_per_rev:.6f}")
        print(f"Suggested MM_PER_STEP: {new_mm_per_step:.8f}")

        print("\nReplace these lines in config.py:")
        print(f"MM_PER_REV = {new_mm_per_rev:.6f}")
        print(f"MM_PER_STEP = MM_PER_REV / STEPS_PER_REV")

    except ValueError:
        print("Invalid numeric input.")


def go_to_named_location():
    locations = {
        "channel_start": config.CHANNEL_LOCATION_START,
        "channel_end": config.CHANNEL_LOCATION_END,
        "channel_center": config.CHANNEL_CENTER,
        "chamber_start": config.CHAMBER_LOCATION_START,
        "chamber_end": config.CHAMBER_LOCATION_END,
        "chamber_center": config.CHAMBER_CENTER,
        "tube1": config.TUBE_1_CENTER,
        "tube2": config.TUBE_2_CENTER,
        "tube3": config.TUBE_3_CENTER,
        "tube4": config.TUBE_4_CENTER,
        "tube5": config.TUBE_5_CENTER,
    }

    print("\nAvailable locations:")
    for name, value in locations.items():
        print(f"  {name:15s} -> {value:.2f} mm")

    choice = input("\nEnter location name: ").strip().lower()

    if choice not in locations:
        print("Unknown location.")
        return

    target = locations[choice]
    move_to_absolute(target)
    print(f"Moved to {choice} at {target:.2f} mm")
    print(f"Software position: {get_current_position():.2f} mm")


def move_absolute_manual():
    try:
        target = float(input("Enter absolute target position (mm): "))
        move_to_absolute(target)
        print(f"Software position: {get_current_position():.2f} mm")
    except ValueError:
        print("Invalid number.")


def move_relative_manual():
    try:
        delta = float(input("Enter relative move distance (mm, +/-): "))
        move_relative(delta)
        print(f"Software position: {get_current_position():.2f} mm")
    except ValueError:
        print("Invalid number.")


def main():
    print("=== Gantry Calibration / Position Test ===")

    while True:
        print("\nMenu:")
        print("  1 - Home gantry to zero")
        print("  2 - Print configured locations")
        print("  3 - Move to absolute position")
        print("  4 - Move relative distance")
        print("  5 - Move to named config location")
        print("  6 - Distance calibration help")
        print("  7 - Compute corrected MM_PER_REV")
        print("  8 - Print current software position")
        print("  q - Quit")

        cmd = input("\nSelect: ").strip().lower()

        if cmd == "1":
            home_to_zero()
            print(f"Software position: {get_current_position():.2f} mm")

        elif cmd == "2":
            print_locations()

        elif cmd == "3":
            move_absolute_manual()

        elif cmd == "4":
            move_relative_manual()

        elif cmd == "5":
            go_to_named_location()

        elif cmd == "6":
            calibration_help()

        elif cmd == "7":
            compute_new_mm_per_rev()

        elif cmd == "8":
            print(f"Software position: {get_current_position():.2f} mm")

        elif cmd == "q":
            print("Exiting.")
            break

        else:
            print("Invalid selection.")


if __name__ == "__main__":
    main()
