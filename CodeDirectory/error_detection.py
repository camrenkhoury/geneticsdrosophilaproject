#!/usr/bin/env python3
"""
error_detection.py
------------------
Counts fruit flies in controlled microscopy images with a near-white
(slightly purple-tinted) background. Pulls images from a 'flyblobs' folder
and outputs per-image fly counts.

Requirements:
    pip install opencv-python numpy

Usage:
    python error_detection.py                      # processes all images in ./flyblobs
    python error_detection.py --debug              # saves debug visualisation images
    python error_detection.py --input path/to/dir  # custom folder
"""

import cv2
import numpy as np
import os
import argparse
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────

# How different a pixel must be from the sampled corner colour to be
# considered foreground. Raise if background is leaking, lower if fly
# pixels are being eaten.
BG_TOLERANCE = 50

# Background subtraction: pixels ABOVE this LAB lightness value are considered
# background. Tune aggressively low since background is near-perfect white.
BG_LIGHTNESS_THRESHOLD = 172

# Purple-tint background filter: LAB B channel, neutral = 128.
# Purple-tinted whites have B < 128. Any pixel below this is treated as
# background regardless of lightness. Raise toward 128 if fly pixels are
# being eaten, lower toward 110 if purple background is leaking through.
PURPLE_B_THRESHOLD = 0

# Morphology kernel sizes
OPEN_KERNEL_SIZE  = 3   # removes tiny noise specks
CLOSE_KERNEL_SIZE = 11   # fills gaps within a fly body (smaller = less merging)

# Hard erosion to separate touching/close blobs before watershed
ERODE_KERNEL_SIZE = 11
ERODE_ITERATIONS  = 7   # increase to 4-5 if close flies still merge

# Watershed distance transform threshold — lower = more aggressive peak finding
# 0.3 is more aggressive than default 0.4, better for close flies
WATERSHED_DIST_THRESHOLD = 0.3

# Fly size bounds as a fraction of total image area.
SINGLE_FLY_MIN_FRAC = 0.001   # ignore blobs smaller than 0.3% of frame (noise)
SINGLE_FLY_MAX_FRAC = 0.12    # single fly ceiling before watershed kicks in

# Watershed triggers if blob area exceeds this multiple of median single-fly area
SPLIT_RATIO_THRESHOLD = 1.3

# Supported image extensions
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}


# ── Core pipeline ──────────────────────────────────────────────────────────────

def subtract_background(bgr_img):
    """
    Sample the corner to get the actual background colour, then remove any
    pixel within a certain tolerance of that sample. Works regardless of
    white balance or tint shift between images.
    """
    # Sample a small patch from the top-left corner
    sample_size = 20
    corner_patch = bgr_img[:sample_size, :sample_size]
    bg_color = np.median(corner_patch.reshape(-1, 3), axis=0)  # BGR

    # Compute per-pixel distance from background colour in BGR space
    diff = np.abs(bgr_img.astype(np.int32) - bg_color.astype(np.int32))
    dist = np.max(diff, axis=2)  # max channel difference

    # Pixels within tolerance = background, outside = foreground (fly)
    mask = np.where(dist > BG_TOLERANCE, 255, 0).astype(np.uint8)
    return mask


def clean_mask(mask):
    """
    Morphological open (remove noise), close (fill body gaps), then hard
    erosion to push touching blobs apart before contour/watershed analysis.
    """
    open_k  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KERNEL_SIZE,  OPEN_KERNEL_SIZE))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (CLOSE_KERNEL_SIZE, CLOSE_KERNEL_SIZE))
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ERODE_KERNEL_SIZE, ERODE_KERNEL_SIZE))

    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  open_k,  iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k, iterations=2)
    # Hard erosion to separate touching/close blobs before watershed
    mask = cv2.erode(mask, erode_k, iterations=ERODE_ITERATIONS)
    return mask


def get_contours(mask):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return contours


def watershed_split(bgr_img, blob_mask):
    """
    Apply distance-transform watershed to split overlapping flies within a
    single large blob. Returns the number of peaks (fly centres) found.
    """
    dist = cv2.distanceTransform(blob_mask, cv2.DIST_L2, 5)
    cv2.normalize(dist, dist, 0, 1.0, cv2.NORM_MINMAX)

    # Lower threshold = more aggressive peak finding for close flies
    _, sure_fg = cv2.threshold(dist, WATERSHED_DIST_THRESHOLD, 255, cv2.THRESH_BINARY)
    sure_fg    = np.uint8(sure_fg)

    # Each connected peak = one fly
    n_labels, _ = cv2.connectedComponents(sure_fg)
    return max(1, n_labels - 1)


