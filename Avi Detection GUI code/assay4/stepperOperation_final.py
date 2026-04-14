from gpiozero import OutputDevice
from gpiozero import DigitalInputDevice
from time import sleep
import sys
# ---------------- DO NOT EXCEED 245mm--------

# ---------------- GPIO SETUP ----------------
STEP = OutputDevice(20)
DIR  = OutputDevice(21)
EN   = OutputDevice(16)
Limit_Min = DigitalInputDevice(5, pull_up=True)
Limit_Max = DigitalInputDevice(6, pull_up=True)
# ---------------- CALIBRATION ----------------
steps_per_rev = 1600
mm_per_rev = 41.75*0.975
mm_per_step = mm_per_rev / steps_per_rev

# Timing correction (measured: 5 sec commanded → 6.4 sec actual)
timing_factor = 0.78125


def get_input(prompt):
    value = input(prompt)
    if value.lower() == 'q':
        print("Exiting.")
        EN.on()
        sys.exit(0)
    return float(value)


try:
    distance_mm = get_input("Enter distance in mm (+ forward, - backward) or 'q' to quit: ")
    move_time = get_input("Enter time to complete move (seconds) or 'q' to quit: ")

    if move_time <= 0:
        raise ValueError("Time must be positive.")

except ValueError as e:
    print(f"Invalid input: {e}")
    EN.on()
    sys.exit(1)

# ---------------- DIRECTION ----------------
if distance_mm >= 0:
    sleep(0.01)
    DIR.on()
if distance_mm < 0:
    sleep(0.01)
    DIR.off()
sleep(0.005)
total_steps = int(abs(distance_mm) / mm_per_step)

if total_steps == 0:
    print("Move too small.")
    EN.on()
    sys.exit(0)

# ---------------- TIMING CALCULATION ----------------
# Ideal delay
ideal_delay = move_time / (2.0 * total_steps)

# Apply correction for Python overhead
min_step_delay = 0.0005 #0.5ms or 1000steps/sec
step_delay = ideal_delay * timing_factor
#step_delay = max(step_delay,min_step_delay)
velocity = abs(distance_mm) / move_time

print("\nRunning:")
print(f"  Distance: {distance_mm} mm")
print(f"  Time: {move_time} sec")
print(f"  Velocity: {velocity:.3f} mm/sec")
print(f"  Total Steps: {total_steps}")
print(f"  Step Delay: {step_delay:.8f} sec\n")

# ---------------- EXECUTION ----------------
EN.off()
sleep(0.01)
# Do not exceed 374mm
moving_toward_min = (distance_mm<0)
moving_toward_max = (distance_mm>0)
min_pulse = 0.00001 # 10us
try:
    for _ in range(total_steps):
        if moving_toward_min and Limit_Min.value:
            print("Minimum Reached: 0mm")
            break
        if moving_toward_max and Limit_Max.value:
            print("Maximum Reached: 374mm")
            break
        STEP.on()
        sleep(step_delay)
        STEP.off()
        sleep(step_delay)

except KeyboardInterrupt:
    print("\nMotion cancelled by user.")

finally:
    EN.on()
    print("Motor disabled. Program ended.")


