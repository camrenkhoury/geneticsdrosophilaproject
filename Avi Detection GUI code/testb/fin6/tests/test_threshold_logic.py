import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from assay_processing import _build_crossing_candidates, _deduplicate_crossings
from assay_tracking import VialCalibration, build_assay_calibration


class ThresholdLogicTests(unittest.TestCase):
    def _calibration(self):
        background = np.zeros((120, 80, 3), dtype=np.uint8)
        vial = VialCalibration(
            physical_index=1,
            assay_index=1,
            enabled=True,
            roi_xywh=[10, 10, 40, 90],
            top_point_px=[30, 10],
            baseline_point_px=[30, 100],
            threshold_point_px=[30, 60],
            tube_height_mm=100.0,
            label="Tube 1",
        )
        return build_assay_calibration(background, [vial])

    def test_crossing_then_drop_then_recross_counts_once(self):
        calibration = self._calibration()
        # Threshold distance from baseline is 40 px. The smoothed heights cross above it twice.
        heights = [5, 12, 25, 45, 55, 32, 28, 48, 52]
        df = pd.DataFrame(
            {
                "internal_track_id": [1] * len(heights),
                "physical_vial_index": [1] * len(heights),
                "assay_tube_index": [1] * len(heights),
                "display_id": [1] * len(heights),
                "frame_index": list(range(len(heights))),
                "time_s": [idx * 0.2 for idx in range(len(heights))],
                "x_px": [30.0] * len(heights),
                "y_px": [100.0 - h for h in heights],
                "distance_from_base_px": heights,
                "height_px_smoothed": heights,
            }
        )
        candidates, details = _build_crossing_candidates(df, calibration, hysteresis_px=1.0)
        crossings_df, track_to_unique = _deduplicate_crossings(candidates)

        self.assertEqual(len(candidates), 1)
        self.assertTrue(details[1]["crossing_detected"])
        self.assertEqual(track_to_unique[1], 1)
        self.assertEqual(len(crossings_df), 1)
        self.assertFalse(bool(crossings_df.loc[0, "deduplicated"]))

    def test_fragmented_tracks_near_same_crossing_are_deduplicated(self):
        candidates = [
            type("Candidate", (), {
                "internal_track_id": 1,
                "assay_tube_index": 1,
                "physical_vial_index": 1,
                "display_id": 1,
                "start_time_s": 0.0,
                "end_time_s": 1.0,
                "crossing_frame_index": 5,
                "crossing_time_s": 1.0,
                "crossing_x_px": 31.0,
                "crossing_y_px": 55.0,
                "threshold_distance_px": 40.0,
                "threshold_distance_mm": 40.0,
            })(),
            type("Candidate", (), {
                "internal_track_id": 2,
                "assay_tube_index": 1,
                "physical_vial_index": 1,
                "display_id": 2,
                "start_time_s": 0.95,
                "end_time_s": 1.5,
                "crossing_frame_index": 6,
                "crossing_time_s": 1.08,
                "crossing_x_px": 33.0,
                "crossing_y_px": 54.0,
                "threshold_distance_px": 40.0,
                "threshold_distance_mm": 40.0,
            })(),
        ]
        crossings_df, track_to_unique = _deduplicate_crossings(candidates)

        self.assertEqual(track_to_unique[1], track_to_unique[2])
        self.assertEqual(int(crossings_df["unique_event_id"].nunique()), 1)
        self.assertEqual(int(crossings_df["deduplicated"].sum()), 1)


if __name__ == "__main__":
    unittest.main()
