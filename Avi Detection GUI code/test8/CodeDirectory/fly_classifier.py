#!/usr/bin/env python3
"""
fly_classifier.py
-----------------
Reusable helpers for capturing, counting, and classifying drosophila images.

Public helpers:
    count_flies_in_image(image_bgr, debug=False)
    classify_image(image_bgr, model=None, model_path=None)
    classify_fly(debug=False)
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

try:
    from ultralytics import YOLO

    ULTRALYTICS_AVAILABLE = True
except ImportError:
    YOLO = None
    ULTRALYTICS_AVAILABLE = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = PROJECT_ROOT / 'models' / 'best.pt'
TEMP_IMAGE_DIR = Path(__file__).resolve().parent / 'tempClassImage'
TEMP_IMAGE_PATH = TEMP_IMAGE_DIR / 'temp.jpg'
DEFAULT_CAPTURE_COMMAND = '/usr/bin/rpicam-still'
UNCERTAIN_THRESHOLD = 0.70

HARD_ERROR_FLAGS = {
    'CAPTURE_FAILED',
    'LOAD_FAILED',
    'CLASSIFIER_FAILED',
}

# Blob counting configuration
BG_TOLERANCE = 65
OPEN_KERNEL_SIZE = 3
CLOSE_KERNEL_SIZE = 11
ERODE_KERNEL_SIZE = 11
ERODE_ITERATIONS = 9
SINGLE_FLY_MIN_FRAC = 0.001
SINGLE_FLY_MAX_AREA = 40000

_MODEL_CACHE: Dict[str, Any] = {}


def load_classifier_model(model_path: str | Path | None = None):
    path = Path(model_path or DEFAULT_MODEL_PATH).expanduser().resolve()
    cache_key = str(path)
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]
    if not ULTRALYTICS_AVAILABLE or YOLO is None:
        raise RuntimeError('Ultralytics is not installed.')
    if not path.exists():
        raise FileNotFoundError(f'Classifier model not found: {path}')
    model = YOLO(str(path))
    _MODEL_CACHE[cache_key] = model
    return model


def _capture_image(
    output_path: str | Path = TEMP_IMAGE_PATH,
    *,
    capture_command: str | Path = DEFAULT_CAPTURE_COMMAND,
) -> bool:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = str(capture_command).strip() or DEFAULT_CAPTURE_COMMAND
    result = subprocess.run(
        [
            command,
            '--output',
            str(output),
            '--zsl',
            '--awbgains',
            '3.0,0.9',
            '--nopreview',
            '-n',
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _subtract_background(bgr_img: np.ndarray) -> np.ndarray:
    sample_size = 20
    corner_patch = bgr_img[:sample_size, :sample_size]
    bg_color = np.median(corner_patch.reshape(-1, 3), axis=0)
    diff = np.abs(bgr_img.astype(np.int32) - bg_color.astype(np.int32))
    dist = np.max(diff, axis=2)
    return np.where(dist > BG_TOLERANCE, 255, 0).astype(np.uint8)


def _clean_mask(mask: np.ndarray) -> np.ndarray:
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KERNEL_SIZE, OPEN_KERNEL_SIZE))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (CLOSE_KERNEL_SIZE, CLOSE_KERNEL_SIZE))
    erode_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ERODE_KERNEL_SIZE, ERODE_KERNEL_SIZE))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel, iterations=2)
    mask = cv2.erode(mask, erode_kernel, iterations=ERODE_ITERATIONS)
    return mask


def count_flies_in_image(
    bgr_img: np.ndarray,
    debug: bool = False,
) -> Tuple[int, Optional[np.ndarray]]:
    """Count chamber flies from a BGR image using a simple blob pipeline."""
    if bgr_img is None or getattr(bgr_img, 'size', 0) == 0:
        raise ValueError('A non-empty BGR image is required.')

    h, w = bgr_img.shape[:2]
    img_area = float(h * w)
    min_area = SINGLE_FLY_MIN_FRAC * img_area

    mask = _subtract_background(bgr_img)
    mask = _clean_mask(mask)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    fly_count = 0
    debug_img = bgr_img.copy() if debug else None
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            blob_count = 0
        elif area <= SINGLE_FLY_MAX_AREA:
            blob_count = 1
            fly_count += 1
        else:
            blob_count = 2
            fly_count += 2

        if debug_img is not None:
            color = (180, 180, 180) if blob_count == 0 else (0, 220, 0) if blob_count == 1 else (0, 100, 255)
            cv2.drawContours(debug_img, [contour], -1, color, 2)
            moments = cv2.moments(contour)
            if moments['m00']:
                cx = int(moments['m10'] / moments['m00'])
                cy = int(moments['m01'] / moments['m00'])
                cv2.putText(debug_img, str(blob_count), (cx - 10, cy + 10), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 2)

    if debug_img is not None:
        cv2.putText(debug_img, f'Total: {fly_count}', (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)
    return fly_count, debug_img


def classify_image(
    image_bgr: np.ndarray,
    *,
    model: Any | None = None,
    model_path: str | Path | None = None,
    uncertain_threshold: float = UNCERTAIN_THRESHOLD,
) -> Dict[str, Any]:
    errors = []
    label = 'UNCERTAIN'
    confidence = 0.0

    if image_bgr is None or getattr(image_bgr, 'size', 0) == 0:
        return {'class': label, 'confidence': confidence, 'errors': ['LOAD_FAILED']}

    try:
        classifier = model or load_classifier_model(model_path)
    except Exception as exc:
        return {'class': label, 'confidence': confidence, 'errors': [f'CLASSIFIER_FAILED:{exc}']}

    try:
        results = classifier(image_bgr, verbose=False)
    except Exception as exc:
        return {'class': label, 'confidence': confidence, 'errors': [f'CLASSIFIER_FAILED:{exc}']}

    if not results:
        return {'class': label, 'confidence': confidence, 'errors': ['CLASSIFIER_FAILED']}

    probs = getattr(results[0], 'probs', None)
    if probs is None:
        return {'class': label, 'confidence': confidence, 'errors': ['CLASSIFIER_FAILED']}

    top1_idx = int(probs.top1)
    top1_conf = float(probs.top1conf)
    raw_label = str(results[0].names[top1_idx]).strip().lower()
    if 'female' in raw_label:
        mapped = 'female'
    elif 'male' in raw_label:
        mapped = 'male'
    else:
        mapped = 'unknown'
        errors.append('UNKNOWN_CLASS')

    confidence = top1_conf
    if mapped == 'unknown' or top1_conf < float(uncertain_threshold):
        label = 'UNCERTAIN'
        errors.append(f'LOW_CONF_CLASS({top1_conf:.2f})')
    else:
        label = mapped

    return {
        'class': label,
        'confidence': round(confidence, 4),
        'errors': errors,
    }


def _cleanup(path: str | Path = TEMP_IMAGE_PATH) -> None:
    try:
        target = Path(path)
        if target.exists():
            target.unlink()
    except Exception:
        pass


def classify_fly(
    debug: bool = False,
    *,
    image_path: str | Path | None = None,
    model_path: str | Path | None = None,
    capture_command: str | Path = DEFAULT_CAPTURE_COMMAND,
) -> Dict[str, Any]:
    """Capture or load an image, count chamber flies, and classify the specimen."""
    errors = []
    confidence = 0.0
    cls_out = 'UNCERTAIN'
    fly_count = 0
    debug_img = None
    cleanup_after = False

    target_path = Path(image_path).expanduser() if image_path else TEMP_IMAGE_PATH
    if image_path is None:
        cleanup_after = True
        if not _capture_image(target_path, capture_command=capture_command):
            errors.append('CAPTURE_FAILED')
            return {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0, 'errors': errors, 'debug_img': None}

    try:
        data = np.fromfile(str(target_path), dtype=np.uint8)
        image_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image_bgr is None:
            raise ValueError('imdecode returned None')
    except Exception:
        errors.append('LOAD_FAILED')
        if cleanup_after:
            _cleanup(target_path)
        return {'count': 0, 'class': 'UNCERTAIN', 'confidence': 0.0, 'errors': errors, 'debug_img': None}

    try:
        fly_count, debug_img = count_flies_in_image(image_bgr, debug=debug)
    except Exception as exc:
        errors.append(f'COUNT_FAILED:{exc}')
        fly_count = 0

    if fly_count != 1:
        errors.append(f'INVALID_FLY_COUNT({fly_count})')
    elif not ULTRALYTICS_AVAILABLE:
        import random

        mock_classes = ['male', 'female', 'UNCERTAIN']
        cls_out = random.choice(mock_classes)
        confidence = random.uniform(0.5, 0.95) if cls_out != 'UNCERTAIN' else 0.0
        if cls_out == 'UNCERTAIN':
            errors.append('SIMULATION_MODE')
    else:
        classified = classify_image(image_bgr, model_path=model_path)
        cls_out = str(classified['class'])
        confidence = float(classified['confidence'])
        errors.extend(classified['errors'])

    if any(flag in errors for flag in HARD_ERROR_FLAGS):
        cls_out = 'UNCERTAIN'
        confidence = 0.0

    if cleanup_after:
        _cleanup(target_path)
    return {
        'count': fly_count,
        'class': cls_out,
        'confidence': round(confidence, 4),
        'errors': errors,
        'debug_img': debug_img,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Emit an annotated count overlay in the result payload.')
    parser.add_argument('--image', help='Optional existing image path instead of capturing with rpicam-still.')
    parser.add_argument('--model', help='Optional YOLO model path.')
    args = parser.parse_args()

    result = classify_fly(debug=args.debug, image_path=args.image, model_path=args.model)
    printable = {key: value for key, value in result.items() if key != 'debug_img'}
    print(printable)
