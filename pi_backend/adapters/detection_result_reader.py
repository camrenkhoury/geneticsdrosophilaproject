from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from pi_backend.core.runtime_state import DetectionSummary
from shared.config.machine_paths import DETECTION_RESULT_JSON, ensure_code_directory_on_path

ensure_code_directory_on_path()

import config as legacy_config  # type: ignore  # noqa: E402


class DetectionResultReader:
    def __init__(self, result_path: Path = DETECTION_RESULT_JSON):
        self.result_path = result_path
        self._cached_signature: tuple[bool, int | None, int | None] | None = None
        self._cached_summary: DetectionSummary | None = None

    def read_summary(self) -> DetectionSummary:
        source_exists = self.result_path.exists()
        source_mtime_ns: int | None = None
        source_size: int | None = None

        if source_exists:
            stat_result = self.result_path.stat()
            source_mtime_ns = stat_result.st_mtime_ns
            source_size = stat_result.st_size

        signature = (source_exists, source_mtime_ns, source_size)
        if self._cached_signature == signature and self._cached_summary is not None:
            return deepcopy(self._cached_summary)

        summary = DetectionSummary(
            source_path=str(self.result_path),
            source_exists=source_exists,
        )

        if not source_exists:
            summary.status = "missing"
            self._cached_signature = signature
            self._cached_summary = deepcopy(summary)
            return summary

        summary.source_mtime = source_mtime_ns / 1_000_000_000 if source_mtime_ns is not None else None

        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary.status = "invalid_json"
            self._cached_signature = signature
            self._cached_summary = deepcopy(summary)
            return summary

        summary.fly_remaining = bool(payload.get("fly_remaining", False))
        raw_positions = payload.get("x_positions_mm")

        if raw_positions is None:
            summary.status = "missing_positions"
            self._cached_signature = signature
            self._cached_summary = deepcopy(summary)
            return summary

        if not isinstance(raw_positions, list):
            summary.status = "invalid_positions"
            self._cached_signature = signature
            self._cached_summary = deepcopy(summary)
            return summary

        try:
            positions = [float(value) for value in raw_positions]
        except (TypeError, ValueError):
            summary.status = "non_numeric_positions"
            self._cached_signature = signature
            self._cached_summary = deepcopy(summary)
            return summary

        summary.x_positions_mm = positions
        summary.corrected_positions_mm = [self._apply_pickup_correction(value) for value in positions]

        if not summary.fly_remaining:
            summary.status = "done"
        elif not summary.corrected_positions_mm:
            summary.status = "empty_positions"
        else:
            summary.status = "ready"

        self._cached_signature = signature
        self._cached_summary = deepcopy(summary)
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
