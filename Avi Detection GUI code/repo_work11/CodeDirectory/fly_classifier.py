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
import numpy as np
import cv2

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
CLASSIFIER_MODEL_PATH = os.path.expanduser('~/newone.pt')
TEMP_IMAGE_DIR        = os.path.expanduser('~/tempClassImage')
TEMP_IMAGE_PATH       = os.path.join(TEMP_IMAGE_DIR, 'temp.jpg')

UNCERTAIN_THRESHOLD   = 0.70   # classifier confidence below this → UNCERTAIN

HARD_ERROR_FLAGS = {
    'CAPTURE_FAILED',
    'LOAD_FAILED',
    'CLASSIFIER_FAILED',
}
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

    result = subprocess.run([
        '/usr/bin/rpicam-still',
        '--output', TEMP_IMAGE_PATH,
        '--zsl',
        '--awbgains', '3.0,0.9',
        '--nopreview',
        '-n',
    ], capture_output=True, text=True)
    return result.returncode == 0


def classify_fly() -> dict:
    """
    Capture an image, classify the fly, delete the image, return result dict.

    Returns
    -------
    dict with keys:
        'class'      : 'male' | 'female' | 'UNCERTAIN'
        'confidence' : float (0.0 if no valid classification)
        'errors'     : list of error strings (empty if all went well)
    """
    errors     = []
    cls_out    = 'UNCERTAIN'
    confidence = 0.0

    # ── Capture ───────────────────────────────────────────────────────────────
    if not _capture_image():
        errors.append('CAPTURE_FAILED')
        return {'class': 'UNCERTAIN', 'confidence': 0.0, 'errors': errors}

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
        except Exception:
            errors.append('LOAD_FAILED')
            _cleanup()
            return {'class': 'UNCERTAIN', 'confidence': 0.0, 'errors': errors}

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
        'class':      cls_out,
        'confidence': round(confidence, 4),
        'errors':     errors,
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
