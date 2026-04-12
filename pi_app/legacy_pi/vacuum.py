from __future__ import annotations

try:
    from gpiozero import OutputDevice, PWMOutputDevice

    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

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

PWM_PIN = 13
DIR_PIN = 25

motor_pwm = PWMOutputDevice(PWM_PIN, frequency=1000)
motor_dir = OutputDevice(DIR_PIN)

motor_dir.on()


def vacuum_on():
    if GPIO_AVAILABLE:
        motor_pwm.value = 1.0
    print("Vacuum ON")


def vacuum_off():
    if GPIO_AVAILABLE:
        motor_pwm.value = 0.0
    print("Vacuum OFF")


if __name__ == "__main__":
    try:
        while True:
            cmd = input("s=ON, x=OFF, q=quit: ")

            if cmd == "s":
                vacuum_on()
            elif cmd == "x":
                vacuum_off()
            elif cmd == "q":
                break
    finally:
        vacuum_off()

