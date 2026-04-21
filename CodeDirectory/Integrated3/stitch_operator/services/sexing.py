from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        self.latest_debug_path = self.capture_dir / "latest_error_detection.jpg"
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

    def _safe_int(self, value: Any, default: int, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = int(default)
        parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def _safe_float(self, value: Any, default: float, *, minimum: float = 0.0, maximum: Optional[float] = None) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = float(default)
        parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def _odd_kernel(self, value: Any, default: int) -> int:
        size = self._safe_int(value, default, minimum=1, maximum=99)
        if size % 2 == 0:
            size = size + 1 if size < 99 else size - 1
        return max(1, size)

    def _error_detection_config(self) -> Dict[str, Any]:
        return {
            "corner_sample_px": self._safe_int(getattr(self.settings, "sexing_error_corner_sample_px", 20), 20, minimum=4, maximum=256),
            "bg_tolerance": self._safe_int(getattr(self.settings, "sexing_error_bg_tolerance", 65), 65, minimum=4, maximum=255),
            "open_kernel_size": self._odd_kernel(getattr(self.settings, "sexing_error_open_kernel_size", 3), 3),
            "close_kernel_size": self._odd_kernel(getattr(self.settings, "sexing_error_close_kernel_size", 11), 11),
            "erode_kernel_size": self._odd_kernel(getattr(self.settings, "sexing_error_erode_kernel_size", 11), 11),
            "erode_iterations": self._safe_int(getattr(self.settings, "sexing_error_erode_iterations", 9), 9, minimum=0, maximum=50),
            "single_fly_min_frac": self._safe_float(getattr(self.settings, "sexing_error_single_fly_min_frac", 0.001), 0.001, minimum=0.0, maximum=0.25),
            "single_fly_max_area_px": self._safe_float(getattr(self.settings, "sexing_error_single_fly_max_area_px", 40000), 40000.0, minimum=10.0),
        }

    def _write_debug_image(self, image_bgr: Optional[cv2.typing.MatLike]) -> str:
        if image_bgr is None:
            return ""
        try:
            self.latest_debug_path.parent.mkdir(parents=True, exist_ok=True)
            ok, encoded = cv2.imencode(".jpg", image_bgr)
            if not ok:
                return ""
            self.latest_debug_path.write_bytes(encoded.tobytes())
            return str(self.latest_debug_path.resolve())
        except Exception:
            return ""

    def _subtract_background(self, image_bgr: cv2.typing.MatLike, *, sample_size: int, tolerance: int) -> cv2.typing.MatLike:
        corner_patch = image_bgr[:sample_size, :sample_size]
        if corner_patch.size == 0:
            return np.zeros(image_bgr.shape[:2], dtype=np.uint8) if np is not None else cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        if np is not None:
            bg_color = np.median(corner_patch.reshape(-1, 3), axis=0)
            diff = np.abs(image_bgr.astype(np.int32) - bg_color.astype(np.int32))
            dist = np.max(diff, axis=2)
            return np.where(dist > tolerance, 255, 0).astype(np.uint8)

        ref = [float(v) for v in cv2.mean(corner_patch)[:3]]
        diff_b = cv2.absdiff(image_bgr[:, :, 0], np.full(image_bgr.shape[:2], int(ref[0]), dtype="uint8"))
        diff_g = cv2.absdiff(image_bgr[:, :, 1], np.full(image_bgr.shape[:2], int(ref[1]), dtype="uint8"))
        diff_r = cv2.absdiff(image_bgr[:, :, 2], np.full(image_bgr.shape[:2], int(ref[2]), dtype="uint8"))
        dist = cv2.max(diff_b, cv2.max(diff_g, diff_r))
        _, mask = cv2.threshold(dist, tolerance, 255, cv2.THRESH_BINARY)
        return mask

    def _clean_mask(self, mask: cv2.typing.MatLike, config: Dict[str, Any]) -> cv2.typing.MatLike:
        open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(config["open_kernel_size"]), int(config["open_kernel_size"])))
        close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(config["close_kernel_size"]), int(config["close_kernel_size"])))
        erode_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(config["erode_kernel_size"]), int(config["erode_kernel_size"])))
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_kernel, iterations=2)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, close_kernel, iterations=2)
        erode_iterations = int(config["erode_iterations"])
        if erode_iterations > 0:
            cleaned = cv2.erode(cleaned, erode_kernel, iterations=erode_iterations)
        return cleaned

    def _count_flies(self, image_bgr: cv2.typing.MatLike, *, debug: bool = False) -> Dict[str, Any]:
        if image_bgr is None:
            return {
                "count": 0,
                "detail": "COUNT_FAILED:missing_image",
                "errors": ["COUNT_FAILED:missing_image"],
                "debug_image": None,
                "mask": None,
                "largest_area": 0.0,
            }

        config = self._error_detection_config()
        height, width = image_bgr.shape[:2]
        image_area = float(max(1, height * width))
        min_area = float(config["single_fly_min_frac"]) * image_area
        max_area = max(float(min_area) + 1.0, float(config["single_fly_max_area_px"]))

        mask = self._subtract_background(
            image_bgr,
            sample_size=int(config["corner_sample_px"]),
            tolerance=int(config["bg_tolerance"]),
        )
        mask = self._clean_mask(mask, config)
        contours_info = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = contours_info[0] if len(contours_info) == 2 else contours_info[1]

        count = 0
        largest_area = 0.0
        blob_summaries: List[tuple[Any, int, float]] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            largest_area = max(largest_area, area)
            if area < min_area:
                blob_summaries.append((contour, 0, area))
                continue
            if area <= max_area:
                count += 1
                blob_summaries.append((contour, 1, area))
            else:
                count += 2
                blob_summaries.append((contour, 2, area))

        debug_image = None
        if debug:
            debug_image = image_bgr.copy()
            for contour, blob_count, _area in blob_summaries:
                color = (180, 180, 180) if blob_count == 0 else (0, 220, 0) if blob_count == 1 else (0, 100, 255)
                cv2.drawContours(debug_image, [contour], -1, color, 2)
                moments = cv2.moments(contour)
                if moments.get("m00"):
                    cx = int(moments["m10"] / moments["m00"])
                    cy = int(moments["m01"] / moments["m00"])
                    cv2.putText(
                        debug_image,
                        str(blob_count),
                        (cx - 10, cy + 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.0,
                        color,
                        2,
                    )
            cv2.putText(
                debug_image,
                f"Count: {count}",
                (20, 48),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (0, 0, 255),
                2,
            )

        errors: List[str] = []
        if count <= 0:
            errors.append("NO_FLY_DETECTED")
        elif count > 1:
            errors.append(f"MULTIPLE_FLIES:{count}")

        detail = (
            f"count={count}; largest_area={largest_area:.1f}; min_area={min_area:.1f}; "
            f"max_single_area={max_area:.1f}; blobs={len(contours)}; bg_tol={int(config['bg_tolerance'])}"
        )
        return {
            "count": int(count),
            "detail": detail,
            "errors": errors,
            "debug_image": debug_image,
            "mask": mask,
            "largest_area": largest_area,
        }

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

    def inspect_chamber(self, debug: bool = False) -> Dict[str, Any]:
        try:
            self._capture_with_rpicam(self.latest_capture_path)
        except Exception as exc:
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "image_path": str(self.latest_capture_path),
                "debug_image_path": "",
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": f"CAPTURE_FAILED:{exc}",
                "count": 0,
                "errors": [f"CAPTURE_FAILED:{exc}"],
                "detail": f"CAPTURE_FAILED:{exc}",
            }

        image_bgr = cv2.imread(str(self.latest_capture_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "image_path": str(self.latest_capture_path.resolve()),
                "debug_image_path": "",
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": "LOAD_FAILED",
                "count": 0,
                "errors": ["LOAD_FAILED"],
                "detail": "LOAD_FAILED",
            }

        occupancy = self._analyze_occupancy(image_bgr)
        count_info = self._count_flies(image_bgr, debug=debug)
        count = int(count_info.get("count", 0) or 0)
        occupied = bool(count > 0 or occupancy.get("occupied", False))
        occupancy_detail = str(occupancy.get("occupancy_detail", "") or "").strip()
        debug_image_path = self._write_debug_image(count_info.get("debug_image")) if debug else ""

        detail_bits: List[str] = []
        if count > 1:
            detail_bits.append(f"Multiple specimens detected in the sexing chamber (count={count}).")
        elif count == 1:
            detail_bits.append("One specimen detected in the sexing chamber.")
        elif occupied:
            detail_bits.append("The sexing chamber appears occupied, but blob counting returned 0.")
        else:
            detail_bits.append("Sexing chamber appears clear.")
        if count_info.get("detail"):
            detail_bits.append(str(count_info.get("detail")))
        if occupancy_detail:
            detail_bits.append(occupancy_detail)

        errors = [str(item) for item in count_info.get("errors", [])]
        if occupied and count <= 0 and "CHAMBER_OCCUPIED_UNCOUNTED" not in errors:
            errors.append("CHAMBER_OCCUPIED_UNCOUNTED")

        return {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "image_path": str(self.latest_capture_path.resolve()),
            "debug_image_path": debug_image_path,
            "occupied": occupied,
            "occupancy_score": float(occupancy.get("occupancy_score", 0.0) or 0.0),
            "occupancy_detail": occupancy_detail,
            "count": count,
            "errors": errors,
            "detail": " ".join(bit for bit in detail_bits if bit).strip(),
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

    def classify(self, debug: bool = False) -> Dict[str, Any]:
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
                "debug_image_path": "",
                "errors": errors,
                "detail": "; ".join(errors),
                "uncertain": True,
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": "; ".join(errors),
                "count": 0,
            }

        image_bgr = cv2.imread(str(self.latest_capture_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            errors.append("LOAD_FAILED")
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": "UNCERTAIN",
                "confidence": 0.0,
                "image_path": str(self.latest_capture_path),
                "debug_image_path": "",
                "errors": errors,
                "detail": "; ".join(errors),
                "uncertain": True,
                "occupied": False,
                "occupancy_score": 0.0,
                "occupancy_detail": "LOAD_FAILED",
                "count": 0,
            }

        occupancy = self._analyze_occupancy(image_bgr)
        count_info = self._count_flies(image_bgr, debug=debug)
        count = int(count_info.get("count", 0) or 0)
        occupied = bool(count > 0 or occupancy.get("occupied", False))
        occupancy_score = float(occupancy.get("occupancy_score", 0.0) or 0.0)
        occupancy_detail = str(occupancy.get("occupancy_detail", "") or "").strip()
        debug_image_path = self._write_debug_image(count_info.get("debug_image")) if debug else ""

        errors.extend(str(item) for item in count_info.get("errors", []))

        if count <= 0:
            if occupied and "CHAMBER_OCCUPIED_UNCOUNTED" not in errors:
                errors.append("CHAMBER_OCCUPIED_UNCOUNTED")
                detail = "The sexing chamber appears occupied, but counting did not isolate a single fly."
            else:
                errors.append("CHAMBER_EMPTY")
                detail = "No specimen detected in the sexing chamber."
            if count_info.get("detail"):
                detail = f"{detail} {count_info.get('detail')}"
            if occupancy_detail:
                detail = f"{detail} {occupancy_detail}"
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": "UNCERTAIN",
                "confidence": 0.0,
                "image_path": str(self.latest_capture_path.resolve()),
                "debug_image_path": debug_image_path,
                "errors": errors,
                "detail": detail.strip(),
                "uncertain": True,
                "occupied": occupied,
                "occupancy_score": occupancy_score,
                "occupancy_detail": occupancy_detail,
                "count": count,
            }

        if count > 1:
            detail_bits = [f"Multiple specimens detected in the sexing chamber (count={count})."]
            if count_info.get("detail"):
                detail_bits.append(str(count_info.get("detail")))
            if occupancy_detail:
                detail_bits.append(occupancy_detail)
            return {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": "UNCERTAIN",
                "confidence": 0.0,
                "image_path": str(self.latest_capture_path.resolve()),
                "debug_image_path": debug_image_path,
                "errors": errors,
                "detail": " ".join(bit for bit in detail_bits if bit).strip(),
                "uncertain": True,
                "occupied": True,
                "occupancy_score": occupancy_score,
                "occupancy_detail": occupancy_detail,
                "count": count,
            }

        parsed = self._parse_prediction(image_bgr)
        errors.extend(parsed["errors"])
        label = str(parsed["label"])
        confidence = float(parsed["confidence"])
        uncertain = label == "UNCERTAIN"
        detail_bits = []
        if errors:
            detail_bits.append("; ".join(errors))
        else:
            detail_bits.append("Model classified successfully.")
        if count_info.get("detail"):
            detail_bits.append(str(count_info.get("detail")))
        return {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "label": label,
            "confidence": confidence,
            "image_path": str(self.latest_capture_path.resolve()),
            "debug_image_path": debug_image_path,
            "errors": errors,
            "detail": " ".join(bit for bit in detail_bits if bit).strip(),
            "uncertain": uncertain,
            "occupied": True,
            "occupancy_score": occupancy_score,
            "occupancy_detail": occupancy_detail,
            "count": count,
        }
