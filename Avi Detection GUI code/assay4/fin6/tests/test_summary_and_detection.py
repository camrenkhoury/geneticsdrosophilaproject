import sys
import unittest
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from assay_processing import _build_vial_summaries
from assay_tracking import VialCalibration, _component_detections_from_mask


class SummaryCappingTests(unittest.TestCase):
    def test_vial_summary_caps_fragmented_track_counts_to_configured_capacity(self):
        track_frames_df = pd.DataFrame(
            [
                {
                    "assay_tube_index": 1,
                    "internal_track_id": idx + 1,
                    "time_s": idx * 0.5,
                    "distance_from_base_px": 10.0 + idx,
                    "vertical_velocity_px_s_smoothed": 2.0 + idx * 0.1,
                    "speed_px_s_smoothed": 2.0 + idx * 0.1,
                    "distance_from_base_mm": 1.0 + idx * 0.2,
                    "vertical_velocity_mm_s_smoothed": 0.5 + idx * 0.05,
                    "speed_mm_s_smoothed": 0.5 + idx * 0.05,
                }
                for idx in range(6)
            ]
        )
        track_summary_df = pd.DataFrame(
            {
                "assay_tube_index": [1, 1, 1, 1, 1, 1],
                "internal_track_id": [1, 2, 3, 4, 5, 6],
            }
        )
        crossings_df = pd.DataFrame(
            {
                "assay_tube_index": [1, 1, 1, 1, 1, 1],
                "unique_event_id": [1, 2, 3, 4, 5, 6],
                "deduplicated": [False, False, False, False, False, False],
            }
        )

        per_vial_summary_df, _timeseries_df = _build_vial_summaries(
            track_frames_df,
            track_summary_df,
            crossings_df,
            processed_duration_s=2.5,
            configured_max_flies_per_vial=4,
        )

        row = per_vial_summary_df.iloc[0]
        self.assertEqual(int(row["configured_flies_per_vial"]), 4)
        self.assertEqual(int(row["track_fragments_detected"]), 6)
        self.assertEqual(int(row["number_of_flies_detected"]), 4)
        self.assertEqual(int(row["raw_unique_threshold_crossings"]), 6)
        self.assertEqual(int(row["unique_threshold_crossings"]), 4)
        self.assertAlmostEqual(float(row["fraction_crossing_by_10s"]), 1.0)
        self.assertGreater(float(row["estimated_average_distance_travelled_mm"]), 0.0)


class DetectionSplitTests(unittest.TestCase):
    def test_medium_merged_blob_can_split_without_exceeding_max_area(self):
        mask = np.zeros((120, 120), dtype=np.uint8)

        # Two typical single-fly blobs establish the per-frame reference size.
        cv2.ellipse(mask, (24, 60), (3, 8), 0, 0, 360, 255, -1)
        cv2.ellipse(mask, (45, 62), (3, 8), 0, 0, 360, 255, -1)

        # A connected "dumbbell" blob that stays below max_area but should still
        # be treated as two adjacent flies.
        cv2.circle(mask, (80, 56), 5, 255, -1)
        cv2.circle(mask, (91, 56), 5, 255, -1)
        cv2.rectangle(mask, (80, 51), (91, 61), 255, -1)

        vial = VialCalibration(
            physical_index=1,
            assay_index=1,
            enabled=True,
            roi_xywh=[0, 0, 120, 120],
            top_point_px=[60, 10],
            baseline_point_px=[60, 110],
            threshold_point_px=[60, 60],
            tube_height_mm=100.0,
            label="Tube 1",
        )

        detections = _component_detections_from_mask(
            mask=mask,
            offset_xy=(0, 0),
            vial=vial,
            frame_index=0,
            time_s=0.0,
            min_area=10,
            max_area=180,
            threshold_used=12.0,
        )

        merged_region = [det for det in detections if float(det.center_xy_px[0]) >= 70.0]
        self.assertEqual(len(detections), 4)
        self.assertGreaterEqual(len(merged_region), 2)


if __name__ == "__main__":
    unittest.main()
