import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from assay_processing import ProcessingError, process_assay_run, process_last_assay
from assay_profile import AssayProfile
from assay_recording import RecordingError, validate_recording_requirements
from assay_tracking import VialCalibration, build_assay_calibration, save_assay_calibration
from shared_utils import open_video_writer_with_path, save_json


class ValidationAndProcessingTests(unittest.TestCase):
    def test_missing_calibration_and_background_raise_clear_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile = AssayProfile(name="validation")
            profile.calibration_path = str(root / "missing_calibration.json")
            profile.outputs.background_root = str(root / "backgrounds")

            with self.assertRaises(RecordingError) as ctx:
                validate_recording_requirements(profile, root)
            self.assertIn("Calibration file does not exist", str(ctx.exception))

            profile.calibration_path = str(root / "calibration.json")
            save_json(profile.calibration_path, {"placeholder": True})
            with self.assertRaises(RecordingError) as ctx:
                validate_recording_requirements(profile, root)
            self.assertIn("No active background is available", str(ctx.exception))

    def test_process_last_assay_reports_missing_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile = AssayProfile(name="empty")
            profile.outputs.output_root = str(Path(tmpdir) / "outputs")
            with self.assertRaises(ProcessingError) as ctx:
                process_last_assay(profile, tmpdir)
            self.assertIn("No assay runs were found", str(ctx.exception))

    def test_no_crossing_run_still_processes_without_tube_index_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "assay_20260409_120001"
            run_dir.mkdir(parents=True, exist_ok=True)

            profile = AssayProfile(name="synthetic_no_cross")
            profile.assay_camera.fps = 10.0
            profile.assay_duration_s = 2.0
            profile.analysis.analysis_fps = 5.0
            profile.analysis.alignment_enabled = False
            profile.detector.min_area = 8
            profile.detector.max_area = 500
            profile.detector.min_threshold = 8.0
            profile.detector.inner_margin_px = 0
            profile.detector.max_flies_per_vial = 4
            profile.analysis.smoothing_window = 1
            profile.calibration_path = str((run_dir / "calibration_snapshot.json").resolve())

            background = np.full((200, 100, 3), 255, dtype=np.uint8)
            cv2.imwrite(str(run_dir / "background_transformed_snapshot.png"), background)
            cv2.imwrite(str(run_dir / "background_raw_snapshot.png"), background)
            save_json(run_dir / "background_meta_snapshot.json", {"transform_signature": profile.transform.signature()})
            save_json(run_dir / "profile_snapshot.json", profile.to_dict())
            save_json(run_dir / "transform_snapshot.json", profile.transform.to_dict())

            vial = VialCalibration(
                physical_index=1,
                assay_index=1,
                enabled=True,
                roi_xywh=[20, 20, 60, 160],
                top_point_px=[50, 20],
                baseline_point_px=[50, 179],
                threshold_point_px=[50, 80],
                tube_height_mm=100.0,
                label="Tube 1",
            )
            calibration = build_assay_calibration(background, [vial], background_path=str((run_dir / "background_transformed_snapshot.png").resolve()))
            save_assay_calibration(run_dir / "calibration_snapshot.json", calibration)

            writer, raw_video_path = open_video_writer_with_path(run_dir / "raw_video.mp4", fps=10.0, frame_size=(100, 200))
            for idx in range(20):
                frame = background.copy()
                y = int(round(170 - min(idx, 2) * 3.0))
                cv2.circle(frame, (50, y), 4, (0, 0, 0), -1)
                writer.write(frame)
            writer.release()

            save_json(
                run_dir / "run_manifest.json",
                {
                    "run_name": run_dir.name,
                    "run_dir": str(run_dir.resolve()),
                    "record_fps": 10.0,
                    "duration_s": 2.0,
                    "frames_recorded": 20,
                    "raw_video_path": str(raw_video_path.resolve()),
                    "profile_snapshot_path": str((run_dir / "profile_snapshot.json").resolve()),
                    "transform_snapshot_path": str((run_dir / "transform_snapshot.json").resolve()),
                    "calibration_snapshot_path": str((run_dir / "calibration_snapshot.json").resolve()),
                    "background_transformed_snapshot_path": str((run_dir / "background_transformed_snapshot.png").resolve()),
                    "background_raw_snapshot_path": str((run_dir / "background_raw_snapshot.png").resolve()),
                },
            )

            result = process_assay_run(run_dir, logger=lambda _msg: None)
            crossings = pd.read_csv(result["threshold_crossings_csv"])
            per_vial = pd.read_csv(result["per_vial_summary_csv"])

            self.assertTrue(crossings.empty)
            self.assertIn("assay_tube_index", crossings.columns.tolist())
            self.assertTrue(Path(result["report_pdf"]).exists())
            self.assertTrue(per_vial.empty or int(per_vial.loc[0, "unique_threshold_crossings"]) == 0)

    def test_synthetic_run_can_be_processed_offline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "assay_20260409_120000"
            run_dir.mkdir(parents=True, exist_ok=True)

            profile = AssayProfile(name="synthetic")
            profile.assay_camera.fps = 10.0
            profile.assay_duration_s = 2.0
            profile.analysis.analysis_fps = 5.0
            profile.analysis.alignment_enabled = False
            profile.detector.min_area = 8
            profile.detector.max_area = 500
            profile.detector.min_threshold = 10.0
            profile.detector.inner_margin_px = 0
            profile.detector.max_flies_per_vial = 4
            profile.analysis.smoothing_window = 1
            profile.calibration_path = str((run_dir / "calibration_snapshot.json").resolve())

            background = np.full((200, 100, 3), 255, dtype=np.uint8)
            cv2.imwrite(str(run_dir / "background_transformed_snapshot.png"), background)
            cv2.imwrite(str(run_dir / "background_raw_snapshot.png"), background)
            save_json(run_dir / "background_meta_snapshot.json", {"transform_signature": profile.transform.signature()})
            save_json(run_dir / "profile_snapshot.json", profile.to_dict())
            save_json(run_dir / "transform_snapshot.json", profile.transform.to_dict())

            vial = VialCalibration(
                physical_index=1,
                assay_index=1,
                enabled=True,
                roi_xywh=[20, 20, 60, 160],
                top_point_px=[50, 20],
                baseline_point_px=[50, 179],
                threshold_point_px=[50, 100],
                tube_height_mm=100.0,
                label="Tube 1",
            )
            calibration = build_assay_calibration(background, [vial], background_path=str((run_dir / "background_transformed_snapshot.png").resolve()))
            save_assay_calibration(run_dir / "calibration_snapshot.json", calibration)

            writer, raw_video_path = open_video_writer_with_path(run_dir / "raw_video.mp4", fps=10.0, frame_size=(100, 200))
            for idx in range(20):
                frame = background.copy()
                y = int(round(170 - idx * 6.0))
                cv2.circle(frame, (50, y), 4, (0, 0, 0), -1)
                writer.write(frame)
            writer.release()

            save_json(
                run_dir / "run_manifest.json",
                {
                    "run_name": run_dir.name,
                    "run_dir": str(run_dir.resolve()),
                    "record_fps": 10.0,
                    "duration_s": 2.0,
                    "frames_recorded": 20,
                    "raw_video_path": str(raw_video_path.resolve()),
                    "profile_snapshot_path": str((run_dir / "profile_snapshot.json").resolve()),
                    "transform_snapshot_path": str((run_dir / "transform_snapshot.json").resolve()),
                    "calibration_snapshot_path": str((run_dir / "calibration_snapshot.json").resolve()),
                    "background_transformed_snapshot_path": str((run_dir / "background_transformed_snapshot.png").resolve()),
                    "background_raw_snapshot_path": str((run_dir / "background_raw_snapshot.png").resolve()),
                },
            )

            result = process_assay_run(run_dir, logger=lambda _msg: None)
            crossings = pd.read_csv(result["threshold_crossings_csv"])
            per_vial = pd.read_csv(result["per_vial_summary_csv"])
            per_fly = pd.read_csv(result["per_fly_summary_csv"])

            unique_crossings = crossings.loc[~crossings["deduplicated"].astype(bool)] if not crossings.empty else crossings
            self.assertGreaterEqual(len(unique_crossings), 1)
            self.assertGreaterEqual(int(per_vial.loc[0, "unique_threshold_crossings"]), 1)
            self.assertTrue(bool(per_fly.loc[0, "crossing_detected"]))
            self.assertTrue(Path(result["report_pdf"]).exists())
            self.assertTrue(Path(result["sqlite_db"]).exists())


if __name__ == "__main__":
    unittest.main()
