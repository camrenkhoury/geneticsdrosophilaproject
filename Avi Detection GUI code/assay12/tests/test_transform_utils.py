import sys
import unittest
from pathlib import Path

import numpy as np

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from transform_utils import TransformSettings, apply_image_transform, transformed_shape


class TransformUtilsTests(unittest.TestCase):
    def test_flip_and_crop_are_applied_deterministically(self):
        image = np.zeros((6, 8, 3), dtype=np.uint8)
        image[1, 1] = (255, 0, 0)
        settings = TransformSettings(flip_horizontal=True, crop_xywh=[4, 0, 4, 6])

        transformed_once = apply_image_transform(image, settings)
        transformed_twice = apply_image_transform(image, settings)

        self.assertEqual(transformed_once.shape[:2], (6, 4))
        self.assertTrue(np.array_equal(transformed_once, transformed_twice))
        # Original bright pixel at x=1 moves to x=6 after horizontal flip, then to x=2 within the crop.
        self.assertEqual(int(transformed_once[1, 2, 0]), 255)

    def test_rotation_changes_shape_consistently(self):
        settings = TransformSettings(rotation_deg=90.0)
        self.assertEqual(transformed_shape((10, 20), settings), (20, 10))


if __name__ == "__main__":
    unittest.main()
