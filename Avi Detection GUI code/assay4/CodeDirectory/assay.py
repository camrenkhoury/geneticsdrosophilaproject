import time
from vibration import vibration_on, vibration_off


def assay():
    print("Assay Started")

    vibration_on()
    time.sleep(5)
    vibration_off()

    print("Assay Completed")
