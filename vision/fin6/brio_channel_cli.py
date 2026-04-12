
#!/usr/bin/env python3
"""
CLI wrapper around the uploaded fly_x_detector.py module.

This gives you the "background", "calibrate", "detect", and "live" modes for the
Logitech Brio /dev/video8 path.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    from .camera_sources import BrioCamera, BrioConfig, capture_background_image
    from .fly_x_detector import (
        click_two_points,
        estimate_channel_crop_from_background,
        load_calibration_data,
        process_fly_detection,
        save_calibration,
    )
except ImportError:
    from camera_sources import BrioCamera, BrioConfig, capture_background_image
    from fly_x_detector import (
        click_two_points,
        estimate_channel_crop_from_background,
        load_calibration_data,
        process_fly_detection,
        save_calibration,
    )


def capture_brio_background(
    output_path: str | Path,
    device: str = "/dev/video8",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    frame_count: int = 15,
) -> str:
    with BrioCamera(BrioConfig(device=device, width=width, height=height, fps=fps)) as camera:
        bg = capture_background_image(camera, frame_count=frame_count, frame_sleep_s=0.03)
    ok = cv2.imwrite(str(output_path), bg)
    if not ok:
        raise IOError(f"Could not save background image to {output_path}")
    return str(Path(output_path).resolve())


def calibrate_channel(
    background_path: str | Path,
    calibration_path: str | Path,
    channel_mm: float = 111.0,
    crop_x_pad: Optional[int] = None,
    crop_above_px: Optional[int] = None,
    crop_below_px: Optional[int] = None,
) -> str:
    bg_color = cv2.imread(str(background_path), cv2.IMREAD_COLOR)
    if bg_color is None:
        raise FileNotFoundError(f"Could not read background image: {background_path}")

    left_pt, right_pt = click_two_points(bg_color, window_name="Click left point, then right point")
    bg_gray = cv2.cvtColor(bg_color, cv2.COLOR_BGR2GRAY)

    if crop_x_pad is None or crop_above_px is None or crop_below_px is None:
        est_x_pad, est_above, est_below, _ = estimate_channel_crop_from_background(
            bg_gray,
            left_pt,
            right_pt,
            crop_x_pad=crop_x_pad,
        )
        if crop_x_pad is None:
            crop_x_pad = est_x_pad
        if crop_above_px is None:
            crop_above_px = est_above
        if crop_below_px is None:
            crop_below_px = est_below

    save_calibration(
        calibration_path,
        left_pt=left_pt,
        right_pt=right_pt,
        channel_length_mm=channel_mm,
        crop_x_pad=crop_x_pad,
        crop_above_px=crop_above_px,
        crop_below_px=crop_below_px,
    )
    return str(Path(calibration_path).resolve())


def detect_once(
    background_path: str | Path,
    calibration_path: str | Path,
    frame_path: Optional[str | Path],
    device: str,
    width: int,
    height: int,
    fps: int,
    out_dir: str | Path,
    no_align: bool = False,
    score_thresh: int = 20,
    band_half_width: int = 35,
) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if frame_path is None:
        with BrioCamera(BrioConfig(device=device, width=width, height=height, fps=fps)) as camera:
            frame_bgr = camera.read()
    else:
        frame_bgr = cv2.imread(str(frame_path), cv2.IMREAD_COLOR)
        if frame_bgr is None:
            raise FileNotFoundError(f"Could not read frame image: {frame_path}")

    result, annotated, mask = process_fly_detection(
        background=str(background_path),
        frame=frame_bgr,
        calibration_path=str(calibration_path),
        score_thresh=score_thresh,
        band_half_width=band_half_width,
        no_align=no_align,
    )

    annotated_path = out_dir / "annotated_detection.png"
    mask_path = out_dir / "fly_mask.png"
    json_path = out_dir / "fly_results.json"

    cv2.imwrite(str(annotated_path), annotated)
    cv2.imwrite(str(mask_path), mask)

    result["annotated_image"] = str(annotated_path.resolve())
    result["mask_image"] = str(mask_path.resolve())
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result


def live_detect(
    background_path: str | Path,
    calibration_path: str | Path,
    device: str = "/dev/video8",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    no_align: bool = False,
    score_thresh: int = 20,
    band_half_width: int = 35,
) -> None:
    with BrioCamera(BrioConfig(device=device, width=width, height=height, fps=fps)) as camera:
        while True:
            frame_bgr = camera.read()
            result, annotated, mask = process_fly_detection(
                background=str(background_path),
                frame=frame_bgr,
                calibration_path=str(calibration_path),
                score_thresh=score_thresh,
                band_half_width=band_half_width,
                no_align=no_align,
            )

            preview = annotated.copy()
            status = f"count={result['count']} positions_mm={result['x_positions_mm']}"
            cv2.putText(preview, status, (12, max(24, preview.shape[0] - 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(preview, status, (12, max(24, preview.shape[0] - 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (0, 0, 255), 1, cv2.LINE_AA)

            cv2.imshow("Brio channel live detect", preview)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
    cv2.destroyAllWindows()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Brio channel detector with background/calibration/detect/live modes.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bg = sub.add_parser("background", help="Capture a median background image from the Brio camera.")
    p_bg.add_argument("-o", "--output", required=True)
    p_bg.add_argument("--device", default="/dev/video8")
    p_bg.add_argument("--width", type=int, default=1920)
    p_bg.add_argument("--height", type=int, default=1080)
    p_bg.add_argument("--fps", type=int, default=30)
    p_bg.add_argument("--frames", type=int, default=15)

    p_cal = sub.add_parser("calibrate", help="Calibrate the horizontal channel against a background image.")
    p_cal.add_argument("-b", "--background", required=True)
    p_cal.add_argument("-c", "--calibration", required=True)
    p_cal.add_argument("--channel-mm", type=float, default=111.0)
    p_cal.add_argument("--crop-x-pad", type=int, default=None)
    p_cal.add_argument("--crop-above-px", type=int, default=None)
    p_cal.add_argument("--crop-below-px", type=int, default=None)

    p_det = sub.add_parser("detect", help="Run one Brio channel detection.")
    p_det.add_argument("-b", "--background", required=True)
    p_det.add_argument("-c", "--calibration", required=True)
    p_det.add_argument("--frame", default=None, help="Optional existing image path. If omitted, a fresh Brio frame is captured.")
    p_det.add_argument("--device", default="/dev/video8")
    p_det.add_argument("--width", type=int, default=1920)
    p_det.add_argument("--height", type=int, default=1080)
    p_det.add_argument("--fps", type=int, default=30)
    p_det.add_argument("-o", "--output-dir", required=True)
    p_det.add_argument("--no-align", action="store_true")
    p_det.add_argument("--score-thresh", type=int, default=20)
    p_det.add_argument("--band-half-width", type=int, default=35)

    p_live = sub.add_parser("live", help="Run live Brio channel detection.")
    p_live.add_argument("-b", "--background", required=True)
    p_live.add_argument("-c", "--calibration", required=True)
    p_live.add_argument("--device", default="/dev/video8")
    p_live.add_argument("--width", type=int, default=1920)
    p_live.add_argument("--height", type=int, default=1080)
    p_live.add_argument("--fps", type=int, default=30)
    p_live.add_argument("--no-align", action="store_true")
    p_live.add_argument("--score-thresh", type=int, default=20)
    p_live.add_argument("--band-half-width", type=int, default=35)

    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.command == "background":
        out = capture_brio_background(
            output_path=args.output,
            device=args.device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            frame_count=args.frames,
        )
        print(out)
        return

    if args.command == "calibrate":
        path = calibrate_channel(
            background_path=args.background,
            calibration_path=args.calibration,
            channel_mm=args.channel_mm,
            crop_x_pad=args.crop_x_pad,
            crop_above_px=args.crop_above_px,
            crop_below_px=args.crop_below_px,
        )
        print(path)
        return

    if args.command == "detect":
        result = detect_once(
            background_path=args.background,
            calibration_path=args.calibration,
            frame_path=args.frame,
            device=args.device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            out_dir=args.output_dir,
            no_align=args.no_align,
            score_thresh=args.score_thresh,
            band_half_width=args.band_half_width,
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "live":
        live_detect(
            background_path=args.background,
            calibration_path=args.calibration,
            device=args.device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            no_align=args.no_align,
            score_thresh=args.score_thresh,
            band_half_width=args.band_half_width,
        )
        return

    parser.error("Unknown command.")


if __name__ == "__main__":
    main()
