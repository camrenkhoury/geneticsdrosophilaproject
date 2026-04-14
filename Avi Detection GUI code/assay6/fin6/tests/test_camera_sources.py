import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from camera_sources import BrioCamera, BrioConfig, CameraError


class _FakeCapture:
    def __init__(self, reads):
        self._reads = list(reads)
        self.released = False

    def isOpened(self):
        return not self.released

    def set(self, *_args, **_kwargs):
        return True

    def read(self):
        if self.released:
            return False, None
        if self._reads:
            return self._reads.pop(0)
        return False, None

    def grab(self):
        return False

    def release(self):
        self.released = True


class CameraSourceTests(unittest.TestCase):
    def test_brio_camera_reopens_after_read_failures(self):
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        first_cap = _FakeCapture(
            [
                (True, frame),   # startup warmup
                (False, None),   # stale stream during recording
                (False, None),
            ]
        )
        second_cap = _FakeCapture(
            [
                (True, frame),   # reopen warmup
                (True, frame),   # successful recovered read
            ]
        )
        captures = [first_cap, second_cap]

        def _next_capture(_device, _backend):
            return captures.pop(0)

        camera = BrioCamera(
            BrioConfig(
                device="/dev/video0",
                warmup_frames=1,
                read_retries=2,
                read_retry_sleep_s=0.0,
                reopen_delay_s=0.0,
            )
        )

        with patch("camera_sources.resolve_camera_device", return_value="/dev/video0"), patch.object(
            BrioCamera, "_open_capture", side_effect=_next_capture
        ):
            camera.start()
            recovered = camera.read()

        self.assertIsNotNone(recovered)
        self.assertTrue(first_cap.released)
        self.assertIs(camera.cap, second_cap)

    def test_brio_camera_error_mentions_reopen_when_recovery_fails(self):
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        first_cap = _FakeCapture(
            [
                (True, frame),
                (False, None),
                (False, None),
            ]
        )
        second_cap = _FakeCapture(
            [
                (True, frame),
                (False, None),
                (False, None),
            ]
        )
        captures = [first_cap, second_cap]

        def _next_capture(_device, _backend):
            return captures.pop(0)

        camera = BrioCamera(
            BrioConfig(
                device="/dev/video0",
                warmup_frames=1,
                read_retries=2,
                read_retry_sleep_s=0.0,
                reopen_delay_s=0.0,
            )
        )

        with patch("camera_sources.resolve_camera_device", return_value="/dev/video0"), patch.object(
            BrioCamera, "_open_capture", side_effect=_next_capture
        ):
            camera.start()
            with self.assertRaises(CameraError) as ctx:
                camera.read()

        self.assertIn("after capture reopen", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
