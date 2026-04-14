import sys
import tempfile
import unittest
from pathlib import Path

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from box_upload import collect_artifacts, resolve_effective_box_settings, should_auto_upload, write_box_templates
from shared_utils import save_json


class BoxUploadTests(unittest.TestCase):
    def test_write_box_templates_and_resolve_effective_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = write_box_templates(tmpdir)
            config_path = Path(result["config_file"])
            tokens_path = Path(result["tokens_file"])
            env_path = Path(result["env_file"])
            self.assertTrue(config_path.exists())
            self.assertTrue(tokens_path.exists())
            self.assertTrue(env_path.exists())

            effective = resolve_effective_box_settings({"config_file": str(config_path)})
            self.assertTrue(effective.enabled)
            self.assertTrue(effective.upload_after_processing)
            self.assertFalse(effective.upload_after_recording)
            self.assertEqual(effective.artifact_mode, "summaries+videos")
            self.assertEqual(Path(effective.tokens_file), tokens_path)
            self.assertTrue(should_auto_upload({"config_file": str(config_path)}, "processing"))
            self.assertFalse(should_auto_upload({"config_file": str(config_path)}, "recording"))

    def test_collect_artifacts_filters_backgrounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "assay_20260411_120000"
            (run_dir / "processed").mkdir(parents=True)
            (run_dir / "graphs").mkdir(parents=True)
            for file_path in [
                run_dir / "run_manifest.json",
                run_dir / "background_raw_snapshot.png",
                run_dir / "background_meta_snapshot.json",
                run_dir / "processed" / "frame_level.csv",
                run_dir / "processed" / "annotated_video.mp4",
                run_dir / "graphs" / "velocity_plot.png",
            ]:
                if file_path.suffix == ".json":
                    save_json(file_path, {"ok": True})
                else:
                    file_path.write_bytes(b"test")

            summaries = collect_artifacts(run_dir, mode="summaries", include_backgrounds=False)
            summary_names = {path.name for path in summaries}
            self.assertIn("run_manifest.json", summary_names)
            self.assertIn("frame_level.csv", summary_names)
            self.assertIn("velocity_plot.png", summary_names)
            self.assertNotIn("background_raw_snapshot.png", summary_names)
            self.assertNotIn("background_meta_snapshot.json", summary_names)
            self.assertNotIn("annotated_video.mp4", summary_names)

            with_videos = collect_artifacts(run_dir, mode="summaries+videos", include_backgrounds=False)
            video_names = {path.name for path in with_videos}
            self.assertIn("annotated_video.mp4", video_names)
            self.assertNotIn("background_raw_snapshot.png", video_names)

            with_backgrounds = collect_artifacts(run_dir, mode="summaries+videos", include_backgrounds=True)
            bg_names = {path.name for path in with_backgrounds}
            self.assertIn("background_raw_snapshot.png", bg_names)
            self.assertIn("background_meta_snapshot.json", bg_names)


if __name__ == "__main__":
    unittest.main()
