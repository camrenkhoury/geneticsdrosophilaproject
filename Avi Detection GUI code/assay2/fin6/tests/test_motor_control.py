import os
import sys
import tempfile
import unittest
from pathlib import Path

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from motor_control import MotorSettings, VibrationMotor


class MotorControlTests(unittest.TestCase):
    def test_external_vibration_module_backend_can_be_used(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            marker = root / "motor_marker.txt"
            module_path = root / "vibration.py"
            module_path.write_text(
                "from pathlib import Path\n"
                f"MARKER = Path(r'{marker}')\n"
                "def vibration_on():\n"
                "    MARKER.write_text('on')\n"
                "def vibration_off():\n"
                "    MARKER.write_text(MARKER.read_text() + ',off')\n"
            )
            old_path = os.environ.get("FIN6_VIBRATION_PATH")
            old_module = os.environ.get("FIN6_VIBRATION_MODULE")
            try:
                os.environ["FIN6_VIBRATION_PATH"] = str(root)
                os.environ["FIN6_VIBRATION_MODULE"] = "vibration"
                settings = MotorSettings(enabled=True, pulse_ms=1, settle_delay_ms=0, backend="module")
                with VibrationMotor(settings) as motor:
                    motor.pulse()
                self.assertEqual(marker.read_text(), "on,off")
            finally:
                if old_path is None:
                    os.environ.pop("FIN6_VIBRATION_PATH", None)
                else:
                    os.environ["FIN6_VIBRATION_PATH"] = old_path
                if old_module is None:
                    os.environ.pop("FIN6_VIBRATION_MODULE", None)
                else:
                    os.environ["FIN6_VIBRATION_MODULE"] = old_module
                sys.modules.pop("vibration", None)

    def test_external_module_is_reused_between_pulses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            marker = root / "motor_marker.txt"
            module_path = root / "vibration.py"
            module_path.write_text(
                "from pathlib import Path\n"
                f"MARKER = Path(r'{marker}')\n"
                "COUNT_PATH = MARKER.parent / 'import_count.txt'\n"
                "count = int(COUNT_PATH.read_text()) + 1 if COUNT_PATH.exists() else 1\n"
                "COUNT_PATH.write_text(str(count))\n"
                "def vibration_on():\n"
                "    current = MARKER.read_text() if MARKER.exists() else ''\n"
                "    MARKER.write_text(current + 'on;')\n"
                "def vibration_off():\n"
                "    current = MARKER.read_text() if MARKER.exists() else ''\n"
                "    MARKER.write_text(current + 'off;')\n"
            )
            old_path = os.environ.get("FIN6_VIBRATION_PATH")
            old_module = os.environ.get("FIN6_VIBRATION_MODULE")
            try:
                os.environ["FIN6_VIBRATION_PATH"] = str(root)
                os.environ["FIN6_VIBRATION_MODULE"] = "vibration"
                settings = MotorSettings(enabled=True, pulse_ms=1, settle_delay_ms=0, backend="module")
                VibrationMotor(settings).test()
                VibrationMotor(settings).test()
                self.assertEqual((root / "import_count.txt").read_text().strip(), "1")
                self.assertIn("on;off;on;off;", marker.read_text())
            finally:
                if old_path is None:
                    os.environ.pop("FIN6_VIBRATION_PATH", None)
                else:
                    os.environ["FIN6_VIBRATION_PATH"] = old_path
                if old_module is None:
                    os.environ.pop("FIN6_VIBRATION_MODULE", None)
                else:
                    os.environ["FIN6_VIBRATION_MODULE"] = old_module
                sys.modules.pop("vibration", None)


if __name__ == "__main__":
    unittest.main()
