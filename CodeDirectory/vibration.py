try:
    from gpiozero import PWMOutputDevice, OutputDevice
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    # Mock classes
    class MockPWMOutputDevice:
        def __init__(self, pin, frequency=1000):
            self.pin = pin
            self.frequency = frequency
            self.value = 0.0

    class MockOutputDevice:
        def __init__(self, pin):
            self.pin = pin
            self.state = False

        def on(self):
            self.state = True

        def off(self):
            self.state = False

    PWMOutputDevice = MockPWMOutputDevice
    OutputDevice = MockOutputDevice

PWM_PIN = 12
DIR_PIN = 24

motor_pwm = PWMOutputDevice(PWM_PIN, frequency=1000)
motor_dir = OutputDevice(DIR_PIN)

motor_dir.on()  # set direction once


def vibration_on():
    if GPIO_AVAILABLE:
        motor_pwm.value = 1.0
    print("Vibration Motor ON (100%)")


def vibration_off():
    if GPIO_AVAILABLE:
        motor_pwm.value = 0.0
    print("Vibration Motor OFF")


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
