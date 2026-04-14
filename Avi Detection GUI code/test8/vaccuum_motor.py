from gpiozero import PWMOutputDevice, OutputDevice

PWM_PIN = 13
DIR_PIN = 25

motor_pwm = PWMOutputDevice(PWM_PIN, frequency=1000)
motor_dir = OutputDevice(DIR_PIN)

motor_dir.on()  # set direction once

try:
    while True:
        cmd = input("s=ON, x=OFF, q=quit: ")

        if cmd == 's':
            print("Motor ON (100%)")
            motor_pwm.value = 1.0   # FULL DUTY

        elif cmd == 'x':
            print("Motor OFF")
            motor_pwm.value = 0.0   # OFF

        elif cmd == 'q':
            break

finally:
    motor_pwm.value = 0.0
