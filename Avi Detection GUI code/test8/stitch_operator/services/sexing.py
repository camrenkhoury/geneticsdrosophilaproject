from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

import cv2
import numpy as np

from ..bootstrap import ensure_repo_paths
from ..settings import OperatorSettings, resolve_repo_path

ensure_repo_paths()

from fly_classifier import count_flies_in_image  # noqa: E402

try:
    from ultralytics import YOLO

    ULTRALYTICS_AVAILABLE = True
except Exception:
    YOLO = None
    ULTRALYTICS_AVAILABLE = False


class SexingService:
    def __init__(self, settings: OperatorSettings):
        self.settings = settings
        self.capture_dir = resolve_repo_path(settings.sexing_capture_dir)
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.latest_capture_path = self.capture_dir / 'latest_capture.jpg'
        self._model = None
        self._model_error = ''
        self._model_ready = False
        self.reload_model()

    @property
    def model_path(self) -> Path:
        return resolve_repo_path(self.settings.sexing_model_path)

    def status(self) -> Dict[str, Any]:
        return {
            'ready': bool(self._model_ready),
            'path': str(self.model_path),
            'error': self._model_error,
        }

    def reload_model(self) -> Dict[str, Any]:
        self._model = None
        self._model_ready = False
        self._model_error = ''
        model_path = self.model_path
        if not model_path.exists():
            self._model_error = f'Model missing: {model_path}'
            return self.status()
        if not ULTRALYTICS_AVAILABLE or YOLO is None:
            self._model_error = 'Ultralytics is not installed. Install it on the Pi to enable sexing.'
            return self.status()
        try:
            self._model = YOLO(str(model_path))
            self._model_ready = True
        except Exception as exc:
            self._model_error = f'Could not load model: {exc}'
        return self.status()

    def _run_capture_command(self, args: List[str], *, error_text: str) -> None:
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or error_text)

    def _warm_rpicam_sensor(self, *, width: int, height: int) -> None:
        warm_command = shutil.which('rpicam-vid') or '/usr/bin/rpicam-vid'
        if not Path(warm_command).exists():
            return
        try:
            self._run_capture_command(
                [
                    warm_command,
                    '--timeout',
                    '900',
                    '--width',
                    str(width),
                    '--height',
                    str(height),
                    '--codec',
                    'yuv420',
                    '--nopreview',
                    '-n',
                    '-o',
                    '/dev/null',
                ],
                error_text='camera warmup failed',
            )
        except Exception:
            # Warmup improves Pi HQ colour/exposure stability, but capture can still proceed without it.
            return

    def _looks_green_tinted(self, image_bgr: np.ndarray) -> bool:
        if image_bgr is None or image_bgr.ndim != 3 or image_bgr.shape[2] < 3:
            return False
        mean_b, mean_g, mean_r = image_bgr.reshape(-1, image_bgr.shape[2]).mean(axis=0)[:3]
        strongest_other = max(float(mean_b), float(mean_r), 1.0)
        return float(mean_g) > strongest_other * 1.35 and (float(mean_g) - strongest_other) > 18.0

    def _capture_with_rpicam(self, output_path: Path) -> None:
        command = str(self.settings.sexing_capture_command).strip() or '/usr/bin/rpicam-still'
        if not os.path.isabs(command):
            resolved = shutil.which(command)
            if resolved:
                command = resolved
        if not Path(command).exists():
            raise RuntimeError(
                'Sexing camera capture command not found. Install libcamera apps or update Debug > Models & Paths.'
            )

        width = 2028
        height = 1520
        still_args = [
            command,
            '--output',
            str(output_path),
            '--nopreview',
            '-n',
            '--width',
            str(width),
            '--height',
            str(height),
        ]

        if Path(command).name == 'rpicam-still':
            self._warm_rpicam_sensor(width=width, height=height)
            capture_args = still_args + ['--immediate']
        else:
            capture_args = list(still_args)

        self._run_capture_command(capture_args, error_text='capture failed')

        if Path(command).name == 'rpicam-still':
            image_bgr = cv2.imread(str(output_path), cv2.IMREAD_COLOR)
            if self._looks_green_tinted(image_bgr):
                self._warm_rpicam_sensor(width=width, height=height)
                retry_args = still_args + ['--timeout', '1000']
                self._run_capture_command(retry_args, error_text='capture retry failed')

    def capture_preview(self) -> Path:
        self._capture_with_rpicam(self.latest_capture_path)
        return self.latest_capture_path

    def _parse_prediction(self, image_bgr: np.ndarray) -> Dict[str, Any]:
        errors: List[str] = []
        label = 'UNCERTAIN'
        confidence = 0.0

        if self._model is None or not self._model_ready:
            errors.append('MODEL_MISSING')
            if self._model_error:
                errors.append(self._model_error)
            return {'label': label, 'confidence': confidence, 'errors': errors}

        try:
            results = self._model(image_bgr, verbose=False)
        except Exception as exc:
            errors.append(f'CLASSIFIER_FAILED: {exc}')
            return {'label': label, 'confidence': confidence, 'errors': errors}

        if not results:
            errors.append('NO_RESULT')
            return {'label': label, 'confidence': confidence, 'errors': errors}

        probs = getattr(results[0], 'probs', None)
        if probs is None:
            errors.append('NO_PROBS')
            return {'label': label, 'confidence': confidence, 'errors': errors}

        top1_idx = int(probs.top1)
        top1_conf = float(probs.top1conf)
        raw_label = str(results[0].names[top1_idx]).strip().lower()
        if 'female' in raw_label:
            mapped = 'female'
        elif 'male' in raw_label:
            mapped = 'male'
        else:
            mapped = 'unknown'
            errors.append(f'UNKNOWN_CLASS:{raw_label}')

        confidence = top1_conf
        threshold = float(self.settings.sexing_uncertain_threshold)
        if mapped == 'unknown' or top1_conf < threshold:
            label = 'UNCERTAIN'
            errors.append(f'LOW_CONF:{top1_conf:.3f}')
        else:
            label = mapped

        return {'label': label, 'confidence': confidence, 'errors': errors}

    def classify(self) -> Dict[str, Any]:
        errors: List[str] = []
        captured_at = time.strftime('%Y-%m-%dT%H:%M:%S')
        try:
            self._capture_with_rpicam(self.latest_capture_path)
        except Exception as exc:
            errors.append(f'CAPTURE_FAILED: {exc}')
            return {
                'captured_at': captured_at,
                'count': 0,
                'label': 'UNCERTAIN',
                'confidence': 0.0,
                'image_path': str(self.latest_capture_path),
                'errors': errors,
                'detail': '; '.join(errors),
                'uncertain': True,
                'rejected': False,
                'rejection_reason': '',
            }

        image_bgr = cv2.imread(str(self.latest_capture_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            errors.append('LOAD_FAILED')
            return {
                'captured_at': captured_at,
                'count': 0,
                'label': 'UNCERTAIN',
                'confidence': 0.0,
                'image_path': str(self.latest_capture_path),
                'errors': errors,
                'detail': '; '.join(errors),
                'uncertain': True,
                'rejected': False,
                'rejection_reason': '',
            }

        try:
            fly_count, _ = count_flies_in_image(image_bgr, debug=False)
        except Exception as exc:
            fly_count = 0
            errors.append(f'COUNT_FAILED: {exc}')

        if fly_count != 1:
            errors.append(f'INVALID_FLY_COUNT:{fly_count}')
            if fly_count <= 0:
                reject_reason = 'No fly was detected in the sexing chamber. Rejecting this pickup cycle.'
            else:
                reject_reason = f'Detected {fly_count} flies in the sexing chamber. Rejecting this pickup cycle.'
            return {
                'captured_at': captured_at,
                'count': fly_count,
                'label': 'REJECTED',
                'confidence': 0.0,
                'image_path': str(self.latest_capture_path.resolve()),
                'errors': errors,
                'detail': reject_reason,
                'uncertain': False,
                'rejected': True,
                'rejection_reason': reject_reason,
            }

        parsed = self._parse_prediction(image_bgr)
        errors.extend(parsed['errors'])
        label = str(parsed['label'])
        confidence = float(parsed['confidence'])
        uncertain = label == 'UNCERTAIN'
        detail = '; '.join(errors) if errors else 'Model classified successfully.'
        return {
            'captured_at': captured_at,
            'count': fly_count,
            'label': label,
            'confidence': confidence,
            'image_path': str(self.latest_capture_path.resolve()),
            'errors': errors,
            'detail': detail,
            'uncertain': uncertain,
            'rejected': False,
            'rejection_reason': '',
        }
