from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

import cv2

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy should be available on the Pi
    np = None

from ..bootstrap import ensure_repo_paths
from ..settings import OperatorSettings, resolve_repo_path

ensure_repo_paths()

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
        self.latest_capture_path = self.capture_dir / "latest_capture.jpg"
        self._model = None
        self._model_error = ""
        self._model_ready = False
        self.reload_model()

    @property
    def model_path(self) -> Path:
        return resolve_repo_path(self.settings.sexing_model_path)

    def status(self) -> Dict[str, Any]:
        return {
            "ready": bool(self._model_ready),
            "path": str(self.model_path),
            "error": self._model_error,
        }

    def reload_model(self) -> Dict[str, Any]:
        self._model = None
        self._model_ready = False
        self._model_error = ""
        model_path = self.model_path
        if not model_path.exists():
            self._model_error = f"Model missing: {model_path}"
            return self.status()
        if not ULTRALYTICS_AVAILABLE or YOLO is None:
            self._model_error = "Ultralytics is not installed. Install it on the Pi to enable sexing."
            return self.status()
        try:
            self._model = YOLO(str(model_path))
            self._model_ready = True
        except Exception as exc:
            self._model_error = f"Could not load model: {exc}"
        return self.status()

    def _capture_with_rpicam(self, output_path: Path) -> None:
        command = str(self.settings.sexing_capture_command).strip() or "/usr/bin/rpicam-still"
        if not os.path.isabs(command):
            resolved = shutil.which(command)
            if resolved:
                command = resolved
        if not Path(command).exists():
            raise RuntimeError(
                "Sexing camera capture command not found. Install libcamera apps or update Debug > Models & Paths."
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            command,
            "--output",
            str(output_path),
            "--immediate",
            "--width",
            str(int(getattr(self.settings, "sexing_capture_width", 2028))),
            "--height",
            str(int(getattr(self.settings, "sexing_capture_height", 1520))),
            "--nopreview",
            "-n",
        ]
        awbgains = str(getattr(self.settings, "sexing_capture_awbgains", "") or "").strip()
        if awbgains:
            cmd.extend(["--awbgains", awbgains])
        roi = str(getattr(self.settings, "sexing_capture_roi", "") or "").strip()
        if roi:
            cmd.extend(["--roi", roi])

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "capture failed")

    def capture_preview(self) -> Path:
        self._capture_with_rpicam(self.latest_capture_path)
        return self.latest_capture_path

    def _analyze_occupancy(self, image_bgr: cv2.typing.MatLike) -> Dict[str, Any]:
        if image_bgr is None:
            return {
                "occupied": True,
                "occupancy_score": 1.0,
                "occupancy_detail": "OCCUPANCY_FALLBACK:missing_image",
            }
        try:
            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            height, width = gray.shape[:2]
            if height < 20 or width < 20:
                return {
                    "occupied": True,
                    "occupancy_score": 1.0,
                    "occupancy_detail": "OCCUPANCY_FALLBACK:image_too_small",
                }

            x_pad = max(8, int(width * 0.12))
            y_pad = max(8, int(height * 0.12))
            roi = gray[y_pad : height - y_pad, x_pad : width - x_pad]
            if roi.size == 0:
                roi = gray

            if np is not None:
                median_value = float(np.median(roi))
                roi_std = float(np.std(roi))
            else:
                median_value = float(cv2.mean(roi)[0])
                roi_std = 0.0

            threshold_value = int(max(35.0, min(135.0, median_value - 42.0)))
            _, mask = cv2.threshold(roi, threshold_value, 255, cv2.THRESH_BINARY_INV)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

            contours_info = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = contours_info[0] if len(contours_info) == 2 else contours_info[1]
            largest_area = max((cv2.contourArea(contour) for contour in contours), default=0.0)
            roi_area = float(max(1, roi.shape[0] * roi.shape[1]))
            dark_fraction = float(cv2.countNonZero(mask)) / roi_area
            area_ratio = float(largest_area) / roi_area
            score = max(
                area_ratio / 0.0025,
                dark_fraction / 0.0200,
                max(0.0, roi_std - 12.0) / 28.0,
            )
            occupied = bool(
                largest_area >= max(280.0, roi_area * 0.0010)
                or area_ratio >= 0.0018
                or (dark_fraction >= 0.016 and roi_std >= 16.0)
            )
            return {
                "occupied": occupied,
                "occupancy_score": round(float(score), 3),
                "occupancy_detail": (
                    f"occupancy_score={score:.2f}; area_ratio={area_ratio:.4f}; "
                    f"dark_fraction={dark_fraction:.4f}; std={roi_std:.1f}; threshold={threshold_value}"
                ),
            }
        except Exception as exc:
            return {
                "occupied": True,
                "occupancy_score": 1.0,
                "occupancy_detail": f"OCCUPANCY_FALLBACK:{exc}",
            }

    def inspect_chamber(self) -> Dict[str, Any]:
        try:
            self._capture_with_rpicam(self.latest_capture_path)
        except Exception as exc:
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "image_path": str(self.latest_capture_path),
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": f"CAPTURE_FAILED:{exc}",
                "detail": f"CAPTURE_FAILED:{exc}",
            }

        image_bgr = cv2.imread(str(self.latest_capture_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "image_path": str(self.latest_capture_path.resolve()),
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": "LOAD_FAILED",
                "detail": "LOAD_FAILED",
            }

        occupancy = self._analyze_occupancy(image_bgr)
        detail = str(occupancy.get("occupancy_detail", "") or "").strip()
        return {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "image_path": str(self.latest_capture_path.resolve()),
            "occupied": bool(occupancy.get("occupied", False)),
            "occupancy_score": float(occupancy.get("occupancy_score", 0.0) or 0.0),
            "occupancy_detail": detail,
            "detail": detail or ("Specimen still visible in the sexing chamber." if occupancy.get("occupied") else "Sexing chamber appears clear."),
        }

    def _parse_prediction(self, image_bgr: cv2.typing.MatLike) -> Dict[str, Any]:
        errors: List[str] = []
        label = "UNCERTAIN"
        confidence = 0.0

        if self._model is None or not self._model_ready:
            errors.append("MODEL_MISSING")
            if self._model_error:
                errors.append(self._model_error)
            return {"label": label, "confidence": confidence, "errors": errors}

        try:
            results = self._model(image_bgr, verbose=False)
        except Exception as exc:
            errors.append(f"CLASSIFIER_FAILED: {exc}")
            return {"label": label, "confidence": confidence, "errors": errors}

        if not results:
            errors.append("NO_RESULT")
            return {"label": label, "confidence": confidence, "errors": errors}

        probs = getattr(results[0], "probs", None)
        if probs is None:
            errors.append("NO_PROBS")
            return {"label": label, "confidence": confidence, "errors": errors}

        top1_idx = int(probs.top1)
        top1_conf = float(probs.top1conf)
        raw_label = str(results[0].names[top1_idx]).strip().lower()
        if "female" in raw_label:
            mapped = "female"
        elif "male" in raw_label:
            mapped = "male"
        else:
            mapped = "unknown"
            errors.append(f"UNKNOWN_CLASS:{raw_label}")

        confidence = top1_conf
        threshold = float(self.settings.sexing_uncertain_threshold)
        if mapped == "unknown" or top1_conf < threshold:
            label = "UNCERTAIN"
            errors.append(f"LOW_CONF:{top1_conf:.3f}")
        else:
            label = mapped

        return {"label": label, "confidence": confidence, "errors": errors}

    def classify(self) -> Dict[str, Any]:
        errors: List[str] = []
        try:
            self._capture_with_rpicam(self.latest_capture_path)
        except Exception as exc:
            errors.append(f"CAPTURE_FAILED: {exc}")
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": "UNCERTAIN",
                "confidence": 0.0,
                "image_path": str(self.latest_capture_path),
                "errors": errors,
                "detail": "; ".join(errors),
                "uncertain": True,
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": "; ".join(errors),
            }

        image_bgr = cv2.imread(str(self.latest_capture_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            errors.append("LOAD_FAILED")
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": "UNCERTAIN",
                "confidence": 0.0,
                "image_path": str(self.latest_capture_path),
                "errors": errors,
                "detail": "; ".join(errors),
                "uncertain": True,
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": "LOAD_FAILED",
            }

        occupancy = self._analyze_occupancy(image_bgr)
        occupied = bool(occupancy.get("occupied", False))
        occupancy_score = float(occupancy.get("occupancy_score", 0.0) or 0.0)
        occupancy_detail = str(occupancy.get("occupancy_detail", "") or "").strip()

        if not occupied:
            errors.append("CHAMBER_EMPTY")
            if occupancy_detail:
                errors.append(occupancy_detail)
            detail = "No specimen detected in the sexing chamber."
            if occupancy_detail:
                detail = f"{detail} {occupancy_detail}"
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": "UNCERTAIN",
                "confidence": 0.0,
                "image_path": str(self.latest_capture_path.resolve()),
                "errors": errors,
                "detail": detail,
                "uncertain": True,
                "occupied": False,
                "occupancy_score": occupancy_score,
                "occupancy_detail": occupancy_detail,
            }

        parsed = self._parse_prediction(image_bgr)
        errors.extend(parsed["errors"])
        label = str(parsed["label"])
        confidence = float(parsed["confidence"])
        uncertain = label == "UNCERTAIN"
        detail = "; ".join(errors) if errors else "Model classified successfully."
        return {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "label": label,
            "confidence": confidence,
            "image_path": str(self.latest_capture_path.resolve()),
            "errors": errors,
            "detail": detail,
            "uncertain": uncertain,
            "occupied": True,
            "occupancy_score": occupancy_score,
            "occupancy_detail": occupancy_detail,
        }
