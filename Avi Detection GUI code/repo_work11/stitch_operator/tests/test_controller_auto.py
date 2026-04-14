from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stitch_operator.controller import WorkflowController
from stitch_operator.settings import OperatorSettings, VialDefinition


class InMemorySettingsStore:
    def __init__(self, settings: OperatorSettings):
        self._settings = settings

    def load(self) -> OperatorSettings:
        return self._settings

    def save(self, settings: OperatorSettings):
        self._settings = settings


class FakeHardware:
    def __init__(self):
        self.available = False
        self._homed = False
        self.position_mm = 0.0
        self.drop_history = []

    @property
    def homed(self):
        return self._homed

    def home(self):
        self._homed = True
        self.position_mm = 0.0
        return self.position_mm

    def reset_outputs(self):
        return None

    def park_for_channel_capture(self, *, vacuum_release_settle_s=0.0, channel_camera_position_mm=None):
        if channel_camera_position_mm is not None:
            self.position_mm = float(channel_camera_position_mm)
        return self.position_mm

    def pickup_position_from_channel(self, positions_mm, pickup_offset_mm=None):
        values = [float(v) for v in positions_mm]
        return max(values) + float(pickup_offset_mm or 0.0)

    def move_to_pickup(self, pickup_position_mm, **kwargs):
        self.position_mm = float(pickup_position_mm)
        return self.position_mm

    def drop_in_chamber_and_clear(self, **kwargs):
        return self.position_mm

    def reacquire_from_chamber(self, **kwargs):
        return self.position_mm

    def drop_into_vial(self, destination_mm, **kwargs):
        self.position_mm = float(destination_mm)
        self.drop_history.append(float(destination_mm))
        return self.position_mm


class FakeChannel:
    queued_captures = []

    def __init__(self, settings):
        self.settings = settings
        self._captures = [deepcopy(item) for item in type(self).queued_captures]
        self._last_result = None
        self._tempdir = Path(tempfile.mkdtemp(prefix="operator-channel-"))
        self.result_json_path = self._tempdir / "last_channel_result.json"
        self.saved_master = None

    def status(self):
        return {
            "background_ready": True,
            "calibration_ready": True,
            "result_ready": self._last_result is not None,
            "camera": "fake-channel",
        }

    def capture_channel(self, *args, **kwargs):
        if not self._captures:
            raise RuntimeError("No fake channel captures remain.")
        self._last_result = deepcopy(self._captures.pop(0))
        return deepcopy(self._last_result)

    def load_last_result(self):
        return deepcopy(self._last_result) if self._last_result is not None else None

    def save_result(self, payload):
        self._last_result = deepcopy(payload)
        return self.result_json_path

    def save_auto_flow_master(self, payload):
        self.saved_master = deepcopy(payload)
        return self._tempdir / "auto_flow_master.json"


class FakeSexing:
    queued_results = []

    def __init__(self, settings):
        self.settings = settings
        self._results = [deepcopy(item) for item in type(self).queued_results]

    def status(self):
        return {"ready": True, "path": "fake-model.pt", "error": ""}

    def classify(self):
        if not self._results:
            raise RuntimeError("No fake sexing results remain.")
        return deepcopy(self._results.pop(0))


class FakeAssay:
    def __init__(self, settings):
        self.settings = settings
        self.profile = SimpleNamespace(name="test-profile", last_run_dir="")

    def status(self):
        return {
            "background_ready": False,
            "calibration_ready": False,
            "profile": self.profile.name,
            "camera": "fake-assay",
        }

    def run_assay(self, **kwargs):
        self.profile.last_run_dir = "/tmp/fake-assay-run"
        return {"run_dir": self.profile.last_run_dir, "duration_s": 1.0}

    def process_last(self, **kwargs):
        return {
            "run_dir": self.profile.last_run_dir or "/tmp/fake-assay-run",
            "processed_at": "now",
            "per_vial_summary_rows": [],
        }


