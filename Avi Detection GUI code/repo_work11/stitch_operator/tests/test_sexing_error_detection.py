from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from stitch_operator.services.sexing import SexingService
from stitch_operator.settings import OperatorSettings, VialDefinition


class SexingErrorDetectionTests(unittest.TestCase):
    def make_service(self) -> SexingService:
        tempdir = tempfile.mkdtemp(prefix="sexing-error-")
        settings = OperatorSettings(
            sexing_model_path="models/does_not_matter.pt",
            sexing_capture_dir=tempdir,
            sexing_capture_command="/usr/bin/true",
            vial_definitions=[
                VialDefinition("V1", "Junk", "junk", 10.0, 999),
            ],
        )
        return SexingService(settings)

    def test_count_flies_detects_single_large_blob(self):
        service = self.make_service()
        image = np.full((480, 640, 3), 230, dtype=np.uint8)
        cv2.ellipse(image, (320, 240), (80, 130), 0, 0, 360, (20, 20, 20), -1)
        info = service._count_flies(image, debug=True)
        self.assertEqual(info["count"], 1)
        self.assertEqual(info["errors"], [])

    def test_classify_returns_uncertain_when_multiple_flies_detected(self):
        service = self.make_service()
        image = np.full((480, 640, 3), 230, dtype=np.uint8)
        cv2.ellipse(image, (170, 240), (80, 130), 0, 0, 360, (20, 20, 20), -1)
        cv2.ellipse(image, (470, 240), (80, 130), 0, 0, 360, (20, 20, 20), -1)
        cv2.imwrite(str(service.latest_capture_path), image)
        with patch.object(service, "_capture_with_rpicam", lambda _output_path: None):
            result = service.classify(debug=True)
        self.assertEqual(result["label"], "UNCERTAIN")
        self.assertEqual(result["count"], 2)
        self.assertTrue(any(str(item).startswith("MULTIPLE_FLIES:") for item in result["errors"]))
        self.assertTrue(Path(result["image_path"]).exists())

    def test_classify_returns_empty_when_no_fly_detected(self):
        service = self.make_service()
        image = np.full((480, 640, 3), 230, dtype=np.uint8)
        cv2.imwrite(str(service.latest_capture_path), image)
        with patch.object(service, "_capture_with_rpicam", lambda _output_path: None):
            result = service.classify(debug=True)
        self.assertEqual(result["label"], "UNCERTAIN")
        self.assertEqual(result["count"], 0)
        self.assertIn("CHAMBER_EMPTY", result["errors"])


if __name__ == "__main__":
    unittest.main()
