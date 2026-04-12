from __future__ import annotations

import time

from .vibration import vibration_off, vibration_on


def assay():
    print("Assay Started")

    vibration_on()
    time.sleep(5)
    vibration_off()

    print("Assay Completed")

