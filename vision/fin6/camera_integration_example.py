#!/usr/bin/env python3
"""
Example of how another Python script can pass images directly as NumPy arrays.

This is the pattern to use if a different camera script captures frames first,
then hands those frames to the detector.
"""

from pathlib import Path
import json
import cv2
import numpy as np

try:
    from .fly_x_detector import process_fly_detection
except ImportError:
    from fly_x_detector import process_fly_detection


def run_detection_with_arrays(background_bgr: np.ndarray, frame_bgr: np.ndarray) -> dict:
    result, annotated, mask = process_fly_detection(
        background=background_bgr,
        frame=frame_bgr,
        calibration_path="calibration.json",
    )

    out_dir = Path("output_from_arrays")
    out_dir.mkdir(parents=True, exist_ok=True)

    cv2.imwrite(str(out_dir / "annotated_detection.png"), annotated)
    cv2.imwrite(str(out_dir / "fly_mask.png"), mask)

    with open(out_dir / "fly_results.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result


def main() -> None:
    # Demo using image files, then converting them to arrays.
    # In your real camera pipeline, these two lines would be replaced by frames
    # captured from a Pi camera, USB camera, or another imaging source.
    background_bgr = cv2.imread("background.jpg")
    frame_bgr = cv2.imread("flies.jpg")

    if background_bgr is None:
        raise FileNotFoundError("Could not read background.jpg")
    if frame_bgr is None:
        raise FileNotFoundError("Could not read flies.jpg")

    result = run_detection_with_arrays(background_bgr, frame_bgr)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
