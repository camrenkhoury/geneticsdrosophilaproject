#!/usr/bin/env python3
"""
fly_classifier.py
-----------------
Importable module for capturing and classifying a single fruit fly image.

Usage from another script:
    from fly_classifier import classify_fly

    result = classify_fly()
    print(result)
    # {'class': 'female', 'confidence': 0.94, 'errors': []}
    # {'class': 'UNCERTAIN', 'confidence': 0.61, 'errors': ['LOW_CONF_CLASS(0.61)']}
    # {'class': 'UNCERTAIN', 'confidence': 0.0,  'errors': ['NO_DETECTION']}
"""

import os
import sys
import subprocess
import json
import shutil
from pathlib import Path
import numpy as np
import cv2

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
        def __init__(self, path):
            self.path = path
        def predict(self, source, conf=0.5, save=False):
            # Mock prediction
            return []

    YOLO = MockYOLO

# ── Config ────────────────────────────────────────────────────────────────────
CLASSIFIER_MODEL_PATH = str(MODEL_PATH)
TEMP_IMAGE_DIR        = str(TEMP_CLASS_IMAGE_DIR)
TEMP_IMAGE_PATH       = os.path.join(TEMP_IMAGE_DIR, 'temp.jpg')
LATEST_IMAGE_PATH     = os.path.join(TEMP_IMAGE_DIR, 'latest_classification.jpg')

UNCERTAIN_THRESHOLD   = 0.70   # classifier confidence below this → UNCERTAIN

HARD_ERROR_FLAGS = {
    'CAPTURE_FAILED',
    'LOAD_FAILED',
    'CLASSIFIER_FAILED',
    'COUNT_FAILED',
    'EMPTY_CHAMBER',
    'MULTIPLE_FLIES_IN_CHAMBER',
}

BG_TOLERANCE = 65
OPEN_KERNEL_SIZE = 3
CLOSE_KERNEL_SIZE = 11
ERODE_KERNEL_SIZE = 11
ERODE_ITERATIONS = 9
SINGLE_FLY_MIN_FRAC = 0.001
SINGLE_FLY_MAX_AREA_PX = 40000

SETTINGS_PATH = REPO_ROOT / "vision" / "fin6" / ".fly_tracking_gui_settings.json"
# ─────────────────────────────────────────────────────────────────────────────

# Load model once at import time
if ULTRALYTICS_AVAILABLE:
    print('[fly_classifier] Loading classifier model ...')
    _classifier_model = YOLO(CLASSIFIER_MODEL_PATH)
    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    print('[fly_classifier] Ready.')
else:
    print('[fly_classifier] Running in simulation mode - no model loaded.')
    _classifier_model = None


def _capture_image() -> bool:
    """Capture a single image to TEMP_IMAGE_PATH. Returns True on success."""
    if not ULTRALYTICS_AVAILABLE:
        # Simulation: pretend capture succeeds
        print("Simulated image capture.")
        return True
    os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    command = [
        '/usr/bin/rpicam-still',
        '--output', TEMP_IMAGE_PATH,
        '--nopreview',
        '-n',
    ]
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding='utf-8')) if SETTINGS_PATH.exists() else {}
    except Exception:
        saved = {}
    camera_index = int(saved.get('sexing_camera_index_var', 0) or 0)
    command.extend(['--camera', str(camera_index)])
    result = subprocess.run(command, capture_output=True, text=True)
    print(f"returncode: {result.returncode}")
    print(f"stderr: {result.stderr}")
    return result.returncode == 0


def _subtract_background(bgr_img: np.ndarray) -> np.ndarray:
    sample_size = 20
    corner_patch = bgr_img[:sample_size, :sample_size]
    bg_color = np.median(corner_patch.reshape(-1, 3), axis=0)
    diff = np.abs(bgr_img.astype(np.int32) - bg_color.astype(np.int32))
    dist = np.max(diff, axis=2)
    return np.where(dist > BG_TOLERANCE, 255, 0).astype(np.uint8)


