#!/usr/bin/env python3
"""
fly_classifier.py
-----------------
Capture a chamber image, extract morphology features, run YOLO sex
classification, then feed both into a pre-trained hybrid Random Forest
for the final sex prediction.

No GUI, no terminal output, no user interaction.
Drop fly_best_model.pkl in the same directory as this file.
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from scipy.fft import rfft, rfftfreq
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks
from skimage import measure

CODE_DIR  = Path(__file__).resolve().parent
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
        def __call__(self, source, verbose=False):
            return []
        def predict(self, source, imgsz=640, verbose=False):
            return []

    YOLO = MockYOLO

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CLASSIFIER_MODEL_PATH = str(MODEL_PATH)
TEMP_IMAGE_DIR        = str(TEMP_CLASS_IMAGE_DIR)
TEMP_IMAGE_PATH       = os.path.join(TEMP_IMAGE_DIR, "temp.jpg")
LATEST_IMAGE_PATH     = os.path.join(TEMP_IMAGE_DIR, "latest_classification.jpg")
LATEST_DEBUG_IMAGE_PATH = os.path.join(TEMP_IMAGE_DIR, "latest_error_detection.jpg")
RF_MODEL_PATH         = str(CODE_DIR / "fly_best_model.pkl")

SETTINGS_PATH = REPO_ROOT / "vision" / "fin6" / ".fly_tracking_gui_settings.json"

# ---------------------------------------------------------------------------
# Thresholds used by the occupancy counter (unchanged from original)
# ---------------------------------------------------------------------------
UNCERTAIN_THRESHOLD   = 0.70

HARD_ERROR_FLAGS = {
    "CAPTURE_FAILED",
    "LOAD_FAILED",
    "CLASSIFIER_FAILED",
    "RF_MODEL_FAILED",
}

BG_TOLERANCE         = 65
OPEN_KERNEL_SIZE     = 3
CLOSE_KERNEL_SIZE    = 11
ERODE_KERNEL_SIZE    = 11
ERODE_ITERATIONS     = 9
SINGLE_FLY_MIN_FRAC  = 0.001
SINGLE_FLY_MAX_AREA_PX = 40_000

GROUP_GAP_PX                = 36
GROUP_GAP_FRAC              = 0.015
DOUBLE_FLY_AREA_FACTOR      = 1.85
DOUBLE_FLY_SEPARATION_FACTOR = 1.4

# ---------------------------------------------------------------------------
# Feature-extraction parameters (mirrors feature_extractor.py defaults)
# ---------------------------------------------------------------------------
BG_BORDER_WIDTH       = 24
BG_THRESH             = 79
MORPH_KERNEL_SIZE     = 7
MORPH_ITERATIONS      = 2
BRIGHTNESS_PERCENTILE = 63
EARLY_HOLE_KERNEL     = 24
EARLY_HOLE_ITERS      = 3
HOLE_FILL_KERNEL      = 85
LEG_DIST_THRESH       = 8
LEG_MORPH_KERNEL      = 3
THICKNESS_CEILING     = 15
THICKNESS_ERODE_ITERS = 5
MIN_BLOB_AREA_FRAC    = 0.10
SKELETON_PRUNE        = True
SKELETON_BRANCH_THRESH = 70
NUM_SLICES            = 50
EYE_RED_THRESH        = 15.0

# ---------------------------------------------------------------------------
# Model loading (module-level, once)
# ---------------------------------------------------------------------------
if ULTRALYTICS_AVAILABLE:
    _yolo_model: Any = YOLO(CLASSIFIER_MODEL_PATH)
    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
else:
    _yolo_model = None

_rf_bundle: dict | None = None
if Path(RF_MODEL_PATH).exists():
    with open(RF_MODEL_PATH, "rb") as _f:
        _rf_bundle = pickle.load(_f)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _safe_int(value, default, *, minimum=0, maximum=None):
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(default)
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _safe_float(value, default, *, minimum=0.0, maximum=None):
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _odd_kernel(value, default):
    size = _safe_int(value, default, minimum=1, maximum=99)
    if size % 2 == 0:
        size = size + 1 if size < 99 else size - 1
    return max(1, size)


def _load_count_config() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    except Exception:
        saved = {}
    return {
        "corner_sample_px":           _safe_int(saved.get("sexing_error_corner_sample_px"), 20, minimum=4, maximum=256),
        "bg_tolerance":               _safe_int(saved.get("sexing_error_bg_tolerance"), BG_TOLERANCE, minimum=4, maximum=255),
        "open_kernel_size":           _odd_kernel(saved.get("sexing_error_open_kernel_size"), OPEN_KERNEL_SIZE),
        "close_kernel_size":          _odd_kernel(saved.get("sexing_error_close_kernel_size"), CLOSE_KERNEL_SIZE),
        "erode_kernel_size":          _odd_kernel(saved.get("sexing_error_erode_kernel_size"), ERODE_KERNEL_SIZE),
        "erode_iterations":           _safe_int(saved.get("sexing_error_erode_iterations"), ERODE_ITERATIONS, minimum=0, maximum=50),
        "single_fly_min_frac":        _safe_float(saved.get("sexing_error_single_fly_min_frac"), SINGLE_FLY_MIN_FRAC, minimum=0.0, maximum=0.25),
        "single_fly_max_area_px":     _safe_float(saved.get("sexing_error_single_fly_max_area_px"), SINGLE_FLY_MAX_AREA_PX, minimum=10.0),
        "group_gap_px":               _safe_int(saved.get("sexing_error_group_gap_px"), GROUP_GAP_PX, minimum=0, maximum=300),
        "group_gap_frac":             _safe_float(saved.get("sexing_error_group_gap_frac"), GROUP_GAP_FRAC, minimum=0.0, maximum=0.25),
        "double_fly_area_factor":     _safe_float(saved.get("sexing_error_double_fly_area_factor"), DOUBLE_FLY_AREA_FACTOR, minimum=1.05, maximum=4.0),
        "double_fly_separation_factor": _safe_float(saved.get("sexing_error_double_fly_separation_factor"), DOUBLE_FLY_SEPARATION_FACTOR, minimum=0.5, maximum=4.0),
    }


def _capture_image() -> bool:
    if not ULTRALYTICS_AVAILABLE:
        return True
    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    except Exception:
        saved = {}
    camera_index = int(saved.get("sexing_camera_index_var", 0) or 0)
    command = [
        "/usr/bin/rpicam-still",
        "--output", TEMP_IMAGE_PATH,
        "--nopreview", "-n",
        "--camera", str(camera_index),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0


def _cleanup() -> None:
    try:
        if os.path.exists(TEMP_IMAGE_PATH):
            os.remove(TEMP_IMAGE_PATH)
    except Exception:
        pass


# ===========================================================================
# Occupancy / fly-count helpers  (unchanged logic from original)
# ===========================================================================

def _subtract_background(bgr_img, *, sample_size, tolerance):
    corner_patch = bgr_img[:sample_size, :sample_size]
    if corner_patch.size == 0:
        return np.zeros(bgr_img.shape[:2], dtype=np.uint8)
    bg_color = np.median(corner_patch.reshape(-1, 3), axis=0)
    diff = np.abs(bgr_img.astype(np.int32) - bg_color.astype(np.int32))
    dist = np.max(diff, axis=2)
    return np.where(dist > tolerance, 255, 0).astype(np.uint8)


def _clean_mask(mask, *, open_kernel_size, close_kernel_size, erode_kernel_size, erode_iterations):
    open_k  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_kernel_size,  open_kernel_size))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel_size, close_kernel_size))
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode_kernel_size, erode_kernel_size))
    cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  open_k,  iterations=2)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, close_k, iterations=2)
    if erode_iterations > 0:
        cleaned = cv2.erode(cleaned, erode_k, iterations=erode_iterations)
    return cleaned


def _boxes_close(box_a, box_b, gap_px):
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    return not (ax1 + gap_px < bx0 or bx1 + gap_px < ax0
                or ay1 + gap_px < by0 or by1 + gap_px < ay0)


def _group_contours(contours, gap_px):
    groups = []
    for contour_info in contours:
        new_group = {"members": [contour_info], "box": contour_info["box"]}
        merged = True
        while merged:
            merged = False
            next_groups = []
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


def _count_flies(bgr_img) -> dict:
    config     = _load_count_config()
    height, width = bgr_img.shape[:2]
    image_area = float(max(1, height * width))
    min_area   = float(config["single_fly_min_frac"]) * image_area
    max_area   = max(float(min_area) + 1.0, float(config["single_fly_max_area_px"]))
    gap_px     = max(int(config["group_gap_px"]),
                     int(round(min(height, width) * float(config["group_gap_frac"]))))

    mask = _subtract_background(bgr_img,
                                 sample_size=int(config["corner_sample_px"]),
                                 tolerance=int(config["bg_tolerance"]))
    mask = _clean_mask(mask,
                       open_kernel_size=int(config["open_kernel_size"]),
                       close_kernel_size=int(config["close_kernel_size"]),
                       erode_kernel_size=int(config["erode_kernel_size"]),
                       erode_iterations=int(config["erode_iterations"]))

    contours_raw = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours     = contours_raw[0] if len(contours_raw) == 2 else contours_raw[1]

    significant = []
    largest_area = 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        largest_area = max(largest_area, area)
        x, y, w, h = cv2.boundingRect(contour)
        info = {
            "contour": contour,
            "area":    area,
            "box":     (int(x), int(y), int(x + w), int(y + h)),
            "center":  (float(x + w / 2.0), float(y + h / 2.0)),
        }
        if area >= min_area:
            significant.append(info)

    groups      = _group_contours(significant, gap_px=gap_px)
    total_count = 0
    counted_groups = []
    for group in groups:
        members    = group["members"]
        total_area = float(sum(m["area"] for m in members))
        group_count = 2 if total_area >= max_area * float(config["double_fly_area_factor"]) else 1
        total_count += group_count
        counted_groups.append({**group, "area": total_area, "count": group_count})

    total_count = min(total_count, max(0, len(counted_groups)))
    if len(counted_groups) == 1 and counted_groups[0]["count"] >= 2:
        total_count = 2
    elif len(counted_groups) >= 2:
        total_count = min(len(counted_groups), 2)

    detail = (
        f"count={total_count} significant_contours={len(significant)} "
        f"groups={len(counted_groups)} largest_area={largest_area:.1f} "
        f"min_area={min_area:.1f} max_area={max_area:.1f} gap_px={gap_px}"
    )
    return {"count": int(total_count), "detail": detail, "errors": []}


# ===========================================================================
# Feature extraction pipeline  (adapted from feature_extractor.py)
# ===========================================================================

def _split_fly_regions(fly_mask, num_slices=NUM_SLICES):
    """Returns (head_mask, thorax_mask, abdomen_mask)."""
    labeled = measure.label(fly_mask)
    props   = measure.regionprops(labeled)
    if not props:
        blank = np.zeros(fly_mask.shape, dtype=np.uint8)
        return blank, blank, blank

    coords  = props[0].coords
    y_coords = coords[:, 0]
    min_y, max_y = y_coords.min(), y_coords.max()
    slice_edges  = np.linspace(min_y, max_y, num_slices + 1)

    slice_widths = []
    for i in range(num_slices):
        in_slice = (y_coords >= slice_edges[i]) & (y_coords < slice_edges[i + 1])
        sc = coords[in_slice]
        if len(sc) == 0:
            slice_widths.append(0)
        else:
            x_vals = sc[:, 1]
            slice_widths.append(x_vals.max() - x_vals.min())

    smooth = gaussian_filter1d(slice_widths, sigma=2)
    all_valleys, _ = find_peaks(-smooth)

    ant = all_valleys[(all_valleys > int(num_slices * 0.10)) & (all_valleys < int(num_slices * 0.40))]
    head_end = int(slice_edges[ant[np.argmin(smooth[ant])]]) if len(ant) > 0 else int(slice_edges[int(num_slices * 0.30)])

    post = all_valleys[(all_valleys > int(num_slices * 0.45)) & (all_valleys < int(num_slices * 0.80))]
    if len(post) > 1:
        post = post[1:]
        abdomen_start = int(slice_edges[post[np.argmin(smooth[post])]])
    elif len(post) == 1:
        abdomen_start = int(slice_edges[post[0]])
    else:
        abdomen_start = int(slice_edges[int(num_slices * 0.66)])

    head_mask    = np.zeros(fly_mask.shape, dtype=np.uint8)
    thorax_mask  = np.zeros(fly_mask.shape, dtype=np.uint8)
    abdomen_mask = np.zeros(fly_mask.shape, dtype=np.uint8)

    head_c    = coords[y_coords <= head_end]
    thorax_c  = coords[(y_coords > head_end) & (y_coords <= abdomen_start)]
    abdomen_c = coords[y_coords > abdomen_start]

    if len(head_c):    head_mask   [head_c[:, 0],    head_c[:, 1]]    = 255
    if len(thorax_c):  thorax_mask [thorax_c[:, 0],  thorax_c[:, 1]]  = 255
    if len(abdomen_c): abdomen_mask[abdomen_c[:, 0], abdomen_c[:, 1]] = 255

    return head_mask, thorax_mask, abdomen_mask


def _segment_fly(bgr_img: np.ndarray):
    """
    Run the full segmentation pipeline on a BGR image.
    Returns the cleaned, rotated (head-up) crop, mask, and region masks,
    or None on failure.
    """
    gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)

    # --- Coarse foreground mask ---
    border_px = np.concatenate([
        gray[:BG_BORDER_WIDTH, :].ravel(),
        gray[-BG_BORDER_WIDTH:, :].ravel(),
        gray[:, :BG_BORDER_WIDTH].ravel(),
        gray[:, -BG_BORDER_WIDTH:].ravel(),
    ])
    bg_mean = np.mean(border_px)
    fg_mask = (np.abs(gray.astype(float) - bg_mean) > BG_THRESH).astype(np.uint8) * 255

    # --- Morphological clean + largest blob ---
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE))
    cleaned = cv2.morphologyEx(fg_mask,  cv2.MORPH_CLOSE, k, iterations=MORPH_ITERATIONS)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN,  k, iterations=MORPH_ITERATIONS)
    n_lab, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned)
    if n_lab > 1:
        largest = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        fly_mask = (labels == largest).astype(np.uint8) * 255
    else:
        fly_mask = cleaned

    # --- Adaptive brightness filter ---
    masked_px = gray[fly_mask > 0]
    if len(masked_px) > 0:
        cutoff = np.percentile(masked_px, BRIGHTNESS_PERCENTILE)
        fly_mask[gray > cutoff] = 0

    # --- Early hole fill ---
    eh_k     = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (EARLY_HOLE_KERNEL, EARLY_HOLE_KERNEL))
    fly_mask = cv2.morphologyEx(fly_mask, cv2.MORPH_CLOSE, eh_k, iterations=EARLY_HOLE_ITERS)

    # --- Thickness-ceiling erosion (leg removal) ---
    dist      = cv2.distanceTransform(fly_mask, cv2.DIST_L2, 5)
    body_core = (dist >= THICKNESS_CEILING).astype(np.uint8)
    guard_k   = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    protected = cv2.dilate(body_core, guard_k, iterations=2)
    prot_mask = (protected > 0).astype(np.uint8) * 255
    working   = fly_mask.copy()
    for _ in range(THICKNESS_ERODE_ITERS):
        thin_now      = cv2.bitwise_and(working, cv2.bitwise_not(prot_mask))
        prot_nb       = cv2.dilate(prot_mask, guard_k, iterations=1)
        safe_thin     = cv2.bitwise_and(thin_now, prot_nb)
        prot_mask     = cv2.bitwise_or(prot_mask, safe_thin)
        removable     = cv2.bitwise_and(thin_now, cv2.bitwise_not(prot_nb))
        working       = cv2.bitwise_and(working, cv2.bitwise_not(removable))
    fly_mask = working

    # --- Skeleton pruning ---
    if SKELETON_PRUNE:
        from skimage.morphology import skeletonize
        skel        = skeletonize((fly_mask > 0).astype(bool)).astype(np.uint8)
        nb_k        = np.ones((3, 3), dtype=np.uint8); nb_k[1, 1] = 0
        nb_count    = cv2.filter2D(skel.astype(np.float32), -1, nb_k.astype(np.float32))
        endpoints   = (skel > 0) & (nb_count == 1)
        ep_coords   = np.argwhere(endpoints)
        remove_mask = np.zeros_like(fly_mask)
        for ey, ex in ep_coords:
            branch, cy, cx, visited = [], ey, ex, set()
            while True:
                if (cy, cx) in visited: break
                visited.add((cy, cx)); branch.append((cy, cx))
                if len(branch) > SKELETON_BRANCH_THRESH: break
                nbrs = [(cy + dy, cx + dx)
                        for dy in [-1, 0, 1] for dx in [-1, 0, 1]
                        if not (dy == 0 and dx == 0)
                        and 0 <= cy + dy < skel.shape[0]
                        and 0 <= cx + dx < skel.shape[1]
                        and skel[cy + dy, cx + dx] > 0
                        and (cy + dy, cx + dx) not in visited]
                if not nbrs or len(nbrs) > 1: break
                cy, cx = nbrs[0]
            if len(branch) <= SKELETON_BRANCH_THRESH:
                for py, px in branch:
                    y0 = max(0, py - LEG_DIST_THRESH); y1 = min(fly_mask.shape[0], py + LEG_DIST_THRESH + 1)
                    x0 = max(0, px - LEG_DIST_THRESH); x1 = min(fly_mask.shape[1], px + LEG_DIST_THRESH + 1)
                    remove_mask[y0:y1, x0:x1] = 255
        fly_mask = cv2.bitwise_and(fly_mask, cv2.bitwise_not(remove_mask))

    # --- Drop small blobs ---
    n_lab, labels, stats, _ = cv2.connectedComponentsWithStats(fly_mask)
    if n_lab > 2:
        fg_stats     = stats[1:]
        largest_area = float(np.max(fg_stats[:, cv2.CC_STAT_AREA]))
        area_thresh  = largest_area * MIN_BLOB_AREA_FRAC
        keep         = np.zeros_like(fly_mask)
        for idx in range(len(fg_stats)):
            if fg_stats[idx, cv2.CC_STAT_AREA] >= area_thresh:
                keep[labels == idx + 1] = 255
        fly_mask = keep

    # --- Hole fill ---
    hk       = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (HOLE_FILL_KERNEL, HOLE_FILL_KERNEL))
    fly_mask = cv2.morphologyEx(fly_mask, cv2.MORPH_CLOSE, hk, iterations=2)
    fly_mask = cv2.medianBlur(fly_mask, 3)

    # --- Rotate to vertical ---
    labeled_r = measure.label(fly_mask)
    props_r   = measure.regionprops(labeled_r)
    if not props_r:
        return None
    angle    = -props_r[0].orientation * 180 / np.pi
    center   = tuple(np.array(bgr_img.shape[1::-1]) / 2)
    rot_mat  = cv2.getRotationMatrix2D(center, angle, 1.0)
    rot_crop = cv2.warpAffine(bgr_img,   rot_mat, bgr_img.shape[1::-1], flags=cv2.INTER_CUBIC)
    rot_mask = cv2.warpAffine(fly_mask,  rot_mat, bgr_img.shape[1::-1], flags=cv2.INTER_NEAREST)

    # --- Orient head-up via compound-eye redness ---
    ys_all  = np.where(rot_mask > 0)[0]
    if len(ys_all) == 0:
        return None
    span    = max(1, ys_all.max() - ys_all.min())
    tip_px  = max(1, int(span * 0.15))

    def _tip_score(y_min, y_max):
        strip = rot_mask[y_min:y_max, :]
        if strip.max() == 0: return 0.0
        px = strip > 0
        r  = rot_crop[y_min:y_max, :, 2][px].astype(float)
        g  = rot_crop[y_min:y_max, :, 1][px].astype(float)
        b  = rot_crop[y_min:y_max, :, 0][px].astype(float)
        return float(np.mean(r - np.maximum(g, b)))

    if _tip_score(ys_all.max() - tip_px, ys_all.max()) > _tip_score(ys_all.min(), ys_all.min() + tip_px):
        rot_crop = cv2.rotate(rot_crop, cv2.ROTATE_180)
        rot_mask = cv2.rotate(rot_mask, cv2.ROTATE_180)

    head_mask, thorax_mask, abdomen_mask = _split_fly_regions(rot_mask)

    return {
        "crop":    rot_crop,
        "mask":    rot_mask,
        "head":    head_mask,
        "thorax":  thorax_mask,
        "abdomen": abdomen_mask,
    }


def _extract_features(seg: dict) -> dict[str, float]:
    """
    Compute all morphology features from the segmented fly.
    Returns a flat dict of float values matching the training feature set.
    """
    rot_crop     = seg["crop"]
    rot_mask     = seg["mask"]
    head_mask    = seg["head"]
    thorax_mask  = seg["thorax"]
    abdomen_mask = seg["abdomen"]

    gray_rot = cv2.cvtColor(rot_crop, cv2.COLOR_BGR2GRAY)

    body_ys     = np.where(rot_mask > 0)[0]
    body_length = float(body_ys.max() - body_ys.min()) if len(body_ys) > 1 else 1.0

    def _px(m):     return float(np.sum(m > 0))
    def _darkness(m):
        roi = m > 0
        return float(255 - np.mean(gray_rot[roi])) if roi.any() else 0.0
    def _seg_len(m):
        ys = np.where(m > 0)[0]
        return float(ys.max() - ys.min()) / body_length if len(ys) > 1 else 0.0

    head_len_norm    = _seg_len(head_mask)
    thorax_len_norm  = _seg_len(thorax_mask)
    abdomen_len_norm = _seg_len(abdomen_mask)

    t_ys = np.where(thorax_mask > 0)[0];  t_xs = np.where(thorax_mask > 0)[1]
    thorax_width_norm = (float(t_xs.max() - t_xs.min()) / body_length if len(t_xs) > 1 else 0.0)
    thorax_aspect     = thorax_width_norm / thorax_len_norm if thorax_len_norm > 0 else 0.0

    a_ys = np.where(abdomen_mask > 0)[0]; a_xs = np.where(abdomen_mask > 0)[1]
    abdomen_width_norm = (float(a_xs.max() - a_xs.min()) / body_length if len(a_xs) > 1 else 0.0)
    abdomen_aspect     = abdomen_width_norm / abdomen_len_norm if abdomen_len_norm > 0 else 0.0

    tip_band = max(1, int((a_ys.max() - a_ys.min()) * 0.10))
    tip_mask = abdomen_mask[a_ys.max() - tip_band: a_ys.max(), :]
    tip_xs   = np.where(tip_mask > 0)[1]
    abdomen_tip_width_norm = (float(tip_xs.max() - tip_xs.min()) / body_length if len(tip_xs) > 1 else 0.0)

    head_darkness    = _darkness(head_mask)
    thorax_darkness  = _darkness(thorax_mask)
    abdomen_darkness = _darkness(abdomen_mask)

    # Abdominal intensity profile
    abd_profile = []
    for row_y in range(int(a_ys.min()), int(a_ys.max())):
        row_px = abdomen_mask[row_y, :] > 0
        if row_px.any():
            abd_profile.append(float(np.mean(gray_rot[row_y, row_px])))

    if len(abd_profile) > 8:
        profile_arr  = np.array(abd_profile) - np.mean(abd_profile)
        fft_mag      = np.abs(rfft(profile_arr))
        freqs        = rfftfreq(len(profile_arr))
        band_mask    = (freqs > 0.04) & (freqs < 0.40)
        striation_score = float(np.max(fft_mag[band_mask])) / body_length if band_mask.any() else 0.0
    else:
        striation_score = 0.0

    # Taper rate
    abd_row_widths = []
    for row_y in range(int(a_ys.min()), int(a_ys.max()) + 1):
        row_px = abdomen_mask[row_y, :] > 0
        if row_px.any():
            xs = np.where(row_px)[0]
            abd_row_widths.append((row_y, float(xs.max() - xs.min())))
    if len(abd_row_widths) > 4:
        rys  = np.array([r[0] for r in abd_row_widths], dtype=float)
        rwds = np.array([r[1] for r in abd_row_widths], dtype=float)
        rys_norm = (rys - rys.min()) / max(1.0, rys.max() - rys.min())
        taper_rate = float(np.polyfit(rys_norm, rwds / body_length, 1)[0])
    else:
        taper_rate = 0.0

    abd_width_peak_pos = (float(np.argmax([r[1] for r in abd_row_widths])) / max(1, len(abd_row_widths) - 1)
                          if len(abd_row_widths) > 2 else 0.5)

    # Abdomen convexity
    abd_contours, _ = cv2.findContours(abdomen_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if abd_contours:
        abd_hull_area = cv2.contourArea(cv2.convexHull(abd_contours[0]))
        abd_convexity = float(np.sum(abdomen_mask > 0)) / max(1.0, abd_hull_area)
    else:
        abd_convexity = 0.0

    # Posterior dark fraction
    mean_body_brightness = float(np.mean(gray_rot[rot_mask > 0])) if np.any(rot_mask > 0) else 128.0
    post_band  = max(1, int((a_ys.max() - a_ys.min()) * 0.30))
    post_mask  = abdomen_mask[a_ys.max() - post_band: a_ys.max() + 1, :]
    post_gray  = gray_rot    [a_ys.max() - post_band: a_ys.max() + 1, :]
    post_px    = post_mask > 0
    posterior_dark_frac = float(np.mean(post_gray[post_px] < mean_body_brightness)) if post_px.any() else 0.0

    # Dark band count
    if len(abd_profile) > 8:
        inv = 255.0 - np.array(abd_profile)
        inv -= inv.min()
        peaks, _ = find_peaks(inv, prominence=inv.max() * 0.10, distance=max(1, len(inv) // 12))
        dark_band_count = int(len(peaks))
    else:
        dark_band_count = 0

    # Thorax stripe contrast
    if len(np.where(thorax_mask > 0)[0]) > 0:
        t_col_means = [float(np.mean(gray_rot[:, cx][thorax_mask[:, cx] > 0]))
                       for cx in range(thorax_mask.shape[1])
                       if (thorax_mask[:, cx] > 0).any()]
        thorax_stripe_contrast = float(np.std(t_col_means)) if len(t_col_means) > 1 else 0.0
    else:
        thorax_stripe_contrast = 0.0

    h_ys = np.where(head_mask > 0)[0]; h_xs = np.where(head_mask > 0)[1]
    head_width_norm = float(h_xs.max() - h_xs.min()) / body_length if len(h_xs) > 1 else 0.0
    head_aspect     = head_width_norm / head_len_norm if head_len_norm > 0 else 0.0

    # Eye metrics
    if len(h_ys) > 0:
        h_r = rot_crop[:, :, 2][head_mask > 0].astype(float)
        h_g = rot_crop[:, :, 1][head_mask > 0].astype(float)
        h_b = rot_crop[:, :, 0][head_mask > 0].astype(float)
        red_scores    = h_r - np.maximum(h_g, h_b)
        eye_red_score = float(np.percentile(red_scores, 90))
        eye_frac      = float(np.mean(red_scores > EYE_RED_THRESH))
    else:
        eye_red_score = 0.0
        eye_frac      = 0.0

    thorax_abdomen_ratio       = thorax_len_norm / abdomen_len_norm if abdomen_len_norm > 0 else 0.0
    thorax_abdomen_width_ratio = thorax_width_norm / abdomen_width_norm if abdomen_width_norm > 0 else 0.0

    all_ys = np.where(rot_mask > 0)[0]
    centroid_pos = ((float(np.mean(all_ys)) - all_ys.min()) / body_length if body_length > 0 else 0.5)

    # Abdomen surface roughness
    if np.any(abdomen_mask > 0):
        gf   = gray_rot.astype(np.float32)
        lm   = cv2.boxFilter(gf, -1, (5, 5))
        lsq  = cv2.boxFilter(gf ** 2, -1, (5, 5))
        lvar = np.sqrt(np.maximum(lsq - lm ** 2, 0))
        abd_roughness = float(np.mean(lvar[abdomen_mask > 0]))
    else:
        abd_roughness = 0.0

    # Thorax texture
    if np.any(thorax_mask > 0):
        gf   = gray_rot.astype(np.float32)
        lm   = cv2.boxFilter(gf, -1, (5, 5))
        lsq  = cv2.boxFilter(gf ** 2, -1, (5, 5))
        lvar = np.sqrt(np.maximum(lsq - lm ** 2, 0))
        thorax_texture = float(np.mean(lvar[thorax_mask > 0]))
    else:
        thorax_texture = 0.0

    # Body aspect
    all_xs = np.where(rot_mask > 0)[1]
    body_max_width    = float(all_xs.max() - all_xs.min()) if len(all_xs) > 1 else 1.0
    body_aspect_ratio = body_length / max(1.0, body_max_width)

    # Asymmetry
    labeled_sym = measure.label(rot_mask)
    props_sym   = measure.regionprops(labeled_sym)
    if props_sym:
        sym_coords = props_sym[0].coords
        sym_ys = sym_coords[:, 0]; sym_xs = sym_coords[:, 1]
        asym_scores = []
        for sy in np.unique(sym_ys):
            row_xs = sym_xs[sym_ys == sy].astype(float)
            row_cx = (row_xs.min() + row_xs.max()) / 2.0
            total  = (row_xs.max() - row_xs.min())
            if total > 0:
                asym_scores.append(abs((row_cx - row_xs.min()) - (row_xs.max() - row_cx)) / total)
        asymmetry = float(np.mean(asym_scores)) if asym_scores else 0.0
    else:
        asymmetry = 0.0

    return {
        "body_length_px":             round(body_length,              1),
        "body_aspect_ratio":          round(body_aspect_ratio,        3),
        "head_len_norm":              round(head_len_norm,            3),
        "thorax_len_norm":            round(thorax_len_norm,          3),
        "abdomen_len_norm":           round(abdomen_len_norm,         3),
        "thorax_abdomen_ratio":       round(thorax_abdomen_ratio,     3),
        "head_aspect":                round(head_aspect,              3),
        "thorax_aspect":              round(thorax_aspect,            3),
        "abdomen_aspect":             round(abdomen_aspect,           3),
        "abdomen_tip_width_norm":     round(abdomen_tip_width_norm,   3),
        "taper_rate":                 round(taper_rate,               4),
        "abd_width_peak_pos":         round(abd_width_peak_pos,       3),
        "abd_convexity":              round(abd_convexity,            3),
        "thorax_abdomen_width_ratio": round(thorax_abdomen_width_ratio, 3),
        "centroid_pos":               round(centroid_pos,             3),
        "head_darkness":              round(head_darkness,            1),
        "thorax_darkness":            round(thorax_darkness,          1),
        "abdomen_darkness":           round(abdomen_darkness,         1),
        "posterior_dark_frac":        round(posterior_dark_frac,      3),
        "striation_score":            round(striation_score,          4),
        "dark_band_count":            float(dark_band_count),
        "abd_roughness":              round(abd_roughness,            2),
        "thorax_texture":             round(thorax_texture,           2),
        "thorax_stripe_contrast":     round(thorax_stripe_contrast,   2),
        "eye_frac":                   round(eye_frac,                 4),
        "eye_red_score":              round(eye_red_score,            2),
        "asymmetry":                  round(asymmetry,                3),
    }


# ===========================================================================
# Hybrid Random Forest inference
# ===========================================================================

def _run_rf_model(morph_features: dict, yolo_class: int, yolo_conf: float) -> tuple[str, float]:
    """
    Feed morphology features + YOLO class/confidence into the loaded RF model.
    Returns (predicted_label, confidence_float).
    """
    if _rf_bundle is None:
        return "UNCERTAIN", 0.0

    model   = _rf_bundle["model"]
    le      = _rf_bundle["label_encoder"]
    f_keys  = _rf_bundle["feature_keys"]

    # Build feature vector in the exact order the model was trained on
    row = []
    morph_with_yolo = {**morph_features, "yolo_class": float(yolo_class), "yolo_confidence": float(yolo_conf)}
    for k in f_keys:
        try:
            row.append(float(morph_with_yolo.get(k, 0.0)))
        except (ValueError, TypeError):
            row.append(0.0)

    X       = np.array([row])
    pred_idx = int(model.predict(X)[0])
    proba   = model.predict_proba(X)[0]
    conf    = float(proba[pred_idx])
    label   = str(le.inverse_transform([pred_idx])[0])
    return label, conf


# ===========================================================================
# Public API
# ===========================================================================

def classify_fly() -> dict[str, Any]:
    """
    Capture an image, count flies, extract morphology features, run the YOLO
    classifier, feed both into the hybrid Random Forest, and return the result.

    Returns
    -------
    {
        "count":           int,
        "class":           "male" | "female" | "UNCERTAIN",
        "confidence":      float,
        "errors":          list[str],
        "image_path":      str | None,
        "debug_image_path": str | None,
        "count_detail":    str,
    }
    """
    errors: list[str]        = []
    cls_out                  = "UNCERTAIN"
    confidence               = 0.0
    fly_count                = 0
    latest_image_path        = None
    debug_image_path         = None
    count_detail             = ""

    # ---- Capture ----
    if not _capture_image():
        errors.append("CAPTURE_FAILED")
        return _error_result(errors)

    # ---- Load image ----
    if not ULTRALYTICS_AVAILABLE:
        image = None
    else:
        try:
            data  = np.fromfile(TEMP_IMAGE_PATH, dtype=np.uint8)
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
            return _error_result(errors)

    # ---- Occupancy count ----
    if ULTRALYTICS_AVAILABLE and image is not None:
        try:
            count_info  = _count_flies(image)
            fly_count   = int(count_info.get("count", 0) or 0)
            count_detail = str(count_info.get("detail", "") or "")
            errors.extend(str(e) for e in count_info.get("errors", []) or [])
        except Exception:
            fly_count = 0
            errors.append("COUNT_FAILED")

    # ---- Simulation mode ----
    if not ULTRALYTICS_AVAILABLE:
        import random
        cls_out    = random.choice(["male", "female", "UNCERTAIN"])
        confidence = random.uniform(0.5, 0.95) if cls_out != "UNCERTAIN" else 0.0
        if cls_out == "UNCERTAIN":
            errors.append("SIMULATION_MODE")
        _cleanup()
        return {
            "count": fly_count, "class": cls_out, "confidence": round(confidence, 4),
            "errors": errors, "image_path": latest_image_path,
            "debug_image_path": debug_image_path, "count_detail": count_detail,
        }

    # ---- YOLO inference ----
    yolo_class_int  = 0
    yolo_conf_float = 0.0
    try:
        cls_results = _yolo_model(image, verbose=False)
        if cls_results and cls_results[0].probs is not None:
            probs          = cls_results[0].probs
            top1_idx       = int(probs.top1)
            top1_conf      = float(probs.top1conf)
            raw_label      = cls_results[0].names[top1_idx].strip().lower()
            yolo_conf_float = top1_conf
            if "female" in raw_label:
                yolo_class_int = 0
            elif "male" in raw_label:
                yolo_class_int = 1
            else:
                errors.append("UNKNOWN_YOLO_CLASS")
        else:
            errors.append("CLASSIFIER_FAILED")
    except Exception:
        errors.append("CLASSIFIER_FAILED")

    # ---- Morphology feature extraction ----
    morph_features: dict = {}
    seg = None
    if "CLASSIFIER_FAILED" not in errors:
        try:
            seg = _segment_fly(image)
            if seg is not None:
                morph_features = _extract_features(seg)
            else:
                errors.append("SEGMENTATION_FAILED")
        except Exception:
            errors.append("SEGMENTATION_FAILED")

    # ---- Hybrid Random Forest ----
    if _rf_bundle is None:
        errors.append("RF_MODEL_FAILED")
    elif morph_features:
        try:
            rf_label, rf_conf = _run_rf_model(morph_features, yolo_class_int, yolo_conf_float)
            cls_out   = rf_label
            confidence = rf_conf
            if rf_conf < UNCERTAIN_THRESHOLD:
                cls_out = "UNCERTAIN"
                errors.append(f"LOW_CONF_RF({rf_conf:.2f})")
        except Exception:
            errors.append("RF_INFERENCE_FAILED")
            cls_out = "UNCERTAIN"
    else:
        # Fall back to raw YOLO if segmentation failed
        if yolo_conf_float >= UNCERTAIN_THRESHOLD and "CLASSIFIER_FAILED" not in errors:
            cls_out    = "female" if yolo_class_int == 0 else "male"
            confidence = yolo_conf_float
            errors.append("RF_SKIPPED_NO_MORPH")
        else:
            cls_out = "UNCERTAIN"

    # Hard errors always override
    if any(e in HARD_ERROR_FLAGS for e in errors):
        cls_out    = "UNCERTAIN"
        confidence = 0.0

    _cleanup()

    return {
        "count":            int(fly_count),
        "class":            cls_out,
        "confidence":       round(confidence, 4),
        "errors":           errors,
        "image_path":       latest_image_path,
        "debug_image_path": debug_image_path,
        "count_detail":     count_detail,
    }


def _error_result(errors: list[str]) -> dict[str, Any]:
    return {
        "count": 0, "class": "UNCERTAIN", "confidence": 0.0,
        "errors": errors, "image_path": None,
        "debug_image_path": None, "count_detail": "",
    }


if __name__ == "__main__":
    result = classify_fly()
    print(result)
