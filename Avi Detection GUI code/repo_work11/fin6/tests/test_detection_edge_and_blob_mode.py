import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from assay_tracking import VialCalibration, build_assay_calibration, detect_assay_frame


class DetectionEdgeAndBlobModeTests(unittest.TestCase):
    def _single_vial_calibration(self, background: np.ndarray) -> VialCalibration:
        return VialCalibration(
            physical_index=1,
            assay_index=1,
            enabled=True,
            roi_xywh=[20, 20, 80, 160],
            top_point_px=[60, 20],
            baseline_point_px=[60, 179],
            threshold_point_px=[60, 100],
            tube_height_mm=100.0,
            tube_width_mm=20.0,
            label="Tube 1",
        )

    def test_edge_fly_is_kept_while_shifted_wall_artifact_is_rejected(self):
        background = np.full((200, 120, 3), 240, dtype=np.uint8)
        cv2.line(background, (20, 20), (20, 179), (190, 190, 190), 2)
        cv2.line(background, (99, 20), (99, 179), (190, 190, 190), 2)

        frame = background.copy()
        # Slightly darker shifted wall that used to produce a ghost blob.
        cv2.line(frame, (21, 20), (21, 179), (150, 150, 150), 2)
        # Real fly hugging the left wall.
        cv2.circle(frame, (24, 110), 3, (0, 0, 0), -1)

        calibration = build_assay_calibration(background, [self._single_vial_calibration(background)])
        detections, _mask, _aligned = detect_assay_frame(
            background,
            frame,
            calibration,
            frame_index=0,
            time_s=0.0,
            min_area=5,
            max_area=80,
            min_threshold=8.0,
            inner_margin_px=8,
            no_align=True,
            allow_blob_split=False,
        )

        self.assertEqual(len(detections), 1)
        self.assertLess(abs(float(detections[0].center_xy_px[0]) - 24.0), 4.0)
        self.assertLess(abs(float(detections[0].center_xy_px[1]) - 110.0), 5.0)

    def test_mask_blob_mode_can_keep_large_connected_blob_as_one_detection(self):
        background = np.full((200, 120, 3), 255, dtype=np.uint8)
        frame = background.copy()
        # Two overlapping flies connected into one dumbbell-shaped component.
        cv2.circle(frame, (50, 100), 8, (0, 0, 0), -1)
        cv2.circle(frame, (65, 100), 8, (0, 0, 0), -1)
        cv2.rectangle(frame, (50, 94), (65, 106), (0, 0, 0), -1)

        calibration = build_assay_calibration(background, [self._single_vial_calibration(background)])
        split_detections, _mask1, _aligned1 = detect_assay_frame(
            background,
            frame,
            calibration,
            frame_index=0,
            time_s=0.0,
            min_area=5,
            max_area=90,
            min_threshold=8.0,
            inner_margin_px=0,
            no_align=True,
            allow_blob_split=True,
            blob_split_max_parts=4,
        )
        blob_detections, _mask2, _aligned2 = detect_assay_frame(
            background,
            frame,
            calibration,
            frame_index=0,
            time_s=0.0,
            min_area=5,
            max_area=90,
            min_threshold=8.0,
            inner_margin_px=0,
            no_align=True,
            allow_blob_split=False,
            blob_split_max_parts=4,
        )

        self.assertGreaterEqual(len(split_detections), 2)
        self.assertEqual(len(blob_detections), 1)
        self.assertGreater(int(blob_detections[0].area_px), int(split_detections[0].area_px))


if __name__ == "__main__":
    unittest.main()
