from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import cv2

from ..bootstrap import PROJECT_ROOT, ensure_repo_paths
from ..settings import OperatorSettings, resolve_repo_path

ensure_repo_paths()
from brio_channel_cli import calibrate_channel, capture_brio_background  # noqa: E402
from camera_sources import BrioCamera, BrioConfig, describe_camera_selection  # noqa: E402
from fly_x_detector import process_fly_detection  # noqa: E402


class ChannelError(RuntimeError):
    pass


class ChannelService:
    def __init__(self, settings: OperatorSettings):
        self.settings = settings
        self.output_dir = resolve_repo_path(settings.channel_output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._camera_descriptor_cache: Optional[str] = None

    @property
    def background_path(self) -> Path:
        return resolve_repo_path(self.settings.channel_background_path)

    @property
    def calibration_path(self) -> Path:
        return resolve_repo_path(self.settings.channel_calibration_path)

    @property
    def raw_image_path(self) -> Path:
        return self.output_dir / "last_channel_raw.png"

    @property
    def annotated_image_path(self) -> Path:
        return self.output_dir / "last_channel_annotated.png"

    @property
    def mask_image_path(self) -> Path:
        return self.output_dir / "last_channel_mask.png"

    @property
    def result_json_path(self) -> Path:
        return self.output_dir / "last_channel_result.json"

    def camera_descriptor_text(self) -> str:
        if self._camera_descriptor_cache is not None:
            return self._camera_descriptor_cache
        try:
            descriptor = describe_camera_selection(
                self.settings.channel_device,
                role="channel",
                preferred_hint=self.settings.channel_preferred_hint,
            )
        except Exception as exc:
            self._camera_descriptor_cache = f"Channel camera unavailable: {exc}"
            return self._camera_descriptor_cache
        if descriptor is None:
            self._camera_descriptor_cache = "Channel camera unavailable"
            return self._camera_descriptor_cache
        self._camera_descriptor_cache = f"{descriptor.card_name} ({descriptor.stable_path})"
        return self._camera_descriptor_cache

    def status(self) -> Dict[str, Any]:
        return {
            "background_ready": self.background_path.exists(),
            "calibration_ready": self.calibration_path.exists(),
            "result_ready": self.result_json_path.exists(),
            "camera": self.camera_descriptor_text(),
            "background_path": str(self.background_path),
            "calibration_path": str(self.calibration_path),
        }

    def capture_background(self) -> Path:
        self.background_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            capture_brio_background(
                output_path=self.background_path,
                device=self.settings.channel_device,
                width=int(self.settings.channel_width),
                height=int(self.settings.channel_height),
                fps=int(self.settings.channel_fps),
                frame_count=15,
            )
        except Exception as exc:
            raise ChannelError(f"Channel background capture failed: {exc}") from exc
        return self.background_path

    def calibrate(self) -> Path:
        if not self.background_path.exists():
            raise ChannelError("Channel background is missing. Capture a background first.")
        self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibrate_channel(
            background_path=self.background_path,
            calibration_path=self.calibration_path,
            channel_mm=float(self.settings.channel_mm),
        )
        return self.calibration_path

    def _capture_frame(self):
        try:
            with BrioCamera(
                BrioConfig(
                    device=self.settings.channel_device,
                    width=int(self.settings.channel_width),
                    height=int(self.settings.channel_height),
                    fps=int(self.settings.channel_fps),
                    preferred_hint=self.settings.channel_preferred_hint,
                )
            ) as camera:
                return camera.read()
        except Exception as exc:
            raise ChannelError(f"Channel capture failed: {exc}") from exc

    def capture_channel(self) -> Dict[str, Any]:
        if not self.background_path.exists():
            raise ChannelError("Channel background missing. Capture or import a background first.")
        if not self.calibration_path.exists():
            raise ChannelError("Channel calibration missing. Calibrate the channel first.")

        frame_bgr = self._capture_frame()
        result, annotated, mask = process_fly_detection(
            background=str(self.background_path),
            frame=frame_bgr,
            calibration_path=str(self.calibration_path),
            channel_mm=float(self.settings.channel_mm),
            band_half_width=int(self.settings.channel_band_half_width),
            score_thresh=int(self.settings.channel_score_thresh),
            no_align=bool(self.settings.channel_no_align),
        )

        cv2.imwrite(str(self.raw_image_path), frame_bgr)
        cv2.imwrite(str(self.annotated_image_path), annotated)
        cv2.imwrite(str(self.mask_image_path), mask)
        result.update(
            {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "raw_image_path": str(self.raw_image_path.resolve()),
                "annotated_image_path": str(self.annotated_image_path.resolve()),
                "mask_image_path": str(self.mask_image_path.resolve()),
            }
        )
        with self.result_json_path.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
        return result

    def save_result(self, payload: Dict[str, Any]) -> Path:
        data = dict(payload)
        data.setdefault("raw_image_path", str(self.raw_image_path.resolve()) if self.raw_image_path.exists() else "")
        data.setdefault(
            "annotated_image_path",
            str(self.annotated_image_path.resolve()) if self.annotated_image_path.exists() else "",
        )
        data.setdefault("mask_image_path", str(self.mask_image_path.resolve()) if self.mask_image_path.exists() else "")
        data.setdefault("result_json_path", str(self.result_json_path.resolve()))
        self.result_json_path.parent.mkdir(parents=True, exist_ok=True)
        with self.result_json_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        return self.result_json_path

    def load_last_result(self) -> Optional[Dict[str, Any]]:
        if not self.result_json_path.exists():
            return None
        with self.result_json_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("raw_image_path", str(self.raw_image_path.resolve()) if self.raw_image_path.exists() else "")
        data.setdefault(
            "annotated_image_path",
            str(self.annotated_image_path.resolve()) if self.annotated_image_path.exists() else "",
        )
        data.setdefault("mask_image_path", str(self.mask_image_path.resolve()) if self.mask_image_path.exists() else "")
        data.setdefault("result_json_path", str(self.result_json_path.resolve()))
        return data
