#!/usr/bin/env python3
"""
fly_classifier.py
-----------------
Importable module for capturing, counting, and classifying fruit flies.
Captures a single image, counts flies via blob detection, then classifies
the fly as male/female via YOLO.

Usage from another script:
    from fly_classifier import classify_fly

    result = classify_fly()
    print(result)
    # {'count': 2, 'class': 'female', 'confidence': 0.94, 'errors': []}
    # {'count': 1, 'class': 'UNCERTAIN', 'confidence': 0.61, 'errors': ['LOW_CONF_CLASS(0.61)']}
    # {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0,  'errors': ['CAPTURE_FAILED']}

Standalone test:
    python3 fly_classifier.py
    python3 fly_classifier.py --debug    # saves debug visualisation to temp folder
"""

import os
import sys
import subprocess
import argparse
import numpy as np
import cv2

try:
    from ultralytics import YOLO
    ULTRALYTICS_AVAILABLE = True
except ImportError:
    ULTRALYTICS_AVAILABLE = False
    class MockYOLO:
        def __init__(self, path): self.path = path
        def predict(self, source, conf=0.5, save=False): return []
    YOLO = MockYOLO


# ── Config ────────────────────────────────────────────────────────────────────

CLASSIFIER_MODEL_PATH = os.path.expanduser('~/geneticsdrosophiliaproject/best.pt')
TEMP_IMAGE_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tempClassImage')
TEMP_IMAGE_PATH       = os.path.join(TEMP_IMAGE_DIR, 'temp.jpg')

# YOLO confidence threshold below which result is UNCERTAIN
UNCERTAIN_THRESHOLD = 0.70

HARD_ERROR_FLAGS = {
    'CAPTURE_FAILED',
    'LOAD_FAILED',
    'CLASSIFIER_FAILED',
}

# ── Blob counting config ───────────────────────────────────────────────────────

# How different a pixel must be from the sampled corner colour to be
# considered foreground. Raise if background is leaking, lower if fly
# pixels are being eaten.
BG_TOLERANCE = 65

# Morphology kernel sizes
OPEN_KERNEL_SIZE  = 3    # removes tiny noise specks
CLOSE_KERNEL_SIZE = 11   # fills gaps within a fly body (smaller = less merging)

# Hard erosion to separate touching/close blobs before watershed
ERODE_KERNEL_SIZE = 11
ERODE_ITERATIONS  = 7

# Watershed distance transform threshold — lower = more aggressive peak finding
WATERSHED_DIST_THRESHOLD = 0.3

# Fly size bounds as a fraction of total image area
SINGLE_FLY_MIN_FRAC = 0.001   # ignore blobs smaller than this (noise)
SINGLE_FLY_MAX_FRAC = 0.12    # single fly ceiling before watershed kicks in

# Watershed triggers if blob area exceeds this multiple of median single-fly area
SPLIT_RATIO_THRESHOLD = 1.3


# ── Model init ────────────────────────────────────────────────────────────────

if ULTRALYTICS_AVAILABLE:
    print('[fly_classifier] Loading classifier model ...')
    _classifier_model = YOLO(CLASSIFIER_MODEL_PATH)
    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    print('[fly_classifier] Ready.')
else:
    print('[fly_classifier] Running in simulation mode - no model loaded.')
    _classifier_model = None


# ── Capture ───────────────────────────────────────────────────────────────────

def _capture_image() -> bool:
    """Capture a single image to TEMP_IMAGE_PATH. Returns True on success."""
    if not ULTRALYTICS_AVAILABLE:
        print('Simulated image capture.')
        return True

    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    result = subprocess.run([
        '/usr/bin/rpicam-still',
        '--output', TEMP_IMAGE_PATH,
        '--nopreview',
        '-n',
        '--width',  '2028',
        '--height', '1520',
    ], capture_output=True, text=True)
    return result.returncode == 0


# ── Blob counting pipeline ────────────────────────────────────────────────────

def _subtract_background(bgr_img):
    """
    Sample top-left corner to get background colour, mask anything within
    BG_TOLERANCE of that colour. Adapts to each image's actual tint.
    """
    sample_size  = 20
    corner_patch = bgr_img[:sample_size, :sample_size]
    bg_color     = np.median(corner_patch.reshape(-1, 3), axis=0)
    diff         = np.abs(bgr_img.astype(np.int32) - bg_color.astype(np.int32))
    dist         = np.max(diff, axis=2)
    return np.where(dist > BG_TOLERANCE, 255, 0).astype(np.uint8)


def _clean_mask(mask):
    open_k  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KERNEL_SIZE,  OPEN_KERNEL_SIZE))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (CLOSE_KERNEL_SIZE, CLOSE_KERNEL_SIZE))
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ERODE_KERNEL_SIZE, ERODE_KERNEL_SIZE))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  open_k,  iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k, iterations=2)
    mask = cv2.erode(mask, erode_k, iterations=ERODE_ITERATIONS)
    return mask


def _watershed_split(bgr_img, blob_mask):
    dist = cv2.distanceTransform(blob_mask, cv2.DIST_L2, 5)
    cv2.normalize(dist, dist, 0, 1.0, cv2.NORM_MINMAX)
    _, sure_fg = cv2.threshold(dist, WATERSHED_DIST_THRESHOLD, 255, cv2.THRESH_BINARY)
    sure_fg    = np.uint8(sure_fg)
    n_labels, _ = cv2.connectedComponents(sure_fg)
    return max(1, n_labels - 1)