def _clean_mask(mask: np.ndarray) -> np.ndarray:
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KERNEL_SIZE, OPEN_KERNEL_SIZE))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (CLOSE_KERNEL_SIZE, CLOSE_KERNEL_SIZE))
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ERODE_KERNEL_SIZE, ERODE_KERNEL_SIZE))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k, iterations=2)
    mask = cv2.erode(mask, erode_k, iterations=ERODE_ITERATIONS)
    return mask


def _count_flies(bgr_img: np.ndarray) -> int:
    h, w = bgr_img.shape[:2]
    img_area = max(1, h * w)
    min_area = SINGLE_FLY_MIN_FRAC * img_area
    mask = _subtract_background(bgr_img)
    mask = _clean_mask(mask)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    fly_count = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        if area <= SINGLE_FLY_MAX_AREA_PX:
            fly_count += 1
        else:
            fly_count += 2
    return int(fly_count)


def classify_fly() -> dict:
    """
    Capture an image, classify the fly, delete the image, return result dict.

    Returns
    -------
    dict with keys:
        'class'      : 'male' | 'female' | 'UNCERTAIN'
        'count'      : int
        'confidence' : float (0.0 if no valid classification)
        'errors'     : list of error strings (empty if all went well)
    """
    errors     = []
    cls_out    = 'UNCERTAIN'
    confidence = 0.0
    fly_count  = 0
    latest_image_path = None

    # ── Capture ───────────────────────────────────────────────────────────────
    if not _capture_image():
        errors.append('CAPTURE_FAILED')
        return {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0, 'errors': errors}

    # ── Load image ────────────────────────────────────────────────────────────
    if not ULTRALYTICS_AVAILABLE:
        # Simulation: skip loading
        image = None
    else:
        try:
            data  = np.fromfile(TEMP_IMAGE_PATH, dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('imdecode returned None')
            try:
                shutil.copyfile(TEMP_IMAGE_PATH, LATEST_IMAGE_PATH)
                latest_image_path = LATEST_IMAGE_PATH
            except Exception:
                latest_image_path = None
        except Exception:
            errors.append('LOAD_FAILED')
            _cleanup()
            return {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0, 'errors': errors}

    if ULTRALYTICS_AVAILABLE and image is not None:
        try:
            fly_count = _count_flies(image)
        except Exception:
            fly_count = 0
            errors.append('COUNT_FAILED')

    # ── Classification ────────────────────────────────────────────────────────
    if not ULTRALYTICS_AVAILABLE:
        # Simulation: return mock result
        import random
        mock_classes = ['male', 'female', 'UNCERTAIN']
        cls_out = random.choice(mock_classes)
        confidence = random.uniform(0.5, 0.95) if cls_out != 'UNCERTAIN' else 0.0
        if cls_out == 'UNCERTAIN':
            errors.append('SIMULATION_MODE')
        print(f"Simulated classification: {cls_out} with confidence {confidence:.2f}")
    elif fly_count <= 0:
        errors.append('EMPTY_CHAMBER')
        cls_out = 'UNCERTAIN'
        confidence = 0.0
    elif fly_count > 1:
        errors.append(f'MULTIPLE_FLIES_IN_CHAMBER({fly_count})')
        cls_out = 'UNCERTAIN'
        confidence = 0.0
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

    # ── Cleanup ───────────────────────────────────────────────────────────────
    _cleanup()

    return {
        'count':      int(fly_count),
        'class':      cls_out,
        'confidence': round(confidence, 4),
        'errors':     errors,
        'image_path': latest_image_path,
    }


def _cleanup():
    """Delete the temporary image."""
    try:
        if os.path.exists(TEMP_IMAGE_PATH):
            os.remove(TEMP_IMAGE_PATH)
    except Exception:
        pass


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('\nRunning test capture + classification...')
    result = classify_fly()
    print(f"\nResult: {result}")
