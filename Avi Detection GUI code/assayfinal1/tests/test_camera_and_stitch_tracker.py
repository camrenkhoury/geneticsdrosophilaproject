import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from assay_processing import SnapshotStitchTracker
from assay_tracking import Detection, VialCalibration, build_assay_calibration
from camera_sources import CameraDescriptor, CameraError, capture_candidate_devices, resolve_camera_device


class CameraSelectionTests(unittest.TestCase):
    def _devices(self):
        return [
            CameraDescriptor(
                device_path="/dev/video0",
                stable_path="/dev/v4l/by-id/usb-046d_Brio_101-video-index0",
                symlink_name="usb-046d_Brio_101-video-index0",
                card_name="Brio 101",
                index=0,
                is_brio=True,
                by_id_path="/dev/v4l/by-id/usb-046d_Brio_101-video-index0",
                by_path_path="/dev/v4l/by-path/usb-xhci-hcd.0-2-video-index0",
            ),
            CameraDescriptor(
                device_path="/dev/video2",
                stable_path="/dev/v4l/by-id/usb-eMeet_HD_Webcam_C960-video-index0",
                symlink_name="usb-eMeet_HD_Webcam_C960-video-index0",
                card_name="HD Webcam eMeet C960: HD Webcam",
                index=0,
                is_brio=False,
                by_id_path="/dev/v4l/by-id/usb-eMeet_HD_Webcam_C960-video-index0",
                by_path_path="/dev/v4l/by-path/usb-xhci-hcd.1-2-video-index0",
            ),
        ]

    @patch("camera_sources.list_video_devices")
    def test_auto_assay_prefers_emeet_camera(self, mock_list_video_devices):
        mock_list_video_devices.return_value = self._devices()
        resolved = resolve_camera_device("auto:assay", role="assay")
        self.assertIn("eMeet_HD_Webcam_C960", str(resolved))

    @patch("camera_sources.list_video_devices")
    def test_assay_role_ignores_explicit_brio_path(self, mock_list_video_devices):
        mock_list_video_devices.return_value = self._devices()
        resolved = resolve_camera_device("/dev/video0", role="assay")
        self.assertIn("eMeet_HD_Webcam_C960", str(resolved))
        self.assertNotIn("Brio", str(resolved))

    @patch("camera_sources.list_video_devices")
    def test_assay_role_raises_when_emeet_camera_is_missing(self, mock_list_video_devices):
        mock_list_video_devices.return_value = [self._devices()[0]]
        with self.assertRaises(CameraError):
            resolve_camera_device("auto:assay", role="assay")

    @patch("camera_sources.list_video_devices")
    def test_capture_candidates_prefer_live_video_node_for_assay_camera(self, mock_list_video_devices):
        mock_list_video_devices.return_value = self._devices()
        candidates = capture_candidate_devices("auto:assay", role="assay")
        self.assertEqual(candidates[0], "/dev/video2")
        self.assertIn("/dev/v4l/by-id/usb-eMeet_HD_Webcam_C960-video-index0", [str(item) for item in candidates])
        self.assertNotIn("/dev/video0", [str(item) for item in candidates])


class SnapshotStitchTrackerTests(unittest.TestCase):
    def _calibration(self):
        background = np.zeros((200, 100, 3), dtype=np.uint8)
        vial = VialCalibration(
            physical_index=1,
            assay_index=1,
            enabled=True,
            roi_xywh=[10, 10, 80, 180],
            top_point_px=[50, 10],
            baseline_point_px=[50, 189],
            threshold_point_px=[50, 100],
            tube_height_mm=100.0,
            tube_width_mm=20.0,
            label="Vial 1",
        )
        return build_assay_calibration(background, [vial])

    def _det(self, frame_index: int, time_s: float, x_px: float, y_px: float, area_px: int = 36):
        roi_x = 10.0
        baseline_y = 189.0
        distance_from_base_px = baseline_y - float(y_px)
        return Detection(
            physical_vial_index=1,
            assay_tube_index=1,
            bbox_xywh=[int(round(x_px - 3)), int(round(y_px - 3)), 6, 6],
            center_xy_px=[float(x_px), float(y_px)],
            area_px=int(area_px),
            frame_index=int(frame_index),
            time_s=float(time_s),
            x_from_left_px=float(x_px - roi_x),
            x_from_left_mm=float((x_px - roi_x) * 20.0 / 80.0),
            y_from_base_px=float(distance_from_base_px),
            y_from_base_mm=float(distance_from_base_px * 100.0 / 180.0),
            distance_from_base_px=float(distance_from_base_px),
            distance_from_base_mm=float(distance_from_base_px * 100.0 / 180.0),
            relative_x=float((x_px - roi_x) / 80.0),
            relative_height=float(distance_from_base_px / 180.0),
            threshold_used=12.0,
        )

    def test_merge_then_split_reuses_original_track_ids(self):
        calibration = self._calibration()
        tracker = SnapshotStitchTracker(
            calibration,
            max_flies_per_vial=10,
            max_gap_frames=1,
            reacquire_frames=6,
        )

        tracker.update(
            frame_index=0,
            time_s=0.0,
            detections=[
                self._det(0, 0.0, 30.0, 170.0),
                self._det(0, 0.0, 70.0, 170.0),
            ],
            dt=0.2,
        )
        tracker.update(
            frame_index=1,
            time_s=0.2,
            detections=[self._det(1, 0.2, 50.0, 170.0, area_px=72)],
            dt=0.2,
        )
        tracker.update(
            frame_index=2,
            time_s=0.4,
            detections=[self._det(2, 0.4, 50.0, 170.0, area_px=72)],
            dt=0.2,
        )
        rows = tracker.update(
            frame_index=3,
            time_s=0.6,
            detections=[
                self._det(3, 0.6, 32.0, 169.0),
                self._det(3, 0.6, 68.0, 169.0),
            ],
            dt=0.2,
        )
        tracker.finish()

        all_tracks = tracker.all_tracks()
        self.assertEqual(tracker.next_internal_id - 1, 2)
        self.assertEqual({int(track.internal_id) for track in all_tracks}, {1, 2})
        self.assertEqual({int(track.display_id) for track in all_tracks}, {1, 2})
        self.assertEqual({int(row["internal_track_id"]) for row in rows}, {1, 2})


if __name__ == "__main__":
    unittest.main()
