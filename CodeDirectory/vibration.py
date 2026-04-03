from gpiozero import PWMOutputDevice, OutputDevice

PWM_PIN = 12
DIR_PIN = 24

motor_pwm = PWMOutputDevice(PWM_PIN, frequency=1000)
motor_dir = OutputDevice(DIR_PIN)

motor_dir.on()  # set direction once


def vibration_on():
    print("Vibration Motor ON (100%)")
    motor_pwm.value = 1.0


def vibration_off():
    print("Vibration Motor OFF")
    motor_pwm.value = 0.0


if __name__ == "__main__":
    try:
        while True:
            cmd = input("s=ON, x=OFF, q=quit: ").strip().lower()

            if cmd == 's':
                vibration_on()

            elif cmd == 'x':
                vibration_off()

            elif cmd == 'q':
                break

            else:
                print("Invalid command")

    finally:
        vibration_off()
