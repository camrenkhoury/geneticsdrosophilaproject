#!/usr/bin/env python3
"""
fly_classifier.py
-----------------
Capture a chamber image, estimate chamber occupancy/count, and classify sex.

The chamber count is not produced by the YOLO sex model. It is a separate
foreground/blob heuristic used to decide whether the chamber looks empty,
contains a single fly, or likely contains multiple flies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

CODE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.config.project_paths import MODEL_PATH, TEMP_CLASS_IMAGE_DIR

try:
    from ultralytics import YOLO

    ULTRALYTICS_AVAILABLE = True
except ImportError:
    ULTRALYTICS_AVAILABLE = False

    class MockYOLO:
        def __init__(self, path: str):
            self.path = path

        def __call__(self, source: np.ndarray, verbose: bool = False):
            return []

    YOLO = MockYOLO


CLASSIFIER_MODEL_PATH = str(MODEL_PATH)
TEMP_IMAGE_DIR = str(TEMP_CLASS_IMAGE_DIR)
TEMP_IMAGE_PATH = os.path.join(TEMP_IMAGE_DIR, "temp.jpg")
LATEST_IMAGE_PATH = os.path.join(TEMP_IMAGE_DIR, "latest_classification.jpg")
LATEST_DEBUG_IMAGE_PATH = os.path.join(TEMP_IMAGE_DIR, "latest_error_detection.jpg")

UNCERTAIN_THRESHOLD = 0.70

HARD_ERROR_FLAGS = {
    "CAPTURE_FAILED",
    "LOAD_FAILED",
    "CLASSIFIER_FAILED",
}

BG_TOLERANCE = 65
OPEN_KERNEL_SIZE = 3
CLOSE_KERNEL_SIZE = 11
ERODE_KERNEL_SIZE = 11
ERODE_ITERATIONS = 9
SINGLE_FLY_MIN_FRAC = 0.001
SINGLE_FLY_MAX_AREA_PX = 40000

# Extra robustness for chamber count:
# - merge nearby significant contours before counting
# - count spatial groups, not contour fragments
# - only upgrade a single merged group to "2" when the combined area strongly
#   suggests multiple flies occupying the same cluster
GROUP_GAP_PX = 36
GROUP_GAP_FRAC = 0.015
DOUBLE_FLY_AREA_FACTOR = 1.85
DOUBLE_FLY_SEPARATION_FACTOR = 1.4

SETTINGS_PATH = REPO_ROOT / "vision" / "fin6" / ".fly_tracking_gui_settings.json"


if ULTRALYTICS_AVAILABLE:
    print("[fly_classifier] Loading classifier model ...")
    _classifier_model = YOLO(CLASSIFIER_MODEL_PATH)
    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    print("[fly_classifier] Ready.")
else:
    print("[fly_classifier] Running in simulation mode - no model loaded.")
    _classifier_model = None


def _safe_int(value: Any, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(default)
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _safe_float(value: Any, default: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _odd_kernel(value: Any, default: int) -> int:
    size = _safe_int(value, default, minimum=1, maximum=99)
    if size % 2 == 0:
        size = size + 1 if size < 99 else size - 1
    return max(1, size)


def _load_count_config() -> dict[str, float | int]:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    except Exception:
        saved = {}

    return {
        "corner_sample_px": _safe_int(saved.get("sexing_error_corner_sample_px"), 20, minimum=4, maximum=256),
        "bg_tolerance": _safe_int(saved.get("sexing_error_bg_tolerance"), BG_TOLERANCE, minimum=4, maximum=255),
        "open_kernel_size": _odd_kernel(saved.get("sexing_error_open_kernel_size"), OPEN_KERNEL_SIZE),
        "close_kernel_size": _odd_kernel(saved.get("sexing_error_close_kernel_size"), CLOSE_KERNEL_SIZE),
        "erode_kernel_size": _odd_kernel(saved.get("sexing_error_erode_kernel_size"), ERODE_KERNEL_SIZE),
        "erode_iterations": _safe_int(saved.get("sexing_error_erode_iterations"), ERODE_ITERATIONS, minimum=0, maximum=50),
        "single_fly_min_frac": _safe_float(saved.get("sexing_error_single_fly_min_frac"), SINGLE_FLY_MIN_FRAC, minimum=0.0, maximum=0.25),
        "single_fly_max_area_px": _safe_float(saved.get("sexing_error_single_fly_max_area_px"), SINGLE_FLY_MAX_AREA_PX, minimum=10.0),
        "group_gap_px": _safe_int(saved.get("sexing_error_group_gap_px"), GROUP_GAP_PX, minimum=0, maximum=300),
        "group_gap_frac": _safe_float(saved.get("sexing_error_group_gap_frac"), GROUP_GAP_FRAC, minimum=0.0, maximum=0.25),
        "double_fly_area_factor": _safe_float(saved.get("sexing_error_double_fly_area_factor"), DOUBLE_FLY_AREA_FACTOR, minimum=1.05, maximum=4.0),
        "double_fly_separation_factor": _safe_float(
            saved.get("sexing_error_double_fly_separation_factor"),
            DOUBLE_FLY_SEPARATION_FACTOR,
            minimum=0.5,
            maximum=4.0,
        ),
    }


def _capture_image() -> bool:
    """Capture a single image to TEMP_IMAGE_PATH."""
    if not ULTRALYTICS_AVAILABLE:
        print("Simulated image capture.")
        return True

    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    command = [
        "/usr/bin/rpicam-still",
        "--output",
        TEMP_IMAGE_PATH,
        "--nopreview",
        "-n",
    ]
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    except Exception:
        saved = {}
    camera_index = int(saved.get("sexing_camera_index_var", 0) or 0)
    command.extend(["--camera", str(camera_index)])
    result = subprocess.run(command, capture_output=True, text=True)
    print(f"returncode: {result.returncode}")
    print(f"stderr: {result.stderr}")
    return result.returncode == 0


def _subtract_background(bgr_img: np.ndarray, *, sample_size: int, tolerance: int) -> np.ndarray:
    corner_patch = bgr_img[:sample_size, :sample_size]
    if corner_patch.size == 0:
        return np.zeros(bgr_img.shape[:2], dtype=np.uint8)
    bg_color = np.median(corner_patch.reshape(-1, 3), axis=0)
    diff = np.abs(bgr_img.astype(np.int32) - bg_color.astype(np.int32))
    dist = np.max(diff, axis=2)
    return np.where(dist > tolerance, 255, 0).astype(np.uint8)


def _clean_mask(mask: np.ndarray, *, open_kernel_size: int, close_kernel_size: int, erode_kernel_size: int, erode_iterations: int) -> np.ndarray:
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_kernel_size, open_kernel_size))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel_size, close_kernel_size))
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode_kernel_size, erode_kernel_size))
    cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k, iterations=2)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, close_k, iterations=2)
    if erode_iterations > 0:
        cleaned = cv2.erode(cleaned, erode_k, iterations=erode_iterations)
    return cleaned


def _boxes_close(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int], gap_px: int) -> bool:
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    return not (
        ax1 + gap_px < bx0
        or bx1 + gap_px < ax0
        or ay1 + gap_px < by0
        or by1 + gap_px < ay0
    )


def _group_contours(contours: list[dict[str, Any]], gap_px: int) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for contour_info in contours:
        new_group = {
            "members": [contour_info],
            "box": contour_info["box"],
        }
        merged = True
        while merged:
            merged = False
            next_groups: list[dict[str, Any]] = []
            for group in groups:
                if _boxes_close(group["box"], new_group["box"], gap_px):
                    merged = True
                    gx0, gy0, gx1, gy1 = group["box"]
                    nx0, ny0, nx1, ny1 = new_group["box"]
                    new_group["members"].extend(group["members"])
                    new_group["box"] = (min(gx0, nx0), min(gy0, ny0), max(gx1, nx1), max(gy1, ny1))
                else:
                    next_groups.append(group)
            groups = next_groups
        groups.append(new_group)
    return groups


def _write_debug_image(image_bgr: np.ndarray | None) -> str | None:
    if image_bgr is None:
        return None
    try:
        os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
        ok, encoded = cv2.imencode(".jpg", image_bgr)
        if not ok:
            return None
        Path(LATEST_DEBUG_IMAGE_PATH).write_bytes(encoded.tobytes())
        return LATEST_DEBUG_IMAGE_PATH
    except Exception:
        return None


def _count_flies(bgr_img: np.ndarray, *, debug: bool = False) -> dict[str, Any]:
    config = _load_count_config()
    height, width = bgr_img.shape[:2]
    image_area = float(max(1, height * width))
    min_area = float(config["single_fly_min_frac"]) * image_area
    max_area = max(float(min_area) + 1.0, float(config["single_fly_max_area_px"]))
    gap_px = max(int(config["group_gap_px"]), int(round(min(height, width) * float(config["group_gap_frac"]))))
    separation_threshold = max(8.0, gap_px * float(config["double_fly_separation_factor"]))

    mask = _subtract_background(
        bgr_img,
        sample_size=int(config["corner_sample_px"]),
        tolerance=int(config["bg_tolerance"]),
    )
    mask = _clean_mask(
        mask,
        open_kernel_size=int(config["open_kernel_size"]),
        close_kernel_size=int(config["close_kernel_size"]),
        erode_kernel_size=int(config["erode_kernel_size"]),
        erode_iterations=int(config["erode_iterations"]),
    )
    contours_info = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = contours_info[0] if len(contours_info) == 2 else contours_info[1]

    ignored_contours: list[dict[str, Any]] = []
    significant_contours: list[dict[str, Any]] = []
    largest_area = 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        largest_area = max(largest_area, area)
        x, y, w, h = cv2.boundingRect(contour)
        info = {
            "contour": contour,
            "area": area,
            "box": (int(x), int(y), int(x + w), int(y + h)),
            "center": (float(x + w / 2.0), float(y + h / 2.0)),
        }
        if area < min_area:
            ignored_contours.append(info)
        else:
            significant_contours.append(info)

    groups = _group_contours(significant_contours, gap_px=gap_px)

    counted_groups: list[dict[str, Any]] = []
    total_count = 0
    for group in groups:
        members = group["members"]
        total_area = float(sum(float(member["area"]) for member in members))
        max_separation = 0.0
        for index, member in enumerate(members):
            ax, ay = member["center"]
            for other in members[index + 1 :]:
                bx, by = other["center"]
                max_separation = max(max_separation, float(np.hypot(ax - bx, ay - by)))

        group_count = 1
        reason = "single_group"
        if total_area >= max_area * float(config["double_fly_area_factor"]):
            group_count = 2
            reason = "oversized_group"

        total_count += group_count
        counted_groups.append(
            {
                "members": members,
                "box": group["box"],
                "area": total_area,
                "count": group_count,
                "reason": reason,
                "max_separation": max_separation,
            }
        )

    # The automation only needs a robust empty / single / multiple decision.
    # Count spatial groups first, then only allow one merged cluster to upgrade
    # itself to 2. This avoids the common false-double case where one fly gets
    # split into two contour fragments by the mask.
    total_count = min(total_count, max(0, len(counted_groups)))
    if len(counted_groups) == 1 and int(counted_groups[0]["count"]) >= 2:
        total_count = 2
    elif len(counted_groups) >= 2:
        total_count = min(len(counted_groups), 2)

    detail = (
        f"count={total_count} significant_contours={len(significant_contours)} groups={len(counted_groups)} "
        f"largest_area={largest_area:.1f} min_area={min_area:.1f} max_area={max_area:.1f} "
        f"gap_px={gap_px} separation_threshold={separation_threshold:.1f}"
    )

    debug_image = None
    if debug:
        debug_image = bgr_img.copy()
        for contour_info in ignored_contours:
            cv2.drawContours(debug_image, [contour_info["contour"]], -1, (120, 120, 120), 1)
        for index, group in enumerate(counted_groups, start=1):
            x0, y0, x1, y1 = group["box"]
            color = (0, 220, 0) if int(group["count"]) == 1 else (0, 140, 255)
            cv2.rectangle(debug_image, (x0, y0), (x1, y1), color, 2)
            for member in group["members"]:
                cv2.drawContours(debug_image, [member["contour"]], -1, color, 1)
            label = f"G{index}:{int(group['count'])} A{int(group['area'])}"
            cv2.putText(debug_image, label, (x0, max(20, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        cv2.putText(
            debug_image,
            f"Count: {total_count}",
            (20, 36),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2,
        )

    return {
        "count": int(total_count),
        "detail": detail,
        "errors": [],
        "largest_area": largest_area,
        "mask": mask,
        "debug_image": debug_image,
        "group_count": len(counted_groups),
    }


def classify_fly() -> dict[str, Any]:
    """
    Capture an image, estimate chamber count, classify sex, and return results.

    Returns:
        {
            "count": int,
            "class": "male" | "female" | "UNCERTAIN",
            "confidence": float,
            "errors": list[str],
            "image_path": str | None,
            "debug_image_path": str | None,
            "count_detail": str,
        }
    """
    errors: list[str] = []
    cls_out = "UNCERTAIN"
    confidence = 0.0
    fly_count = 0
    latest_image_path: str | None = None
    debug_image_path: str | None = None
    count_detail = ""

    if not _capture_image():
        errors.append("CAPTURE_FAILED")
        return {
            "count": 0,
            "class": "UNCERTAIN",
            "confidence": 0.0,
            "errors": errors,
            "image_path": None,
            "debug_image_path": None,
            "count_detail": "",
        }

    if not ULTRALYTICS_AVAILABLE:
        image = None
    else:
        try:
            data = np.fromfile(TEMP_IMAGE_PATH, dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("imdecode returned None")
            try:
                shutil.copyfile(TEMP_IMAGE_PATH, LATEST_IMAGE_PATH)
                latest_image_path = LATEST_IMAGE_PATH
            except Exception:
                latest_image_path = None
        except Exception:
            errors.append("LOAD_FAILED")
            _cleanup()
            return {
                "count": 0,
                "class": "UNCERTAIN",
                "confidence": 0.0,
                "errors": errors,
                "image_path": None,
                "debug_image_path": None,
                "count_detail": "",
            }

    if ULTRALYTICS_AVAILABLE and image is not None:
        try:
            count_info = _count_flies(image, debug=True)
            fly_count = int(count_info.get("count", 0) or 0)
            count_detail = str(count_info.get("detail", "") or "")
            debug_image_path = _write_debug_image(count_info.get("debug_image"))
            errors.extend(str(item) for item in count_info.get("errors", []) or [])
        except Exception:
            fly_count = 0
            errors.append("COUNT_FAILED")

    if not ULTRALYTICS_AVAILABLE:
        import random

        mock_classes = ["male", "female", "UNCERTAIN"]
        cls_out = random.choice(mock_classes)
        confidence = random.uniform(0.5, 0.95) if cls_out != "UNCERTAIN" else 0.0
        if cls_out == "UNCERTAIN":
            errors.append("SIMULATION_MODE")
        print(f"Simulated classification: {cls_out} with confidence {confidence:.2f}")
    else:
        cls_results = _classifier_model(image, verbose=False)

        if cls_results and cls_results[0].probs is not None:
            probs = cls_results[0].probs
            top1_idx = int(probs.top1)
            top1_conf = float(probs.top1conf)
            raw_label = cls_results[0].names[top1_idx].strip().lower()

            if "female" in raw_label:
                label = "female"
            elif "male" in raw_label:
                label = "male"
            else:
                label = "unknown"
                errors.append("UNKNOWN_CLASS")

            confidence = top1_conf

            if top1_conf < UNCERTAIN_THRESHOLD or label == "unknown":
                cls_out = "UNCERTAIN"
                errors.append(f"LOW_CONF_CLASS({top1_conf:.2f})")
            else:
                cls_out = label
        else:
            errors.append("CLASSIFIER_FAILED")

    if any(error in HARD_ERROR_FLAGS for error in errors):
        cls_out = "UNCERTAIN"
        confidence = 0.0

    _cleanup()

    return {
        "count": int(fly_count),
        "class": cls_out,
        "confidence": round(confidence, 4),
        "errors": errors,
        "image_path": latest_image_path,
        "debug_image_path": debug_image_path,
        "count_detail": count_detail,
    }


def _cleanup() -> None:
    """Delete the temporary captured image."""
    try:
        if os.path.exists(TEMP_IMAGE_PATH):
            os.remove(TEMP_IMAGE_PATH)
    except Exception:
        pass


if __name__ == "__main__":
    print("\nRunning test capture + classification...")
    result = classify_fly()
    print(f"\nResult: {result}")