def _count_flies(bgr_img, debug=False):
    """
    Run blob counting pipeline on bgr_img.
    Returns (count, debug_img_or_None).
    """
    h, w     = bgr_img.shape[:2]
    img_area = h * w
    min_area = SINGLE_FLY_MIN_FRAC * img_area
    max_area = SINGLE_FLY_MAX_FRAC * img_area

    mask     = _subtract_background(bgr_img)
    mask     = _clean_mask(mask)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidate_areas = [cv2.contourArea(c) for c in contours
                       if min_area <= cv2.contourArea(c) <= max_area]
    median_area = np.median(candidate_areas) if candidate_areas else (min_area + max_area) / 2

    fly_count   = 0
    blob_counts = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            blob_counts.append((cnt, 0))
            continue
        if area <= max_area:
            fly_count += 1
            blob_counts.append((cnt, 1))
        else:
            blob_mask = np.zeros((h, w), dtype=np.uint8)
            cv2.drawContours(blob_mask, [cnt], -1, 255, thickness=cv2.FILLED)
            n_watershed = _watershed_split(bgr_img, blob_mask)
            n_area      = max(1, round(area / median_area))
            n_flies     = min(n_watershed, n_area)
            fly_count  += n_flies
            blob_counts.append((cnt, n_flies))

    debug_img = None
    if debug:
        debug_img = bgr_img.copy()
        for cnt, n in blob_counts:
            color = (180, 180, 180) if n == 0 else (0, 220, 0) if n == 1 else (0, 100, 255)
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


# ── Cleanup ───────────────────────────────────────────────────────────────────

def _cleanup():
    try:
        if os.path.exists(TEMP_IMAGE_PATH):
            os.remove(TEMP_IMAGE_PATH)
    except Exception:
        pass


# ── Main API ──────────────────────────────────────────────────────────────────

def classify_fly(debug=False) -> dict:
    """
    Capture an image, count flies, classify male/female, return result dict.

    Returns
    -------
    dict with keys:
        'count'      : int   — number of flies detected by blob counter
        'class'      : 'male' | 'female' | 'UNCERTAIN'
        'confidence' : float (0.0 if no valid classification)
        'errors'     : list of error strings (empty if all went well)
        'debug_img'  : annotated BGR image or None (only if debug=True)
    """
    errors     = []
    cls_out    = 'UNCERTAIN'
    confidence = 0.0
    fly_count  = 0
    debug_img  = None

    # ── Capture ───────────────────────────────────────────────────────────────
    if not _capture_image():
        errors.append('CAPTURE_FAILED')
        return {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0,
                'errors': errors, 'debug_img': None}

    # ── Load image ────────────────────────────────────────────────────────────
    if not ULTRALYTICS_AVAILABLE:
        image = None
    else:
        try:
            data  = np.fromfile(TEMP_IMAGE_PATH, dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('imdecode returned None')
        except Exception:
            errors.append('LOAD_FAILED')
            _cleanup()
            return {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0,
                    'errors': errors, 'debug_img': None}

    # ── Blob count ────────────────────────────────────────────────────────────
    if ULTRALYTICS_AVAILABLE and image is not None:
        fly_count, debug_img = _count_flies(image, debug=debug)
    else:
        import random
        fly_count = random.randint(1, 3)

    # ── YOLO classification ───────────────────────────────────────────────────
    if not ULTRALYTICS_AVAILABLE:
        import random
        mock_classes = ['male', 'female', 'UNCERTAIN']
        cls_out    = random.choice(mock_classes)
        confidence = random.uniform(0.5, 0.95) if cls_out != 'UNCERTAIN' else 0.0
        if cls_out == 'UNCERTAIN':
            errors.append('SIMULATION_MODE')
        print(f'Simulated classification: {cls_out} ({confidence:.2f})')
    else:
        cls_results = _classifier_model(image, verbose=False)

        if cls_results and cls_results[0].probs is not None:
            probs     = cls_results[0].probs
            top1_idx  = int(probs.top1)
            top1_conf = float(probs.top1conf)
            raw_label = cls_results[0].names[top1_idx].strip().lower()

            if 'female' in raw_label:
                label = 'female'
            elif 'male' in raw_label:
                label = 'male'
            else:
                label = 'unknown'
                errors.append('UNKNOWN_CLASS')

            confidence = top1_conf

            if top1_conf < UNCERTAIN_THRESHOLD or label == 'unknown':
                cls_out = 'UNCERTAIN'
                errors.append(f'LOW_CONF_CLASS({top1_conf:.2f})')
            else:
                cls_out = label
        else:
            errors.append('CLASSIFIER_FAILED')

    # Force UNCERTAIN on any hard error
    if any(e in HARD_ERROR_FLAGS for e in errors):
        cls_out    = 'UNCERTAIN'
        confidence = 0.0

    _cleanup()

    return {
        'count':      fly_count,
        'class':      cls_out,
        'confidence': round(confidence, 4),
        'errors':     errors,
        'debug_img':  debug_img,
    }


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true',
                        help='Save debug visualisation to temp folder')
    args = parser.parse_args()

    print('\nRunning test capture + classification...\n')
    result = classify_fly(debug=args.debug)

    if args.debug and result['debug_img'] is not None:
        debug_path = os.path.join(TEMP_IMAGE_DIR, 'debug_last.jpg')
        cv2.imwrite(debug_path, result['debug_img'])
        print(f'[Debug] Annotated image saved to {debug_path}')

    # Print without the image array
    printable = {k: v for k, v in result.items() if k != 'debug_img'}
    print(f'\nResult: {printable}')
