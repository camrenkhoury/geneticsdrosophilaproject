import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from background_manager import BackgroundStore
from transform_utils import TransformSettings


class BackgroundStoreTests(unittest.TestCase):
    def test_background_versioning_and_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackgroundStore(tmpdir, "rig-alpha")
            image_a = np.full((20, 30, 3), 20, dtype=np.uint8)
            image_b = np.full((20, 30, 3), 220, dtype=np.uint8)

            record_a = store.save_background(image_a, TransformSettings(), source="test")
            self.assertTrue(store.current_meta_path.exists())
            self.assertIsNone(store.load_previous())
            self.assertTrue(Path(record_a.transformed_path).exists())

            record_b = store.save_background(image_b, TransformSettings(flip_horizontal=True), source="test")
            previous = store.load_previous()
            self.assertIsNotNone(previous)
            assert previous is not None
            self.assertEqual(Path(previous.raw_path).name.split("_")[-1], Path(record_a.raw_path).name.split("_")[-1])
            self.assertEqual(store.load_current().transform_signature, record_b.transform_signature)

            restored = store.restore_previous()
            self.assertEqual(restored.transform_signature, record_a.transform_signature)
            self.assertEqual(store.load_current().transform_signature, record_a.transform_signature)
            self.assertTrue(store.current_raw_path.exists())
            self.assertTrue(store.current_transformed_path.exists())


if __name__ == "__main__":
    unittest.main()
