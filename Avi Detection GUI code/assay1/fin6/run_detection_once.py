#!/usr/bin/env python3
"""
Simple wrapper to run one detection from two image files.

This exists mainly so you do not have to remember the longer CLI command.
Edit the paths below, then run:
    python3 run_detection_once.py
"""

from pathlib import Path
import json
import cv2

from fly_x_detector import process_fly_detection

# -----------------------------
# EDIT THESE PATHS
# -----------------------------
BACKGROUND_IMAGE = "background.jpg"
FRAME_IMAGE = "flies.jpg"
CALIBRATION_JSON = "calibration.json"

OUTPUT_DIR = Path("output")
ANNOTATED_OUT = OUTPUT_DIR / "annotated_detection.png"
MASK_OUT = OUTPUT_DIR / "fly_mask.png"
JSON_OUT = OUTPUT_DIR / "fly_results.json"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    result, annotated, mask = process_fly_detection(
        background=BACKGROUND_IMAGE,
        frame=FRAME_IMAGE,
        calibration_path=CALIBRATION_JSON,
    )

    cv2.imwrite(str(ANNOTATED_OUT), annotated)
    cv2.imwrite(str(MASK_OUT), mask)

    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("Done.")
    print(f"Annotated image: {ANNOTATED_OUT.resolve()}")
    print(f"Mask image:      {MASK_OUT.resolve()}")
    print(f"JSON results:    {JSON_OUT.resolve()}")
    print(f"Fly remaining:   {result['fly_remaining']}")
    print(f"Count:           {result['count']}")
    print(f"X positions mm:  {result['x_positions_mm']}")


if __name__ == "__main__":
    main()
