from motion import home_to_zero, move_to_absolute, move_relative, get_current_position

def main():
    print("Interactive Motion Test")
    print("Commands:")
    print("  h           -> home to zero")
    print("  a           -> move to absolute position")
    print("  r           -> move relative distance")
    print("  p           -> print current software position")
    print("  q           -> quit")

    while True:
        cmd = input("\nEnter command: ").strip().lower()

        if cmd == "q":
            print("Exiting motion test.")
            break

        elif cmd == "h":
            home_to_zero()
            print(f"Current position: {get_current_position():.2f} mm")

        elif cmd == "p":
            print(f"Current position: {get_current_position():.2f} mm")

        elif cmd == "a":
            try:
                value = float(input("Enter absolute target position in mm: "))
                move_to_absolute(value)
                print(f"Current position: {get_current_position():.2f} mm")
            except ValueError:
                print("Invalid number.")

        elif cmd == "r":
            try:
                value = float(input("Enter relative move in mm (+/-): "))
                move_relative(value)
                print(f"Current position: {get_current_position():.2f} mm")
            except ValueError:
                print("Invalid number.")

        else:
            print("Invalid command.")

if __name__ == "__main__":
    main()