class WorkflowControllerAutoTests(unittest.TestCase):
    def make_settings(self) -> OperatorSettings:
        return OperatorSettings(
            sexing_capture_command="/usr/bin/true",
            vacuum_pick_delay_s=0.0,
            vacuum_drop_delay_s=0.0,
            vacuum_release_settle_s=0.0,
            classification_delay_s=0.0,
            auto_max_pick_attempts_per_location=2,
            channel_position_match_tolerance_mm=4.0,
            vial_definitions=[
                VialDefinition("V1", "Junk", "junk", 10.0, 999),
                VialDefinition("V2", "M1", "male", 20.0, 10),
                VialDefinition("V3", "F1", "female", 30.0, 10),
                VialDefinition("V4", "M2", "male", 40.0, 10),
                VialDefinition("V5", "F2", "female", 50.0, 10),
            ],
        )

    def build_controller(self, captures, sexing_results) -> WorkflowController:
        FakeChannel.queued_captures = captures
        FakeSexing.queued_results = sexing_results
        settings = self.make_settings()
        store = InMemorySettingsStore(settings)
        with patch("stitch_operator.controller.HardwareService", FakeHardware), patch(
            "stitch_operator.controller.ChannelService", FakeChannel
        ), patch("stitch_operator.controller.SexingService", FakeSexing), patch(
            "stitch_operator.controller.AssayService", FakeAssay
        ):
            controller = WorkflowController(store)
        return controller

    def test_choose_destination_prefers_first_vial_until_full(self):
        controller = self.build_controller([], [])
        controller.increment_vial("V2")
        for _ in range(8):
            controller.increment_vial("V2")
        self.assertEqual(controller.choose_destination_for_sex("male").vial_id, "V2")
        controller.increment_vial("V2")
        self.assertEqual(controller.choose_destination_for_sex("male").vial_id, "V4")
        self.assertEqual(controller.choose_junk_destination().vial_id, "V1")

    def test_auto_flow_skips_stuck_location_after_two_pickup_misses(self):
        captures = [
            {
                "captured_at": "t0",
                "count": 2,
                "fly_remaining": True,
                "x_positions_mm": [90.0, 40.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t1",
                "count": 2,
                "fly_remaining": True,
                "x_positions_mm": [90.0, 40.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t2",
                "count": 2,
                "fly_remaining": True,
                "x_positions_mm": [90.0, 40.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t3",
                "count": 1,
                "fly_remaining": True,
                "x_positions_mm": [90.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
        ]
        sexing_results = [
            {"captured_at": "s1", "label": "UNCERTAIN", "confidence": 0.0, "image_path": "", "detail": "NO_PROBS", "uncertain": True},
            {"captured_at": "s2", "label": "UNCERTAIN", "confidence": 0.0, "image_path": "", "detail": "NO_PROBS", "uncertain": True},
            {"captured_at": "s3", "label": "male", "confidence": 0.96, "image_path": "", "detail": "ok", "uncertain": False},
            {"captured_at": "s4", "label": "male", "confidence": 0.95, "image_path": "", "detail": "ok", "uncertain": False},
        ]
        controller = self.build_controller(captures, sexing_results)
        controller.start_auto_flow()
        controller._task_thread.join(timeout=3)
        self.assertFalse(controller.snapshot().busy)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.stage_label, "Auto flow paused")
        self.assertEqual(snapshot.vials[1].current_count, 1)  # V2 / M1
        self.assertIn("Remaining channel positions", snapshot.status_message)
        self.assertIsNotNone(controller.channel.saved_master)
        self.assertEqual(len(controller.channel.saved_master.get("attempt_history", [])), 3)
        self.assertTrue(controller.channel.saved_master.get("skipped_positions_mm"))

    def test_auto_flow_routes_uncertain_fly_to_junk_when_source_disappears(self):
        captures = [
            {
                "captured_at": "t0",
                "count": 1,
                "fly_remaining": True,
                "x_positions_mm": [55.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t1",
                "count": 0,
                "fly_remaining": False,
                "x_positions_mm": [],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
        ]
        sexing_results = [
            {"captured_at": "s1", "label": "UNCERTAIN", "confidence": 0.12, "image_path": "", "detail": "LOW_CONF", "uncertain": True},
        ]
        controller = self.build_controller(captures, sexing_results)
        controller.start_auto_flow()
        controller._task_thread.join(timeout=3)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.stage_label, "Loading complete")
        self.assertEqual(snapshot.vials[0].current_count, 1)  # Junk vial
        self.assertIn("Prepare or run the assay", snapshot.status_message)


    def test_uncertain_pickup_miss_does_not_route_to_junk_vial(self):
        captures = [
            {
                "captured_at": "t0",
                "count": 1,
                "fly_remaining": True,
                "x_positions_mm": [70.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t1",
                "count": 1,
                "fly_remaining": True,
                "x_positions_mm": [70.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t2",
                "count": 0,
                "fly_remaining": False,
                "x_positions_mm": [],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
        ]
        sexing_results = [
            {"captured_at": "s1", "label": "UNCERTAIN", "confidence": 0.02, "image_path": "", "detail": "NO_PROBS", "uncertain": True},
            {"captured_at": "s2", "label": "male", "confidence": 0.97, "image_path": "", "detail": "ok", "uncertain": False},
            {"captured_at": "s3", "label": "male", "confidence": 0.96, "image_path": "", "detail": "ok", "uncertain": False},
        ]
        controller = self.build_controller(captures, sexing_results)
        controller.start_auto_flow()
        controller._task_thread.join(timeout=3)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.vials[0].current_count, 0)
        self.assertEqual(snapshot.vials[1].current_count, 1)
        self.assertEqual(len(controller.hardware.drop_history), 1)

    def test_auto_flow_rejects_mismatched_second_sex_to_junk(self):
        captures = [
            {
                "captured_at": "t0",
                "count": 1,
                "fly_remaining": True,
                "x_positions_mm": [62.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
            {
                "captured_at": "t1",
                "count": 0,
                "fly_remaining": False,
                "x_positions_mm": [],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
        ]
        sexing_results = [
            {"captured_at": "s1", "label": "male", "confidence": 0.98, "image_path": "", "detail": "ok", "uncertain": False},
            {"captured_at": "s2", "label": "female", "confidence": 0.97, "image_path": "", "detail": "ok", "uncertain": False},
        ]
        controller = self.build_controller(captures, sexing_results)
        controller.start_auto_flow()
        controller._task_thread.join(timeout=3)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.vials[0].current_count, 1)
        self.assertEqual(snapshot.vials[1].current_count, 0)
        self.assertIn("sexing 1", snapshot.status_message.lower())
        self.assertIsNotNone(controller.channel.saved_master)
        stats = controller.channel.saved_master.get("stats", {})
        self.assertEqual(int(stats.get("sexing_rejects", 0) or 0), 1)


    def test_manual_route_timeout_falls_back_to_junk(self):
        captures = [
            {
                "captured_at": "t0",
                "count": 1,
                "fly_remaining": True,
                "x_positions_mm": [80.0],
                "raw_image_path": "",
                "annotated_image_path": "",
                "mask_image_path": "",
            },
        ]
        sexing_results = [
            {"captured_at": "s1", "label": "UNCERTAIN", "confidence": 0.10, "image_path": "", "detail": "LOW_CONF", "uncertain": True},
        ]
        controller = self.build_controller(captures, sexing_results)
        channel_result = controller.channel.capture_channel()
        with patch.object(controller, "request_choice", return_value=None):
            controller._route_next_fly_impl(channel_result=channel_result, require_fresh_capture=False, finalize_stage=False)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.vials[0].current_count, 1)
        self.assertEqual(len(controller.hardware.drop_history), 1)


class WorkflowControllerResetTests(unittest.TestCase):
    def test_reset_vial_counts_marks_ready_for_new_run(self):
        settings = OperatorSettings(
            sexing_capture_command="/usr/bin/true",
            vial_definitions=[
                VialDefinition("V1", "Junk", "junk", 10.0, 999),
                VialDefinition("V2", "M1", "male", 20.0, 10),
            ],
        )
        store = InMemorySettingsStore(settings)
        with patch("stitch_operator.controller.HardwareService", FakeHardware), patch(
            "stitch_operator.controller.ChannelService", FakeChannel
        ), patch("stitch_operator.controller.SexingService", FakeSexing), patch(
            "stitch_operator.controller.AssayService", FakeAssay
        ):
            controller = WorkflowController(store)
        controller.increment_vial("V2")
        controller.hardware.home()
        controller.reset_vial_counts()
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.vials[1].current_count, 0)
        self.assertEqual(snapshot.stage_label, "Ready for new run")
        self.assertEqual(snapshot.status_message, "Vial counts reset. Ready for a new run.")


if __name__ == "__main__":
    unittest.main()
