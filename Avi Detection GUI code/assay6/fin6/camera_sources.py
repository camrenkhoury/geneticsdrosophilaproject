#!/usr/bin/env python3
"""
Camera helpers for the Brio USB camera and assay camera(s).

This module now supports stable role-based selection so the GUI can default to:
- the Logitech Brio for channel mode
- the other non-Brio capture camera for assay mode

It prefers stable `/dev/v4l/by-id/...` paths when available, then `/dev/v4l/by-path/...`,
then falls back to `/dev/videoN`.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence, Union

import cv2
import numpy as np


class CameraError(RuntimeError):
    """Raised when a camera cannot be opened or read."""


@dataclass
class CameraDescriptor:
    device_path: str
    stable_path: str
    symlink_name: str
    card_name: str
    index: int
    is_brio: bool
    by_id_path: Optional[str] = None
    by_path_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_cv2_device(device: Union[str, int]) -> Union[str, int]:
    if isinstance(device, int):
        return device
    s = str(device).strip()
    if s.isdigit():
        return int(s)
    return s


def normalize_assay_camera_backend(camera_backend: str) -> str:
    backend = str(camera_backend or "opencv").strip().lower()
    aliases = {
        "opencv": "opencv",
        "usb": "opencv",
        "uvc": "opencv",
        "webcam": "opencv",
        "pihq": "pihq",
        "picamera2": "pihq",
        "imx477": "pihq",
    }
    if backend not in aliases:
        raise CameraError(
            f"Unsupported assay camera backend {camera_backend!r}. "
            "Use 'opencv' or 'pihq'."
        )
    return aliases[backend]


def save_image(path: str, image_bgr: np.ndarray) -> str:
    ok = cv2.imwrite(path, image_bgr)
    if not ok:
        raise CameraError(f"Failed to save image: {path}")
    return path


def median_background(frames: Sequence[np.ndarray]) -> np.ndarray:
    if not frames:
        raise CameraError("No frames were provided for background generation.")
    stack = np.stack(frames, axis=0).astype(np.uint8)
    return np.median(stack, axis=0).astype(np.uint8)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def list_video_devices(prefer_index_zero: bool = True) -> list[CameraDescriptor]:
    sys_root = Path("/sys/class/video4linux")
    if not sys_root.exists():
        return []
    by_id_dir = Path("/dev/v4l/by-id")
    by_path_dir = Path("/dev/v4l/by-path")
    by_id_links = list(by_id_dir.glob("*")) if by_id_dir.exists() else []
    by_path_links = list(by_path_dir.glob("*")) if by_path_dir.exists() else []

    out: list[CameraDescriptor] = []
    for node in sorted(sys_root.glob("video*")):
        device_name = node.name
        dev_path = f"/dev/{device_name}"
        index_raw = _read_text(node / "index")
        index = int(index_raw) if index_raw.isdigit() else -1
        if prefer_index_zero and index not in {-1, 0}:
            continue
        card_name = _read_text(node / "name") or device_name

        resolved = Path(dev_path)
        by_id_path = None
        symlink_name = ""
        for link in by_id_links:
            try:
                if link.resolve() == resolved.resolve():
                    by_id_path = str(link)
                    symlink_name = link.name
                    break
            except Exception:
                continue
        by_path_path = None
        for link in by_path_links:
            try:
                if link.resolve() == resolved.resolve():
                    by_path_path = str(link)
                    if not symlink_name:
                        symlink_name = link.name
                    break
            except Exception:
                continue
        stable_path = by_id_path or by_path_path or dev_path
        text_blob = " ".join(part.lower() for part in [card_name, symlink_name, stable_path] if part)
        is_brio = "brio" in text_blob or ("logitech" in text_blob and "046d" in text_blob)
        out.append(
            CameraDescriptor(
                device_path=dev_path,
                stable_path=stable_path,
                symlink_name=symlink_name,
                card_name=card_name,
                index=index,
                is_brio=is_brio,
                by_id_path=by_id_path,
                by_path_path=by_path_path,
            )
        )
    out.sort(key=lambda item: (0 if item.is_brio else 1, item.card_name.lower(), item.device_path))
    return out


def resolve_camera_device(
    device_reference: Union[str, int, None],
    *,
    role: Optional[str] = None,
    preferred_hint: str = "",
) -> Union[str, int]:
    if device_reference is None:
        device_reference = "auto"
    if isinstance(device_reference, int):
        return device_reference

    raw = str(device_reference).strip()
    if raw.isdigit():
        return int(raw)
    if raw and raw.startswith("/dev/") and Path(raw).exists():
        return raw
    if raw.startswith("name:"):
        preferred_hint = raw.split(":", 1)[1].strip()
        raw = "auto"

    devices = list_video_devices(prefer_index_zero=True)
    if not devices:
        if raw in {"", "auto", "auto:assay", "auto:channel"}:
            raise CameraError("No /dev/video* capture devices were discovered.")
        return raw

    hint = preferred_hint.strip().lower()
    if hint:
        for device in devices:
            haystack = " ".join([
                device.device_path.lower(),
                device.stable_path.lower(),
                device.symlink_name.lower(),
                device.card_name.lower(),
            ])
            if hint in haystack:
                return device.stable_path

    raw_norm = raw.lower()
    if raw_norm in {"", "auto", "auto:channel", "channel"} or role == "channel":
        for device in devices:
            if device.is_brio:
                return device.stable_path
        return devices[0].stable_path

    if raw_norm in {"auto:assay", "assay", "auto_assay"} or role == "assay":
        for device in devices:
            if not device.is_brio:
                return device.stable_path
        return devices[-1].stable_path

    if raw_norm.startswith("by-id:"):
        token = raw_norm.split(":", 1)[1]
        for device in devices:
            if device.by_id_path and token in device.by_id_path.lower():
                return device.by_id_path
    if raw_norm.startswith("by-path:"):
        token = raw_norm.split(":", 1)[1]
        for device in devices:
            if device.by_path_path and token in device.by_path_path.lower():
                return device.by_path_path

    for device in devices:
        haystack = " ".join([
            device.device_path.lower(),
            device.stable_path.lower(),
            device.symlink_name.lower(),
            device.card_name.lower(),
        ])
        if raw_norm and raw_norm in haystack:
            return device.stable_path
    return raw


def describe_camera_selection(
    device_reference: Union[str, int, None],
    *,
    role: Optional[str] = None,
    preferred_hint: str = "",
) -> Optional[CameraDescriptor]:
    resolved = resolve_camera_device(device_reference, role=role, preferred_hint=preferred_hint)
    devices = list_video_devices(prefer_index_zero=True)
    for device in devices:
        if resolved in {device.device_path, device.stable_path, device.by_id_path, device.by_path_path}:
            return device
    return None


@dataclass
class BrioConfig:
    device: Union[str, int] = "auto:channel"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    use_mjpg: bool = True
    warmup_frames: int = 15
    read_retries: int = 8
    read_retry_sleep_s: float = 0.05
    flush_grabs: int = 0
    preferred_hint: str = ""
    reopen_on_read_failure: bool = True
    reopen_delay_s: float = 0.20


class BrioCamera:
    """Simple OpenCV/V4L2 camera wrapper for a Logitech Brio or similar UVC camera."""

    def __init__(self, config: Optional[BrioConfig] = None) -> None:
        self.config = config or BrioConfig()
        self.cap: Optional[cv2.VideoCapture] = None
        self._open_mode = "uninitialized"
        self.resolved_device: Optional[Union[str, int]] = None
        self._open_errors: list[str] = []

    def _open_attempts(self) -> list[tuple[str, Optional[int], bool]]:
        attempts: list[tuple[str, Optional[int], bool]] = [
            ("v4l2+mjpg", cv2.CAP_V4L2, True),
            ("v4l2", cv2.CAP_V4L2, False),
            ("default+mjpg", None, True),
            ("default", None, False),
        ]
        if not self.config.use_mjpg:
            return [("v4l2", cv2.CAP_V4L2, False), ("default", None, False)]
        return attempts

    def _open_capture(self, device: Union[str, int], backend: Optional[int]) -> cv2.VideoCapture:
        if backend is None:
            return cv2.VideoCapture(device)
        return cv2.VideoCapture(device, backend)

    def _configure_capture(self, cap: cv2.VideoCapture, use_mjpg: bool) -> None:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.config.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.config.height))
        cap.set(cv2.CAP_PROP_FPS, int(self.config.fps))
        if use_mjpg:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def _warmup_capture(self, cap: cv2.VideoCapture) -> bool:
        success = False
        attempts = max(1, int(self.config.warmup_frames))
        for _ in range(attempts):
            ok, frame = cap.read()
            if ok and frame is not None:
                success = True
            else:
                time.sleep(float(self.config.read_retry_sleep_s))
        return success

    def _start_capture(self, device: Union[str, int]) -> "BrioCamera":
        errors: list[str] = []
        for label, backend, use_mjpg in self._open_attempts():
            cap = self._open_capture(device, backend)
            if not cap.isOpened():
                errors.append(f"{label}: open failed")
                cap.release()
                continue
            self._configure_capture(cap, use_mjpg=use_mjpg)
            if self._warmup_capture(cap):
                self.cap = cap
                self._open_mode = label
                self._open_errors = []
                return self
            errors.append(f"{label}: opened but no readable frames")
            cap.release()
        self._open_errors = errors
        raise CameraError(
            f"Could not open/read UVC camera at {device!r}. Tried: {', '.join(errors)}"
        )

    def _reopen_after_read_failure(self) -> bool:
        device = self.resolved_device
        if device is None:
            return False
        self.release()
        delay_s = max(0.0, float(getattr(self.config, "reopen_delay_s", 0.0)))
        if delay_s > 0:
            time.sleep(delay_s)
        try:
            self._start_capture(device)
            return True
        except Exception:
            return False

    def start(self) -> "BrioCamera":
        resolved = resolve_camera_device(self.config.device, role="channel", preferred_hint=self.config.preferred_hint)
        device = _normalize_cv2_device(resolved)
        self.resolved_device = device
        return self._start_capture(device)

    def _read_once(self) -> Optional[np.ndarray]:
        if self.cap is None:
            return None
        flush_grabs = max(0, int(getattr(self.config, "flush_grabs", 0)))
        for _flush_idx in range(flush_grabs):
            try:
                if not self.cap.grab():
                    break
            except Exception:
                break
        ok, frame = self.cap.read()
        if ok and frame is not None:
            return frame
        return None

    def read(self) -> np.ndarray:
        if self.cap is None:
            raise CameraError("Brio camera has not been started.")
        attempts = max(1, int(self.config.read_retries))
        for _ in range(attempts):
            frame = self._read_once()
            if frame is not None:
                return frame
            time.sleep(float(self.config.read_retry_sleep_s))
        reopened = False
        if bool(getattr(self.config, "reopen_on_read_failure", True)):
            reopened = self._reopen_after_read_failure()
            if reopened:
                for _ in range(attempts):
                    frame = self._read_once()
                    if frame is not None:
                        return frame
                    time.sleep(float(self.config.read_retry_sleep_s))
        recovery_note = " after capture reopen" if reopened else ""
        raise CameraError(
            f"Could not read a frame from the camera after {attempts} attempts "
            f"{recovery_note} (open mode: {self._open_mode}, device: {self.resolved_device!r})."
        )

    def read_many(self, count: int, sleep_s: float = 0.03) -> list[np.ndarray]:
        frames: list[np.ndarray] = []
        for _ in range(int(count)):
            frames.append(self.read())
            if sleep_s > 0:
                time.sleep(sleep_s)
        return frames

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self) -> "BrioCamera":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass
class PiHQConfig:
    width: int = 1536
    height: int = 864
    fps: float = 10.0
    warmup_seconds: float = 1.5
    camera_index: int = 0


class PiHQCamera:
    """
    Picamera2/libcamera wrapper for the Raspberry Pi High Quality Camera (IMX477).
    Import is lazy so the rest of the codebase can still be linted/tested on a
    machine without Picamera2 installed.
    """

    def __init__(self, config: Optional[PiHQConfig] = None) -> None:
        self.config = config or PiHQConfig()
        self.picam2 = None
        self.camera_index_in_use: Optional[int] = None

    def _candidate_camera_indices(self, Picamera2) -> list[int]:
        requested = int(self.config.camera_index)
        indices = [requested]
        try:
            info = Picamera2.global_camera_info()
        except Exception:
            info = None
        if info:
            for idx, _item in enumerate(info):
                if idx not in indices:
                    indices.append(idx)
        return indices

    def _start_with_index(self, Picamera2, camera_index: int) -> "PiHQCamera":
        picam2 = Picamera2(int(camera_index))
        video_config = picam2.create_video_configuration(
            main={"size": (int(self.config.width), int(self.config.height)), "format": "RGB888"},
            controls={"FrameRate": float(self.config.fps)},
            queue=False,
        )
        picam2.configure(video_config)
        picam2.start()
        self.picam2 = picam2
        self.camera_index_in_use = int(camera_index)
        time.sleep(max(0.0, float(self.config.warmup_seconds)))
        return self

    def start(self) -> "PiHQCamera":
        try:
            from picamera2 import Picamera2  # type: ignore
        except Exception as exc:
            raise CameraError(
                "Picamera2 is required for the IMX477 path. Install it with "
                "`sudo apt install python3-picamera2` on Raspberry Pi OS."
            ) from exc

        errors: list[str] = []
        for camera_index in self._candidate_camera_indices(Picamera2):
            try:
                return self._start_with_index(Picamera2, camera_index)
            except Exception as exc:
                errors.append(f"index {camera_index}: {exc}")
                if self.picam2 is not None:
                    try:
                        self.picam2.stop()
                    except Exception:
                        pass
                    self.picam2 = None
        raise CameraError(
            f"Could not initialize the Pi HQ camera. Tried {self._candidate_camera_indices(Picamera2)}. "
            f"Errors: {'; '.join(errors)}."
        )

    def read(self) -> np.ndarray:
        if self.picam2 is None:
            raise CameraError("Pi HQ camera has not been started.")
        arr = self.picam2.capture_array()
        if arr is None:
            raise CameraError("Could not capture an array from the Pi HQ camera.")
        if arr.ndim == 3 and arr.shape[2] == 3:
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        if arr.ndim == 2:
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        raise CameraError(f"Unexpected Pi HQ frame shape: {arr.shape}")

    def read_many(self, count: int, sleep_s: float = 0.01) -> list[np.ndarray]:
        frames: list[np.ndarray] = []
        for _ in range(int(count)):
            frames.append(self.read())
            if sleep_s > 0:
                time.sleep(sleep_s)
        return frames

    def release(self) -> None:
        if self.picam2 is not None:
            try:
                self.picam2.stop()
            finally:
                self.picam2 = None
                self.camera_index_in_use = None

    def __enter__(self) -> "PiHQCamera":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def capture_background_image(camera, frame_count: int = 15, frame_sleep_s: float = 0.03) -> np.ndarray:
    frames = camera.read_many(frame_count, sleep_s=frame_sleep_s)
    return median_background(frames)


def open_assay_camera(
    camera_backend: str = "opencv",
    width: int = 1536,
    height: int = 864,
    fps: float = 10.0,
    camera_index: int = 0,
    camera_device: Union[str, int] = "auto:assay",
    preferred_hint: str = "",
    role: str = "assay",
):
    backend = normalize_assay_camera_backend(camera_backend)
    if backend == "pihq":
        return PiHQCamera(
            PiHQConfig(
                width=int(width),
                height=int(height),
                fps=float(fps),
                camera_index=int(camera_index),
            )
        )
    resolved_device = resolve_camera_device(camera_device, role=role, preferred_hint=preferred_hint)
    return BrioCamera(
        BrioConfig(
            device=resolved_device,
            width=int(width),
            height=int(height),
            fps=max(1, int(round(float(fps)))),
            flush_grabs=2,
            preferred_hint=preferred_hint,
        )
    )
