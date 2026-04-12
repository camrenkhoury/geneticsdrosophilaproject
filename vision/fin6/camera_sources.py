
#!/usr/bin/env python3
"""
Camera helpers for the Brio USB camera and Raspberry Pi HQ camera (IMX477).

The Brio path uses OpenCV / V4L2.
The IMX477 path uses Picamera2 / libcamera and returns BGR numpy arrays
for direct OpenCV processing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence, Union

import cv2
import numpy as np


class CameraError(RuntimeError):
    """Raised when a camera cannot be opened or read."""


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


@dataclass
class BrioConfig:
    device: Union[str, int] = "/dev/video8"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    use_mjpg: bool = True
    warmup_frames: int = 15
    read_retries: int = 8
    read_retry_sleep_s: float = 0.05
    flush_grabs: int = 0


class BrioCamera:
    """
    Simple OpenCV/V4L2 camera wrapper for a Logitech Brio or similar UVC camera.
    """

    def __init__(self, config: Optional[BrioConfig] = None) -> None:
        self.config = config or BrioConfig()
        self.cap: Optional[cv2.VideoCapture] = None
        self._open_mode = "uninitialized"

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

        # Best-effort hint; some cameras/drivers ignore this.
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

    def start(self) -> "BrioCamera":
        device = _normalize_cv2_device(self.config.device)
        attempts: list[tuple[str, Optional[int], bool]] = [
            ("v4l2+mjpg", cv2.CAP_V4L2, True),
            ("v4l2", cv2.CAP_V4L2, False),
            ("default+mjpg", None, True),
            ("default", None, False),
        ]

        if not self.config.use_mjpg:
            attempts = [
                ("v4l2", cv2.CAP_V4L2, False),
                ("default", None, False),
            ]

        errors: list[str] = []
        for label, backend, use_mjpg in attempts:
            cap = self._open_capture(device, backend)
            if not cap.isOpened():
                errors.append(f"{label}: open failed")
                cap.release()
                continue

            self._configure_capture(cap, use_mjpg=use_mjpg)
            if self._warmup_capture(cap):
                self.cap = cap
                self._open_mode = label
                return self

            errors.append(f"{label}: opened but no readable frames")
            cap.release()

        raise CameraError(
            f"Could not open/read Brio/UVC camera at {self.config.device!r}. "
            f"Tried: {', '.join(errors)}"
        )

    def read(self) -> np.ndarray:
        if self.cap is None:
            raise CameraError("Brio camera has not been started.")
        attempts = max(1, int(self.config.read_retries))
        for _ in range(attempts):
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
            time.sleep(float(self.config.read_retry_sleep_s))
        raise CameraError(
            f"Could not read a frame from the Brio camera after {attempts} attempts "
            f"(open mode: {self._open_mode})."
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

        hint = ""
        if errors and any("camera_init_sequence" in err or "Camera __init__ sequence" in err for err in errors):
            hint = " The requested Picamera2 index may be wrong or the camera may still be busy; try a different assay camera index in the GUI."
        raise CameraError(
            f"Could not initialize the Pi HQ camera. Tried {', '.join(self._candidate_camera_indices(Picamera2))}. "
            f"Errors: {'; '.join(errors)}.{hint}"
        )

    def read(self) -> np.ndarray:
        if self.picam2 is None:
            raise CameraError("Pi HQ camera has not been started.")
        arr = self.picam2.capture_array()
        if arr is None:
            raise CameraError("Could not capture an array from the Pi HQ camera.")
        if arr.ndim == 3 and arr.shape[2] == 3:
            # Picamera2 returns RGB888 here; convert to BGR for OpenCV code.
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


def capture_background_image(
    camera,
    frame_count: int = 15,
    frame_sleep_s: float = 0.03,
) -> np.ndarray:
    """
    Capture a short burst and median-combine it into a single static background,
    similar in spirit to FreeClimber's background estimation.
    """
    frames = camera.read_many(frame_count, sleep_s=frame_sleep_s)
    return median_background(frames)


def open_assay_camera(
    camera_backend: str = "opencv",
    width: int = 1536,
    height: int = 864,
    fps: float = 10.0,
    camera_index: int = 0,
    camera_device: Union[str, int] = "/dev/video10",
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
    return BrioCamera(
        BrioConfig(
            device=camera_device,
            width=int(width),
            height=int(height),
            fps=max(1, int(round(float(fps)))),
            flush_grabs=2,
        )
    )
