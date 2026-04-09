from gpiozero import DigitalInputDevice
from time import sleep
import config

Limit_Min = DigitalInputDevice(config.LIMIT_MIN_PIN, pull_up=False)
Limit_Max = DigitalInputDevice(config.LIMIT_MAX_PIN, pull_up=False)

print("Watching limit switches. Press Ctrl+C to stop.")

while True:
    print("MIN:", Limit_Min.value, " MAX:", Limit_Max.value)
    sleep(0.5)
