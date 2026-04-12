from __future__ import annotations

import json
from pathlib import Path

from pi_backend.core.runtime_state import DetectionSummary
from shared.config.machine_paths import DETECTION_RESULT_JSON, ensure_code_directory_on_path

ensure_code_directory_on_path()

import config as legacy_config  # type: ignore  # noqa: E402


class DetectionResultReader:
    def __init__(self, result_path: Path = DETECTION_RESULT_JSON):
        self.result_path = result_path

    def read_summary(self) -> DetectionSummary:
        summary = DetectionSummary(
            source_path=str(self.result_path),
            source_exists=self.result_path.exists(),
        )

        if not self.result_path.exists():
            summary.status = "missing"
            return summary

        summary.source_mtime = self.result_path.stat().st_mtime

        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary.status = "invalid_json"
            return summary

        summary.fly_remaining = bool(payload.get("fly_remaining", False))
        raw_positions = payload.get("x_positions_mm")

        if raw_positions is None:
            summary.status = "missing_positions"
            return summary

        if not isinstance(raw_positions, list):
            summary.status = "invalid_positions"
            return summary

        try:
            positions = [float(value) for value in raw_positions]
        except (TypeError, ValueError):
            summary.status = "non_numeric_positions"
            return summary

        summary.x_positions_mm = positions
        summary.corrected_positions_mm = [self._apply_pickup_correction(value) for value in positions]

        if not summary.fly_remaining:
            summary.status = "done"
        elif not summary.corrected_positions_mm:
            summary.status = "empty_positions"
        else:
            summary.status = "ready"

        return summary

    def read_positions_for_pickup(self) -> list[float] | str | None:
        summary = self.read_summary()

        if summary.status == "done":
            return "done"

        if summary.status != "ready":
            return None

        return sorted(summary.corrected_positions_mm, reverse=True)

    def _apply_pickup_correction(self, position_mm: float) -> float:
        corrected_position = position_mm + legacy_config.PICKUP_POSITION_CORRECTION_MM
        return max(0.0, min(corrected_position, self._operational_max_mm()))

    def _operational_max_mm(self) -> float:
        return float(legacy_config.GANTRY_MAX_MM - (2.0 * legacy_config.VACUUM_CENTER_OFFSET_MM))