def count_flies_in_image(bgr_img, debug=False):
    """
    Full pipeline. Returns (count, debug_img_or_None).
    """
    h, w     = bgr_img.shape[:2]
    img_area = h * w

    min_area = SINGLE_FLY_MIN_FRAC * img_area
    max_area = SINGLE_FLY_MAX_FRAC * img_area

    # 1. Background subtraction (lightness + purple tint)
    mask = subtract_background(bgr_img)

    # 2. Morphological cleanup + erosion
    mask = clean_mask(mask)

    # 3. Find contours
    contours = get_contours(mask)

    # 4. Collect single-fly candidate areas for median reference
    candidate_areas = [cv2.contourArea(c) for c in contours
                       if min_area <= cv2.contourArea(c) <= max_area]
    median_area = np.median(candidate_areas) if candidate_areas else (min_area + max_area) / 2

    # 5. Count flies, splitting large blobs via watershed
    fly_count   = 0
    blob_counts = []  # (contour, count) for debug

    for cnt in contours:
        area = cv2.contourArea(cnt)

        if area < min_area:
            # Noise — ignore
            blob_counts.append((cnt, 0))
            continue

        if area <= max_area:
            # Single fly
            fly_count += 1
            blob_counts.append((cnt, 1))

        else:
            # Large blob — watershed + area ratio, take conservative estimate
            blob_mask = np.zeros((h, w), dtype=np.uint8)
            cv2.drawContours(blob_mask, [cnt], -1, 255, thickness=cv2.FILLED)
            n_watershed   = watershed_split(bgr_img, blob_mask)
            n_area        = max(1, round(area / median_area))
            n_flies       = min(n_watershed, n_area)
            fly_count    += n_flies
            blob_counts.append((cnt, n_flies))

    # 6. Optional debug visualisation
    debug_img = None
    if debug:
        debug_img = bgr_img.copy()
        for cnt, n in blob_counts:
            if n == 0:
                color = (180, 180, 180)   # grey  = noise ignored
            elif n == 1:
                color = (0, 220, 0)       # green = single fly
            else:
                color = (0, 100, 255)     # orange = merged, split by watershed
            cv2.drawContours(debug_img, [cnt], -1, color, 2)
            M = cv2.moments(cnt)
            if M['m00']:
                cx = int(M['m10'] / M['m00'])
                cy = int(M['m01'] / M['m00'])
                cv2.putText(debug_img, str(n), (cx - 10, cy + 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 2)
        cv2.putText(debug_img, f"Total: {fly_count}", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.8, (0, 0, 255), 3)

    return fly_count, debug_img


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Count fruit flies in microscopy images.")
    parser.add_argument('--input', default='flyblobs', help='Folder containing images (default: ./flyblobs)')
    parser.add_argument('--debug', action='store_true', help='Save debug visualisation images alongside originals')
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"[ERROR] Folder '{input_dir}' not found.")
        return

    images = [p for p in sorted(input_dir.iterdir()) if p.suffix.lower() in IMAGE_EXTS]
    if not images:
        print(f"[ERROR] No images found in '{input_dir}'.")
        return

    print(f"\nProcessing {len(images)} image(s) from '{input_dir}'\n")
    print(f"{'Image':<40} {'Flies':>6}")
    print("─" * 48)

    total = 0
    for img_path in images:
        bgr = cv2.imread(str(img_path))
        if bgr is None:
            print(f"{img_path.name:<40} {'[read error]':>6}")
            continue

        count, debug_img = count_flies_in_image(bgr, debug=args.debug)
        print(f"{img_path.name:<40} {count:>6}")
        total += count

        if args.debug and debug_img is not None:
            out_path = input_dir / f"debug_{img_path.name}"
            cv2.imwrite(str(out_path), debug_img)

    print("─" * 48)
    print(f"{'TOTAL':<40} {total:>6}\n")

    if args.debug:
        print(f"[Debug] Annotated images saved to '{input_dir}/' with 'debug_' prefix.\n")


if __name__ == '__main__':
    main()