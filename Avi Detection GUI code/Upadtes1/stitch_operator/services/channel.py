from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import cv2

from ..bootstrap import ensure_repo_paths
from ..settings import OperatorSettings, resolve_repo_path

ensure_repo_paths()
from brio_channel_cli import calibrate_channel, capture_brio_background  # noqa: E402
from camera_sources import BrioCamera, BrioConfig, describe_camera_selection  # noqa: E402
from fly_x_detector import load_calibration_data, process_fly_detection  # noqa: E402


class ChannelError(RuntimeError):
    pass


class ChannelService:
    def __init__(self, settings: OperatorSettings):
        self.settings = settings
        self.output_dir = resolve_repo_path(settings.channel_output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._camera_session: Optional[BrioCamera] = None
        self._background_cache: Optional[Dict[str, Any]] = None
        self._calibration_cache: Optional[Dict[str, Any]] = None
        self._camera_descriptor_cache: Optional[str] = None

    @property
    def background_path(self) -> Path:
        return resolve_repo_path(self.settings.channel_background_path)

    @property
    def calibration_path(self) -> Path:
        return resolve_repo_path(self.settings.channel_calibration_path)

    @property
    def raw_image_path(self) -> Path:
        return self.output_dir / "last_channel_raw.jpg"

    @property
    def annotated_image_path(self) -> Path:
        return self.output_dir / "last_channel_annotated.png"

    @property
    def mask_image_path(self) -> Path:
        return self.output_dir / "last_channel_mask.png"

    @property
    def result_json_path(self) -> Path:
        return self.output_dir / "last_channel_result.json"

    @property
    def auto_flow_master_path(self) -> Path:
        return self.output_dir / "auto_flow_master.json"

    def camera_descriptor_text(self) -> str:
        if self._camera_descriptor_cache:
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

    def _brio_config(self) -> BrioConfig:
        return BrioConfig(
            device=self.settings.channel_device,
            width=int(self.settings.channel_width),
            height=int(self.settings.channel_height),
            fps=int(self.settings.channel_fps),
            preferred_hint=self.settings.channel_preferred_hint,
            warmup_frames=8,
            flush_grabs=2,
        )

    @contextmanager
    def capture_session(self):
        if self._camera_session is not None:
            yield self
            return
        camera = BrioCamera(self._brio_config())
        try:
            self._camera_session = camera.start()
            yield self
        except Exception as exc:
            raise ChannelError(f"Channel capture session failed: {exc}") from exc
        finally:
            if self._camera_session is not None:
                try:
                    self._camera_session.release()
                finally:
                    self._camera_session = None

    def _load_background_frame(self):
        path = self.background_path
        mtime = path.stat().st_mtime
        cache = self._background_cache
        if cache is not None and cache.get("path") == str(path) and cache.get("mtime") == mtime:
            return cache["image"]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ChannelError(f"Could not read channel background image: {path}")
        self._background_cache = {"path": str(path), "mtime": mtime, "image": image}
        return image

    def _load_calibration_payload(self) -> Dict[str, Any]:
        path = self.calibration_path
        mtime = path.stat().st_mtime
        cache = self._calibration_cache
        if cache is not None and cache.get("path") == str(path) and cache.get("mtime") == mtime:
            return dict(cache["payload"])
        payload = load_calibration_data(path)
        self._calibration_cache = {"path": str(path), "mtime": mtime, "payload": dict(payload)}
        return dict(payload)

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
            self._background_cache = None
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
        self._calibration_cache = None
        return self.calibration_path

    def _write_image(self, path: Path, image_bgr) -> None:
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            cv2.imwrite(str(path), image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            return
        if suffix == ".png":
            cv2.imwrite(str(path), image_bgr, [int(cv2.IMWRITE_PNG_COMPRESSION), 1])
            return
        cv2.imwrite(str(path), image_bgr)

    def _capture_frame(self):
        try:
            if self._camera_session is not None:
                return self._camera_session.read()
            with BrioCamera(self._brio_config()) as camera:
                return camera.read()
        except Exception as exc:
            raise ChannelError(f"Channel capture failed: {exc}") from exc

    def capture_channel(self) -> Dict[str, Any]:
        if not self.background_path.exists():
            raise ChannelError("Channel background missing. Capture or import a background first.")
        if not self.calibration_path.exists():
            raise ChannelError("Channel calibration missing. Calibrate the channel first.")

        started = time.perf_counter()
        frame_bgr = self._capture_frame()
        capture_elapsed_s = time.perf_counter() - started

        background_bgr = self._load_background_frame()
        calibration = self._load_calibration_payload()

        process_started = time.perf_counter()
        result, annotated, mask = process_fly_detection(
            background=background_bgr,
            frame=frame_bgr,
            left_pt=tuple(map(int, calibration["left_point_px"])),
            right_pt=tuple(map(int, calibration["right_point_px"])),
            channel_mm=float(calibration.get("channel_length_mm", self.settings.channel_mm)),
            band_half_width=int(self.settings.channel_band_half_width),
            score_thresh=int(self.settings.channel_score_thresh),
            no_align=bool(self.settings.channel_no_align),
            crop_x_pad=calibration.get("crop_x_pad"),
            crop_above_px=calibration.get("crop_above_px"),
            crop_below_px=calibration.get("crop_below_px"),
        )
        processing_elapsed_s = time.perf_counter() - process_started

        self._write_image(self.raw_image_path, frame_bgr)
        self._write_image(self.annotated_image_path, annotated)

        mask_image_path = ""
        if bool(getattr(self.settings, "channel_save_mask_image", False)):
            self._write_image(self.mask_image_path, mask)
            mask_image_path = str(self.mask_image_path.resolve())

        total_elapsed_s = time.perf_counter() - started
        result.update(
            {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "raw_image_path": str(self.raw_image_path.resolve()),
                "annotated_image_path": str(self.annotated_image_path.resolve()),
                "mask_image_path": mask_image_path,
                "timings": {
                    "frame_capture_s": round(capture_elapsed_s, 3),
                    "processing_s": round(processing_elapsed_s, 3),
                    "total_s": round(total_elapsed_s, 3),
                },
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
        data.setdefault("mask_image_path", "")
        data.setdefault("result_json_path", str(self.result_json_path.resolve()))
        self.result_json_path.parent.mkdir(parents=True, exist_ok=True)
        with self.result_json_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        return self.result_json_path

    def save_auto_flow_master(self, payload: Dict[str, Any]) -> Path:
        self.auto_flow_master_path.parent.mkdir(parents=True, exist_ok=True)
        with self.auto_flow_master_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return self.auto_flow_master_path

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
        data.setdefault("mask_image_path", "")
        data.setdefault("result_json_path", str(self.result_json_path.resolve()))
        return data
