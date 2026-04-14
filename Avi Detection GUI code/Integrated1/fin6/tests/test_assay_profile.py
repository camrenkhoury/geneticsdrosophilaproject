import sys
import tempfile
import unittest
from pathlib import Path

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from assay_profile import AssayProfile, ProfileStore
from transform_utils import TransformSettings


class ProfileStoreTests(unittest.TestCase):
    def test_profile_round_trip_duplicate_and_last_used(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProfileStore(tmpdir)
            profile = store.create_profile("assay rig A")
            profile.description = "Main enclosure"
            profile.assay_camera.device = "auto:assay"
            profile.assay_camera.preferred_hint = "Logitech"
            profile.transform = TransformSettings(rotation_deg=90.0, flip_horizontal=True, crop_xywh=[10, 20, 100, 200])
            profile.assay_duration_s = 10.0
            profile.analysis.analysis_fps = 5.0
            profile.motor.enabled = True
            profile.motor.gpio_pin = 18
            path = store.save_profile(profile)

            self.assertTrue(path.exists())
            loaded = store.load_profile("assay rig A")
            self.assertEqual(loaded.name, "assay rig A")
            self.assertEqual(loaded.description, "Main enclosure")
            self.assertEqual(loaded.assay_camera.preferred_hint, "Logitech")
            self.assertEqual(loaded.transform.crop_xywh, [10, 20, 100, 200])
            self.assertTrue(loaded.motor.enabled)
            self.assertEqual(loaded.motor.gpio_pin, 18)

            duplicated_path = store.duplicate_profile("assay rig A", "assay rig B")
            duplicated = store.load_profile(duplicated_path)
            self.assertEqual(duplicated.name, "assay rig B")
            self.assertEqual(duplicated.description, "Main enclosure")
            self.assertEqual(duplicated.last_run_dir, "")
            self.assertEqual(duplicated.transform.signature(), loaded.transform.signature())

            last_used = store.load_last_used()
            self.assertIsNotNone(last_used)
            assert last_used is not None
            self.assertEqual(last_used.name, "assay rig B")
            self.assertIn("assay rig A", store.list_profile_names())
            self.assertIn("assay rig B", store.list_profile_names())


if __name__ == "__main__":
    unittest.main()
