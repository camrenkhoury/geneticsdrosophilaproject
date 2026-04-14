from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

import cv2

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
        }
