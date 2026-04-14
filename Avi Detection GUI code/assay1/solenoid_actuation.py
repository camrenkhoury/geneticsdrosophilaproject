from gpiozero import LED
from time import sleep

solenoid = LED(4)

try:
    while True:
        cmd = input("Type 'start' or 'stop': ").strip().lower()

        if cmd == "start":
            solenoid.blink(on_time=0.08, off_time=0.10, background=True)
            print("Started pulsing")

        elif cmd == "stop":
            solenoid.off()
            print("Stopped")

except KeyboardInterrupt:
    solenoid.off()
