from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
import traceback
import uuid
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from .settings import OperatorSettings, OperatorSettingsStore
from .state import AssayRunState, ChannelState, OperatorState, ReadinessState, SexingState, VialState, WorkflowStage
from .services.assay import AssayService, BackgroundError, ProcessingError, RecordingError
from .services.channel import ChannelError, ChannelService
from .services.hardware import HardwareError, HardwareService
from .services.sexing import SexingService


class OperatorFacingError(RuntimeError):
    pass


class TaskCancelled(OperatorFacingError):
    pass


class WorkflowController:
    def __init__(self, settings_store: Optional[OperatorSettingsStore] = None):
        self.settings_store = settings_store or OperatorSettingsStore()
        self.settings: OperatorSettings = self.settings_store.load()

        self.hardware = HardwareService()
        self.channel = ChannelService(self.settings)
        self.sexing = SexingService(self.settings)
        self.assay = AssayService(self.settings)

        self._lock = threading.RLock()
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._pending_requests: Dict[str, "queue.Queue[Optional[str]]"] = {}
        self._task_stop_event = threading.Event()
        self._task_thread: Optional[threading.Thread] = None

        self.state = OperatorState(vials=self._build_vial_states())
        self.refresh_readiness()
        self._set_stage(WorkflowStage.IDLE, "Idle", next_action="Initialize")
        self.log("Operator controller ready.")

    # ------------------------------------------------------------------
    # state helpers
    # ------------------------------------------------------------------
    def _build_vial_states(self) -> List[VialState]:
        return [
            VialState(
                vial_id=item.vial_id,
                label=item.label,
                target_sex=item.target_sex,
                position_mm=float(item.position_mm),
                max_count=int(item.max_count),
            )
            for item in self.settings.vial_definitions
        ]

    def snapshot(self) -> OperatorState:
        with self._lock:
            return deepcopy(self.state)

    def settings_json(self) -> str:
        return json.dumps(self.settings.to_dict(), indent=2)

    def _set_stage(
        self,
        stage: WorkflowStage,
        label: str,
        *,
        next_action: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.state.stage = stage
            self.state.stage_label = label
            if next_action is not None:
                self.state.next_action = next_action
            if message is not None:
                self.state.status_message = message
            self.state.updated_at = time.time()

    def _update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self.state, key, value)
            self.state.updated_at = time.time()

    def _set_channel_state(self, payload: Dict[str, Any], *, stale: bool = False) -> None:
        channel_state = ChannelState(
            captured_at=str(payload.get("captured_at", "") or ""),
            count=int(payload.get("count", 0) or 0),
            fly_remaining=bool(payload.get("fly_remaining", False)),
            x_positions_mm=[float(v) for v in payload.get("x_positions_mm", [])],
            raw_image_path=str(payload.get("raw_image_path", "") or ""),
            annotated_image_path=str(payload.get("annotated_image_path", "") or ""),
            mask_image_path=str(payload.get("mask_image_path", "") or ""),
            result_json_path=str(payload.get("result_json_path", self.channel.result_json_path) or self.channel.result_json_path),
            stale=bool(stale),
        )
        with self._lock:
            self.state.channel = channel_state
            self.state.updated_at = time.time()

    def _default_sexing_payload(self, detail: str = "Awaiting next specimen.") -> Dict[str, Any]:
        return {
            "captured_at": "",
            "label": "--",
            "confidence": 0.0,
            "image_path": "",
            "detail": detail,
            "uncertain": False,
        }

    def _clear_sexing_state(self, detail: str = "Awaiting next specimen.") -> None:
        self._set_sexing_state(self._default_sexing_payload(detail))

    def _set_sexing_state(self, payload: Dict[str, Any]) -> None:
        sexing_state = SexingState(
            captured_at=str(payload.get("captured_at", "") or ""),
            label=str(payload.get("label", "--") or "--"),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            image_path=str(payload.get("image_path", "") or ""),
            detail=str(payload.get("detail", "") or ""),
            uncertain=bool(payload.get("uncertain", False)),
            model_path=str(self.settings.sexing_model_path),
        )
        with self._lock:
            self.state.sexing = sexing_state
            self.state.updated_at = time.time()

    def _set_assay_state(self, payload: Dict[str, Any]) -> None:
        assay_state = replace(
            self.state.assay,
            run_dir=str(payload.get("run_dir", self.state.assay.run_dir) or self.state.assay.run_dir),
            preview_image_path=str(payload.get("preview_path", payload.get("preview_image_path", self.state.assay.preview_image_path)) or self.state.assay.preview_image_path),
            processed_dir=str(payload.get("processing_dir", self.state.assay.processed_dir) or self.state.assay.processed_dir),
            processed_at=str(payload.get("processed_at", self.state.assay.processed_at) or self.state.assay.processed_at),
            pdf_path=str(payload.get("pdf_path", payload.get("summary_pdf", payload.get("report_pdf", self.state.assay.pdf_path))) or self.state.assay.pdf_path),
            processing_json=str(payload.get("processing_json", self.state.assay.processing_json) or self.state.assay.processing_json),
            summary_csv_path=str(payload.get("per_vial_summary_csv", self.state.assay.summary_csv_path) or self.state.assay.summary_csv_path),
            upload_status=str(payload.get("upload_status", self.state.assay.upload_status) or self.state.assay.upload_status),
            unique_crossings_total=int(payload.get("unique_threshold_crossings_total", self.state.assay.unique_crossings_total) or self.state.assay.unique_crossings_total),
            duration_s=float(payload.get("duration_s", payload.get("assay_duration_s", self.state.assay.duration_s)) or self.state.assay.duration_s),
            per_vial_summary=list(payload.get("per_vial_summary_rows", self.state.assay.per_vial_summary) or self.state.assay.per_vial_summary),
        )
        with self._lock:
            self.state.assay = assay_state
            self.state.updated_at = time.time()

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} | {message}"
        with self._lock:
            self.state.recent_logs.append(line)
            self.state.recent_logs = self.state.recent_logs[-500:]
            self.state.updated_at = time.time()

    def _apply_settings(self, settings: OperatorSettings, *, preserve_counts: bool = True, log_message: Optional[str] = None) -> None:
        with self._lock:
            previous_vials = {
                vial.vial_id: {
                    "count": vial.current_count,
                    "status": vial.status,
                    "last_routed_at": vial.last_routed_at,
                }
                for vial in self.state.vials
            }
        self.settings = settings
        self.settings_store.save(self.settings)
        self.channel = ChannelService(self.settings)
        self.sexing = SexingService(self.settings)
        self.assay = AssayService(self.settings)
        new_vials = self._build_vial_states()
        if preserve_counts:
            for vial in new_vials:
                old = previous_vials.get(vial.vial_id)
                if not old:
                    continue
                vial.current_count = min(int(old.get("count", 0) or 0), vial.max_count)
                vial.last_routed_at = str(old.get("last_routed_at", "") or "")
                vial.status = "READY" if vial.current_count < vial.max_count else "FULL"
        with self._lock:
            self.state.vials = new_vials
            self.state.updated_at = time.time()
        self.refresh_readiness()
        if log_message:
            self.log(log_message)

    def refresh_readiness(self) -> None:
        channel_status = self.channel.status()
        assay_status = self.assay.status()
        sex_status = self.sexing.status()
        readiness = ReadinessState(
            homed=bool(self.hardware.homed),
            model_ready=bool(sex_status.get("ready", False)),
            channel_background_ready=bool(channel_status.get("background_ready", False)),
            channel_calibration_ready=bool(channel_status.get("calibration_ready", False)),
            assay_background_ready=bool(assay_status.get("background_ready", False)),
            assay_calibration_ready=bool(assay_status.get("calibration_ready", False)),
            active_profile=str(assay_status.get("profile", "") or ""),
            channel_camera=str(channel_status.get("camera", "unknown") or "unknown"),
            assay_camera=str(assay_status.get("camera", "unknown") or "unknown"),
        )
        with self._lock:
            self.state.readiness = readiness
            self.state.hardware_position_mm = float(self.hardware.position_mm)
            self.state.sexing.model_path = str(self.settings.sexing_model_path)
            self.state.updated_at = time.time()
        if self.state.channel.result_json_path == "":
            last_result = self.channel.load_last_result()
            if last_result is not None:
                self._set_channel_state(last_result, stale=True)
        if self.assay.profile.last_run_dir and not self.state.assay.run_dir:
            self._set_assay_state({"run_dir": self.assay.profile.last_run_dir})

    def periodic_refresh(self) -> None:
        with self._lock:
            self.state.hardware_position_mm = float(self.hardware.position_mm)
            self.state.updated_at = time.time()
        self.refresh_readiness()

    # ------------------------------------------------------------------
    # task runner
    # ------------------------------------------------------------------
    def _begin_task(self, task_name: str, stage: WorkflowStage, stage_label: str, start_message: str, target: Callable[[], None]) -> bool:
        with self._lock:
            if self.state.busy:
                self.log(f"Ignoring {task_name}: another task is already running.")
                return False
            self.state.busy = True
            self.state.active_task = task_name
            self.state.brief_error = ""
            self.state.error_detail = ""
            self.state.status_message = start_message
            self.state.updated_at = time.time()
        self._set_stage(stage, stage_label, message=start_message)
        self._task_stop_event = threading.Event()

        def runner():
            try:
                target()
            except TaskCancelled as exc:
                message = str(exc) or "Operation stopped."
                self.log(message)
                self._set_stage(
                    WorkflowStage.READY,
                    "Stopped",
                    next_action="Capture Channel" if self.hardware.homed else "Initialize",
                    message=message,
                )
                self._update(brief_error="", error_detail="")
            except OperatorFacingError as exc:
                self.log(f"Operator error: {exc}")
                self._set_stage(WorkflowStage.ERROR, "Attention needed", next_action="Review status", message=str(exc))
                self._update(brief_error=str(exc), error_detail=str(exc))
            except (ChannelError, HardwareError, BackgroundError, ProcessingError, RecordingError) as exc:
                detail = traceback.format_exc()
                self.log(f"Workflow error: {exc}")
                self._set_stage(WorkflowStage.ERROR, "Error", next_action="Review Debug", message=str(exc))
                self._update(brief_error=str(exc), error_detail=detail)
            except Exception as exc:  # pragma: no cover - safety net
                detail = traceback.format_exc()
                self.log(f"Unhandled error: {exc}")
                self._set_stage(WorkflowStage.ERROR, "Error", next_action="Review Debug", message=str(exc))
                self._update(brief_error=str(exc), error_detail=detail)
            finally:
                with self._lock:
                    self.state.busy = False
                    self.state.active_task = ""
                    self.state.hardware_position_mm = float(self.hardware.position_mm)
                    self.state.updated_at = time.time()
                self.refresh_readiness()

        self._task_thread = threading.Thread(target=runner, daemon=True)
        self._task_thread.start()
        return True

    def stop_current_task(self) -> None:
        self._task_stop_event.set()
        self.log("Stop requested. The workflow will stop at the next safe point.")

    def _check_cancelled(self, message: str = "Operation stopped.") -> None:
        if self._task_stop_event.is_set():
            raise TaskCancelled(message)

    # ------------------------------------------------------------------
    # confirmation bridge
    # ------------------------------------------------------------------
    def request_choice(
        self,
        *,
        title: str,
        message: str,
        options: List[str],
        timeout_s: Optional[float] = None,
    ) -> Optional[str]:
        request_id = uuid.uuid4().hex
        response_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1)
        self._pending_requests[request_id] = response_queue
        effective_timeout = max(1.0, float(self.settings.confirmation_timeout_s if timeout_s is None else timeout_s))
        self.events.put({
            "type": "choice",
            "request_id": request_id,
            "title": title,
            "message": message,
            "options": options,
            "timeout_s": effective_timeout,
        })
        try:
            return response_queue.get(timeout=effective_timeout)
        except queue.Empty:
            return None
        finally:
            self._pending_requests.pop(request_id, None)

    def resolve_choice(self, request_id: str, value: Optional[str]) -> None:
        queue_obj = self._pending_requests.get(request_id)
        if queue_obj is None:
            return
        try:
            queue_obj.put_nowait(value)
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # destination helpers
    # ------------------------------------------------------------------
    def _available_vials_for_sex(self, sex: str) -> List[VialState]:
        target = str(sex).lower().strip()
        with self._lock:
            return [
                item
                for item in self.state.vials
                if str(item.target_sex).lower().strip() == target and item.current_count < item.max_count
            ]

    def _choose_first_available_target(self, *targets: str) -> Optional[VialState]:
        wanted = {str(item).lower().strip() for item in targets if str(item).strip()}
        with self._lock:
            for item in self.state.vials:
                if str(item.target_sex).lower().strip() in wanted and item.current_count < item.max_count:
                    return item
        return None

    def choose_destination_for_sex(self, sex: str) -> VialState:
        destination = self._choose_first_available_target(sex)
        if destination is None:
            raise OperatorFacingError(
                f"No available {sex} vial. Adjust vial capacities or use Debug > Manual recovery."
            )
        return destination

    def choose_junk_destination(self) -> VialState:
        destination = self._choose_first_available_target("junk", "discard", "unknown")
        if destination is None:
            raise OperatorFacingError(
                "No junk vial is available. Add a junk vial definition or use Debug > Manual recovery."
            )
        return destination

    def choose_auto_destination(self, sex: str) -> tuple[VialState, str]:
        target = str(sex).lower().strip()
        if target in {"male", "female"}:
            destination = self._choose_first_available_target(target)
            if destination is not None:
                return destination, target
            if bool(getattr(self.settings, "auto_fallback_to_junk_when_full", True)):
                return self.choose_junk_destination(), "junk"
            raise OperatorFacingError(
                f"No available {target} vial. Adjust vial capacities or use Debug > Manual recovery."
            )

        if bool(getattr(self.settings, "auto_uncertain_to_junk", True)):
            return self.choose_junk_destination(), "junk"

        raise OperatorFacingError(
            "The sexing result was uncertain and automatic junk routing is disabled."
        )

    def increment_vial(self, vial_id: str) -> None:
        with self._lock:
            for vial in self.state.vials:
                if vial.vial_id == vial_id:
                    vial.current_count = min(int(vial.max_count), int(vial.current_count) + 1)
                    vial.last_routed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    vial.status = "READY" if vial.current_count < vial.max_count else "FULL"
                    break
            self.state.updated_at = time.time()

    def reset_vial_counts(self) -> None:
        if self.snapshot().busy:
            raise OperatorFacingError("Stop the current task before resetting vial counts.")
        with self._lock:
            for vial in self.state.vials:
                vial.current_count = 0
                vial.status = "READY"
                vial.last_routed_at = ""
            self.state.selected_destination = ""
            self.state.current_target = ""
            self.state.updated_at = time.time()
        self._clear_sexing_state()
        next_action = "Capture Channel" if self.hardware.homed else "Initialize"
        stage = WorkflowStage.READY if self.hardware.homed else WorkflowStage.IDLE
        stage_label = "Ready for new run" if self.hardware.homed else "Idle"
        self._set_stage(stage, stage_label, next_action=next_action, message="Vial counts reset. Ready for a new run.")
        self.log("Vial counts reset.")

    def _has_loaded_flies(self) -> bool:
        with self._lock:
            return any(vial.current_count > 0 for vial in self.state.vials)

    def _consume_channel_position(self, channel_result: Dict[str, Any], consumed_x_mm: float) -> Dict[str, Any]:
        updated = dict(channel_result)
        x_positions = [float(v) for v in updated.get("x_positions_mm", [])]
        if x_positions:
            index = min(range(len(x_positions)), key=lambda idx: abs(x_positions[idx] - consumed_x_mm))
            x_positions.pop(index)
            updated["x_positions_mm"] = x_positions
            x_positions_px = list(updated.get("x_positions_px", []))
            if len(x_positions_px) > index:
                x_positions_px.pop(index)
                updated["x_positions_px"] = x_positions_px
            detections = list(updated.get("detections", []))
            if len(detections) > index:
                detections.pop(index)
                updated["detections"] = detections
        updated["count"] = len(updated.get("x_positions_mm", []))
        updated["fly_remaining"] = bool(updated["count"])
        updated["result_json_path"] = str(self.channel.result_json_path.resolve())
        self.channel.save_result(updated)
        self._set_channel_state(updated, stale=True)
        return updated

    def _channel_position_tolerance_mm(self) -> float:
        try:
            value = float(getattr(self.settings, "channel_position_match_tolerance_mm", 4.0))
        except Exception:
            value = 4.0
        return max(0.5, value)

    def _position_still_present(
        self,
        channel_result: Dict[str, Any],
        reference_mm: float,
        *,
        tolerance_mm: Optional[float] = None,
    ) -> bool:
        positions = [float(v) for v in channel_result.get("x_positions_mm", [])]
        tolerance = self._channel_position_tolerance_mm() if tolerance_mm is None else max(0.5, float(tolerance_mm))
        return any(abs(position - float(reference_mm)) <= tolerance for position in positions)

    def _next_source_position(
        self,
        channel_result: Dict[str, Any],
        *,
        skipped_positions_mm: Optional[List[float]] = None,
    ) -> Optional[float]:
        positions = sorted((float(v) for v in channel_result.get("x_positions_mm", [])), reverse=True)
        skipped = [float(v) for v in (skipped_positions_mm or [])]
        tolerance = self._channel_position_tolerance_mm()
        for position in positions:
            if any(abs(position - skipped_position) <= tolerance for skipped_position in skipped):
                continue
            return position
        return None

    def _record_pick_failure(self, failure_records: List[Dict[str, float]], source_position_mm: float) -> int:
        tolerance = self._channel_position_tolerance_mm()
        for record in failure_records:
            if abs(float(record.get("anchor_mm", 0.0)) - float(source_position_mm)) <= tolerance:
                record["anchor_mm"] = float(source_position_mm)
                record["attempts"] = int(record.get("attempts", 0) or 0) + 1
                return int(record["attempts"])
        failure_records.append({"anchor_mm": float(source_position_mm), "attempts": 1})
        return 1

    def _clear_failure_record(self, failure_records: List[Dict[str, float]], source_position_mm: float) -> None:
        tolerance = self._channel_position_tolerance_mm()
        failure_records[:] = [
            record
            for record in failure_records
            if abs(float(record.get("anchor_mm", 0.0)) - float(source_position_mm)) > tolerance
        ]

    # ------------------------------------------------------------------
    # internal workflow implementations
    # ------------------------------------------------------------------
    def _initialize_impl(self) -> None:
        self.log("Initializing gantry and outputs.")
        self.hardware.reset_outputs()
        self.hardware.home()
        self.refresh_readiness()
        self._update(selected_destination="", current_target="")
        self.log("System initialized.")

    def _capture_channel_impl(
        self,
        *,
        park_nozzle: bool = True,
        clear_target: bool = True,
        log_message: Optional[str] = "Capturing channel frame.",
    ) -> Dict[str, Any]:
        self._check_cancelled("Capture stopped before the next specimen was loaded.")
        if not self.hardware.homed:
            self.log("System not homed; homing before channel capture.")
            self.hardware.home()
        if park_nozzle:
            self.hardware.park_for_channel_capture(
                vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
                channel_camera_position_mm=float(self.settings.channel_camera_position_mm),
            )
        if log_message:
            self.log(log_message)
        result = self.channel.capture_channel()
        result["result_json_path"] = str(self.channel.result_json_path.resolve())
        timings = dict(result.get("timings", {}) or {})
        total_s = float(timings.get("total_s", 0.0) or 0.0)
        if total_s > 0.0:
            summary_label = "Channel capture complete" if park_nozzle else "Channel refresh complete"
            self.log(
                f"{summary_label}: "
                f"{int(result.get('count', 0) or 0)} flies detected in {total_s:.2f}s "
                f"(capture {float(timings.get('frame_capture_s', 0.0) or 0.0):.2f}s, "
                f"detect {float(timings.get('processing_s', 0.0) or 0.0):.2f}s)."
            )
        self._set_channel_state(result, stale=False)
        if clear_target:
            self._update(selected_destination="", current_target="")
        return result

    def _chamber_reacquire_hold_s(self) -> float:
        try:
            configured = float(getattr(self.settings, "chamber_reacquire_pick_delay_s", 0.0) or 0.0)
        except Exception:
            configured = 0.0
        base_delay = max(0.0, float(self.settings.vacuum_pick_delay_s))
        return max(base_delay, configured, 0.75)

    def _chamber_clear_attempts(self) -> int:
        try:
            configured = int(getattr(self.settings, "chamber_clear_retry_attempts", 2) or 2)
        except Exception:
            configured = 2
        return max(1, configured)

    def _inspect_chamber(self) -> Dict[str, Any]:
        inspect_method = getattr(self.sexing, "inspect_chamber", None)
        if not callable(inspect_method):
            return {"occupied": False, "occupancy_score": 0.0, "detail": "Chamber inspection unavailable."}
        try:
            payload = dict(inspect_method() or {})
        except Exception as exc:
            self.log(f"Chamber inspection failed: {exc}")
            return {
                "occupied": False,
                "occupancy_score": 0.0,
                "detail": f"CHAMBER_INSPECTION_FAILED:{exc}",
            }
        return payload

    def _attempt_chamber_clear(
        self,
        *,
        destination: VialState,
        reason: str,
        fly_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        attempts_allowed = self._chamber_clear_attempts()
        prefix = f"Fly {fly_index}: " if fly_index is not None and fly_index > 0 else ""
        last_inspection: Dict[str, Any] = {}
        for attempt_number in range(1, attempts_allowed + 1):
            self._check_cancelled("Stop requested while clearing the sexing chamber.")
            self._update(
                current_target=f"{prefix}clearing chamber",
                selected_destination=destination.label,
                status_message=(
                    f"{prefix}sexing chamber appears occupied. "
                    f"Automatic recovery {attempt_number}/{attempts_allowed} -> {destination.label}."
                ),
            )
            self.log(
                f"{prefix}Sexing chamber appears occupied. "
                f"Automatic recovery {attempt_number}/{attempts_allowed} -> {destination.label}. {reason}"
            )
            self.hardware.reacquire_from_chamber(
                vacuum_pick_delay_s=self._chamber_reacquire_hold_s(),
                vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
                chamber_position_mm=float(self.settings.chamber_position_mm),
            )
            self.hardware.drop_into_vial(
                float(destination.position_mm),
                vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s),
                vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            )
            last_inspection = self._inspect_chamber()
            if not bool(last_inspection.get("occupied", False)):
                cleared_message = f"{prefix}Sexing chamber clear after automatic recovery to {destination.label}."
                self.log(cleared_message)
                return {
                    "cleared": True,
                    "attempts": attempt_number,
                    "inspection": last_inspection,
                    "message": cleared_message,
                }
        blocked_message = (
            f"{prefix}Sexing chamber still appears occupied after {attempts_allowed} automatic recovery attempts."
        )
        self.log(blocked_message)
        return {
            "cleared": False,
            "attempts": attempts_allowed,
            "inspection": last_inspection,
            "message": blocked_message,
        }

    def _route_next_fly_impl(
        self,
        *,
        channel_result: Optional[Dict[str, Any]] = None,
        require_fresh_capture: bool = True,
        finalize_stage: bool = True,
    ) -> Dict[str, Any]:
        self._check_cancelled("Routing stopped before the next specimen was picked.")

        if channel_result is None:
            channel_result = self.channel.load_last_result()
        if channel_result is None:
            raise OperatorFacingError("Capture the channel before routing the next fly.")

        if require_fresh_capture and self.snapshot().channel.stale:
            self._set_channel_state(channel_result, stale=True)
            raise OperatorFacingError("Capture the channel again before routing the next fly.")

        if channel_result.get("fly_remaining") is not True:
            self._set_channel_state(channel_result, stale=False)
            raise OperatorFacingError("No flies remain in the channel. Capture again or start auto when more flies are loaded.")

        self._set_channel_state(channel_result, stale=False)
        positions_mm = [float(v) for v in channel_result.get("x_positions_mm", [])]
        if not positions_mm:
            raise OperatorFacingError("No pickup coordinates were saved. Capture the channel again.")

        source_position_mm = max(positions_mm)
        pickup_position = self.hardware.pickup_position_from_channel(
            positions_mm,
            pickup_offset_mm=float(self.settings.pickup_offset_mm),
        )

        self._clear_sexing_state("Classifying the next specimen...")
        self.log(f"Routing next fly from {pickup_position:.2f} mm.")
        self._update(current_target=f"Pickup {pickup_position:.1f} mm")
        self.hardware.move_to_pickup(
            pickup_position,
            vacuum_pick_delay_s=float(self.settings.vacuum_pick_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            approach_position_mm=float(self.settings.channel_camera_position_mm),
        )

        self._update(current_target="Sexing chamber")
        self.hardware.drop_in_chamber_and_clear(
            vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            chamber_position_mm=float(self.settings.chamber_position_mm),
            chamber_clear_offset_mm=float(self.settings.chamber_clear_offset_mm),
        )
        self._check_cancelled(
            "Routing stopped with a specimen in the chamber. Use Debug > Manual recovery if needed."
        )

        time.sleep(max(0.0, float(self.settings.classification_delay_s)))
        sex_result = self.sexing.classify()
        self._set_sexing_state(sex_result)
        self.log(f"Sexing result: {sex_result['label']} ({sex_result['confidence']:.2f}).")

        chosen_sex = str(sex_result.get("label", "UNCERTAIN")).lower()
        destination: Optional[VialState] = None
        if chosen_sex not in {"male", "female"}:
            manual_timeout_s = min(5.0, max(1.0, float(self.settings.confirmation_timeout_s)))
            choice = self.request_choice(
                title="Manual sex routing",
                message=(
                    "The sexing model was missing or uncertain. The fly is waiting in the chamber. "
                    f"Choose Male or Female within {manual_timeout_s:.0f}s, or the specimen will be sent to the junk vial."
                ),
                options=["Male", "Female"],
                timeout_s=manual_timeout_s,
            )
            if choice is None:
                destination = self.choose_junk_destination()
                sex_result = dict(sex_result)
                prior_detail = str(sex_result.get("detail", "") or "").strip()
                timeout_detail = f"No operator response within {manual_timeout_s:.0f}s; sending specimen to {destination.label}."
                sex_result["detail"] = f"{prior_detail}; {timeout_detail}" if prior_detail else timeout_detail
                self._set_sexing_state(sex_result)
                self.log(
                    f"No operator response within {manual_timeout_s:.0f}s; sending specimen to {destination.label}."
                )
            else:
                chosen_sex = str(choice).strip().lower()
                self.log(f"Operator selected manual route: {chosen_sex}.")

        if destination is None:
            destination = self.choose_destination_for_sex(chosen_sex)
        self._update(selected_destination=destination.label, current_target=destination.label)
        self.hardware.reacquire_from_chamber(
            vacuum_pick_delay_s=self._chamber_reacquire_hold_s(),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            chamber_position_mm=float(self.settings.chamber_position_mm),
        )
        self.hardware.drop_into_vial(
            float(destination.position_mm),
            vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
        )
        self.increment_vial(destination.vial_id)
        self.log(f"Fly routed to {destination.label} ({destination.target_sex}).")

        updated_channel = self._consume_channel_position(channel_result, source_position_mm)
        self.hardware.park_for_channel_capture(
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            channel_camera_position_mm=float(self.settings.channel_camera_position_mm),
        )
        self._update(current_target="", selected_destination=destination.label)

        if finalize_stage:
            self._set_stage(
                WorkflowStage.READY,
                "Fly routed",
                next_action="Capture Channel",
                message=f"Fly routed to {destination.label}. Capture the channel again for the next specimen.",
            )
        return updated_channel

    def _auto_route_from_source_impl(
        self,
        *,
        channel_result: Dict[str, Any],
        source_position_mm: float,
        fly_index: int,
    ) -> Dict[str, Any]:
        positions_mm = [float(v) for v in channel_result.get("x_positions_mm", [])]
        if not positions_mm:
            raise OperatorFacingError("No pickup coordinates were saved. Capture the channel again.")
        if not self._position_still_present(channel_result, source_position_mm):
            raise OperatorFacingError("The selected source position is no longer present. Capture the channel again.")

        before_count = int(channel_result.get("count", len(positions_mm)) or len(positions_mm))
        pickup_position = self.hardware.pickup_position_from_channel(
            [source_position_mm],
            pickup_offset_mm=float(self.settings.pickup_offset_mm),
        )

        self._clear_sexing_state("Classifying the next specimen...")
        self._update(
            current_target=f"Fly {fly_index} from {source_position_mm:.1f} mm",
            selected_destination="",
            status_message=(
                f"Fly {fly_index}: picking from {source_position_mm:.1f} mm. "
                f"{before_count} flies detected in the channel."
            ),
        )
        self.log(f"Fly {fly_index}: attempting pickup from {source_position_mm:.2f} mm.")
        self.hardware.move_to_pickup(
            pickup_position,
            vacuum_pick_delay_s=float(self.settings.vacuum_pick_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            approach_position_mm=float(self.settings.channel_camera_position_mm),
        )

        self._update(
            current_target=f"Fly {fly_index} in chamber",
            status_message=f"Fly {fly_index}: moving to the sexing chamber.",
        )
        self.hardware.drop_in_chamber_and_clear(
            vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            chamber_position_mm=float(self.settings.chamber_position_mm),
            chamber_clear_offset_mm=float(self.settings.chamber_clear_offset_mm),
        )
        self._check_cancelled(
            "Routing stopped with a specimen in the chamber. Use Debug > Manual recovery if needed."
        )

        time.sleep(max(0.0, float(self.settings.classification_delay_s)))
        sex_result = dict(self.sexing.classify())
        chosen_label = str(sex_result.get("label", "UNCERTAIN")).lower().strip()
        confidence = float(sex_result.get("confidence", 0.0) or 0.0)
        destination, destination_mode = self.choose_auto_destination(chosen_label)

        if destination_mode == "junk":
            if chosen_label in {"male", "female"}:
                sex_result["detail"] = (
                    f"{chosen_label.title()} detected, but the primary {chosen_label} vials are full. "
                    f"Sending this specimen to {destination.label}."
                )
            else:
                sex_result["detail"] = sex_result.get("detail") or f"Classifier uncertain. Sending this specimen to {destination.label}."
        self._set_sexing_state(sex_result)
        self.log(
            f"Fly {fly_index}: sexing result {sex_result.get('label', 'UNCERTAIN')} "
            f"({confidence:.2f}) -> {destination.label}."
        )

        after_result: Dict[str, Any]
        verified_channel_result: Optional[Dict[str, Any]] = None
        after_count = before_count
        source_still_present = True
        chamber_clear_attempts = 0
        chamber_clear_success = True

        if destination_mode == "junk" and chosen_label not in {"male", "female"}:
            self._update(
                selected_destination="",
                current_target=f"Fly {fly_index}: verifying pickup",
                status_message=f"Fly {fly_index}: classifier uncertain. Verifying the pickup before routing to {destination.label}.",
            )
            after_result = self._capture_channel_impl(
                park_nozzle=False,
                clear_target=False,
                log_message="Refreshing channel to verify an uncertain pickup.",
            )
            after_count = int(after_result.get("count", 0) or 0)
            source_still_present = self._position_still_present(after_result, source_position_mm)
            pickup_success = (after_count < before_count) or (not source_still_present)
            if pickup_success:
                verified_channel_result = dict(after_result)
            if not pickup_success:
                pickup_miss_message = (
                    f"Fly {fly_index}: likely pickup miss at {source_position_mm:.1f} mm. "
                    "The source is still visible on the channel. Skipping junk routing."
                )
                sex_result["label"] = "UNCERTAIN"
                sex_result["uncertain"] = True
                sex_result["detail"] = pickup_miss_message
                self._set_sexing_state(sex_result)
                chamber_inspection = self._inspect_chamber()
                if bool(chamber_inspection.get("occupied", False)):
                    clear_info = self._attempt_chamber_clear(
                        destination=self.choose_junk_destination(),
                        reason="Clearing a specimen that still appears to be in the sexing chamber after a likely pickup miss.",
                        fly_index=fly_index,
                    )
                    chamber_clear_attempts = int(clear_info.get("attempts", 0) or 0)
                    chamber_clear_success = bool(clear_info.get("cleared", False))
                    if not chamber_clear_success:
                        blocked_message = (
                            f"Fly {fly_index}: likely pickup miss, and the sexing chamber still appears occupied. "
                            "Use Debug > Manual recovery before continuing."
                        )
                        self._update(current_target="", selected_destination="", status_message=blocked_message)
                        self.log(blocked_message)
                        return {
                            "outcome": "chamber_blocked",
                            "channel_result": after_result,
                            "source_position_mm": float(source_position_mm),
                            "destination_label": destination.label,
                            "destination_mode": destination_mode,
                            "sex_label": str(sex_result.get("label", "UNCERTAIN")),
                            "confidence": confidence,
                            "after_count": after_count,
                            "source_still_present": source_still_present,
                            "chamber_clear_attempts": chamber_clear_attempts,
                            "chamber_clear_success": chamber_clear_success,
                            "status_message": blocked_message,
                        }
                self._update(current_target="", selected_destination="", status_message=pickup_miss_message)
                self.log(pickup_miss_message)
                return {
                    "outcome": "pickup_failed",
                    "channel_result": after_result,
                    "source_position_mm": float(source_position_mm),
                    "destination_label": destination.label,
                    "destination_mode": destination_mode,
                    "sex_label": str(sex_result.get("label", "UNCERTAIN")),
                    "confidence": confidence,
                    "after_count": after_count,
                    "source_still_present": source_still_present,
                    "chamber_clear_attempts": chamber_clear_attempts,
                    "chamber_clear_success": chamber_clear_success,
                    "status_message": pickup_miss_message,
                }

        self._update(
            selected_destination=destination.label,
            current_target=destination.label,
            status_message=f"Fly {fly_index}: sending to {destination.label}.",
        )
        self.hardware.reacquire_from_chamber(
            vacuum_pick_delay_s=self._chamber_reacquire_hold_s(),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            chamber_position_mm=float(self.settings.chamber_position_mm),
        )
        self.hardware.drop_into_vial(
            float(destination.position_mm),
            vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
        )

        try:
            after_result = self._capture_channel_impl(
                park_nozzle=False,
                clear_target=False,
                log_message="Refreshing channel after routing attempt.",
            )
        except Exception as exc:
            if verified_channel_result is None:
                raise
            after_result = dict(verified_channel_result)
            after_result["result_json_path"] = str(self.channel.result_json_path.resolve())
            self._set_channel_state(after_result, stale=False)
            self.log(f"Channel refresh after routing failed; reusing the last verified channel frame: {exc}")
        after_count = int(after_result.get("count", 0) or 0)
        source_still_present = self._position_still_present(after_result, source_position_mm)
        pickup_success = (after_count < before_count) or (not source_still_present)

        chamber_inspection = self._inspect_chamber()
        if bool(chamber_inspection.get("occupied", False)):
            clear_info = self._attempt_chamber_clear(
                destination=destination,
                reason="Clearing a specimen that still appears to be in the sexing chamber after routing.",
                fly_index=fly_index,
            )
            chamber_clear_attempts = int(clear_info.get("attempts", 0) or 0)
            chamber_clear_success = bool(clear_info.get("cleared", False))
            if not chamber_clear_success:
                blocked_message = (
                    f"Fly {fly_index}: the source left the channel, but the sexing chamber still appears occupied. "
                    "Use Debug > Manual recovery before continuing."
                )
                sex_result["label"] = str(sex_result.get("label", "UNCERTAIN") or "UNCERTAIN")
                sex_result["detail"] = blocked_message
                sex_result["uncertain"] = True
                self._set_sexing_state(sex_result)
                self._update(current_target="", selected_destination=destination.label, status_message=blocked_message)
                self.log(blocked_message)
                return {
                    "outcome": "chamber_blocked",
                    "channel_result": after_result,
                    "source_position_mm": float(source_position_mm),
                    "destination_label": destination.label,
                    "destination_mode": destination_mode,
                    "sex_label": str(sex_result.get("label", "UNCERTAIN")),
                    "confidence": confidence,
                    "after_count": after_count,
                    "source_still_present": source_still_present,
                    "chamber_clear_attempts": chamber_clear_attempts,
                    "chamber_clear_success": chamber_clear_success,
                    "status_message": blocked_message,
                }

        if not pickup_success:
            pickup_miss_message = (
                f"Fly {fly_index}: likely pickup miss at {source_position_mm:.1f} mm. "
                "The source is still visible on the channel."
            )
            sex_result["label"] = "UNCERTAIN"
            sex_result["uncertain"] = True
            sex_result["detail"] = pickup_miss_message
            self._set_sexing_state(sex_result)
            self._update(current_target="", selected_destination="", status_message=pickup_miss_message)
            self.log(pickup_miss_message)
            return {
                "outcome": "pickup_failed",
                "channel_result": after_result,
                "source_position_mm": float(source_position_mm),
                "destination_label": destination.label,
                "destination_mode": destination_mode,
                "sex_label": str(sex_result.get("label", "UNCERTAIN")),
                "confidence": confidence,
                "after_count": after_count,
                "source_still_present": source_still_present,
                "chamber_clear_attempts": chamber_clear_attempts,
                "chamber_clear_success": chamber_clear_success,
                "status_message": pickup_miss_message,
            }

        self.increment_vial(destination.vial_id)
        if destination_mode == "junk" and chosen_label not in {"male", "female"}:
            message = f"Fly {fly_index}: classifier uncertain; sent to {destination.label}. {after_count} flies remain."
        elif destination_mode == "junk":
            message = f"Fly {fly_index}: sex-specific vial full; sent to {destination.label}. {after_count} flies remain."
        else:
            message = f"Fly {fly_index}: sent to {destination.label}. {after_count} flies remain in the channel."
        self._update(current_target="", selected_destination=destination.label, status_message=message)
        self.log(message)
        return {
            "outcome": "routed",
            "channel_result": after_result,
            "source_position_mm": float(source_position_mm),
            "destination_label": destination.label,
            "destination_mode": destination_mode,
            "sex_label": str(sex_result.get("label", "UNCERTAIN")),
            "confidence": confidence,
            "after_count": after_count,
            "source_still_present": False,
            "chamber_clear_attempts": chamber_clear_attempts,
            "chamber_clear_success": chamber_clear_success,
            "status_message": message,
        }

    def _run_assay_impl(self, *, finalize_stage: bool = True) -> Dict[str, Any]:
        self.refresh_readiness()
        if not self.state.readiness.assay_background_ready:
            raise OperatorFacingError("Assay background missing. Capture a background before running the assay.")
        if not self.state.readiness.assay_calibration_ready:
            raise OperatorFacingError("Assay calibration missing. Load or create calibration before running the assay.")

        self.log("Recording assay run.")

        def preview_callback(payload: Dict[str, Any]) -> None:
            if payload.get("preview_path"):
                self._set_assay_state({"preview_path": payload["preview_path"]})
            time_s = float(payload.get("time_s", 0.0) or 0.0)
            duration_s = float(self.assay.profile.assay_duration_s)
            self._update(status_message=f"Recording assay {time_s:0.1f}s / {duration_s:0.1f}s")

        manifest = self.assay.run_assay(
            stop_event=self._task_stop_event,
            logger=self.log,
            preview_callback=preview_callback,
        )
        self._set_assay_state({
            "run_dir": manifest.get("run_dir", ""),
            "duration_s": manifest.get("duration_s", 0.0),
            "preview_path": str(self.assay.live_preview_path.resolve()) if self.assay.live_preview_path.exists() else "",
        })
        if finalize_stage:
            self._set_stage(
                WorkflowStage.ASSAY,
                "Assay recorded",
                next_action="Process Last Assay",
                message="Assay recording complete.",
            )
        self.log(f"Assay recorded: {manifest.get('run_dir', '')}")
        return manifest

    def _process_last_assay_impl(self, *, finalize_stage: bool = True) -> Dict[str, Any]:
        self.log("Processing last assay.")

        def progress_callback(payload: Dict[str, Any]) -> None:
            stage = str(payload.get("stage", "processing"))
            self._update(status_message=f"Processing assay: {stage}")

        result = self.assay.process_last(logger=self.log, progress_callback=progress_callback)
        self._set_assay_state(result)
        if finalize_stage:
            self._set_stage(
                WorkflowStage.RESULTS,
                "Results ready",
                next_action="Upload Last Run",
                message="Assay processing complete.",
            )
        self.log("Assay processing complete.")
        return result

    # ------------------------------------------------------------------
    # public workflow actions
    # ------------------------------------------------------------------
    def start_initialize(self) -> bool:
        def task():
            self._initialize_impl()
            self._set_stage(
                WorkflowStage.READY,
                "Ready",
                next_action="Capture Channel",
                message="System initialized and homed.",
            )

        return self._begin_task("initialize", WorkflowStage.INITIALIZING, "Initializing", "Initializing system...", task)

    def start_auto_flow(self) -> bool:
        def _auto_stats_message(*, routed_count: int, pickup_rejects: int, skipped_count: int) -> str:
            return f"Auto stats: routed {routed_count} · rejects {pickup_rejects} · skipped {skipped_count}."

        def task():
            self.log("Auto flow started.")
            if any(vial.current_count > 0 for vial in self.snapshot().vials):
                self.log("Auto flow is continuing with the current vial counts. Use NEW RUN / RESET COUNTS to start fresh.")
            self._set_stage(
                WorkflowStage.ROUTING,
                "Auto flow",
                next_action="Stop",
                message="Initializing, clearing the channel view, and loading specimens automatically...",
            )
            self._clear_sexing_state()
            self._initialize_impl()

            preflight_inspection = self._inspect_chamber()
            if bool(preflight_inspection.get("occupied", False)):
                junk_destination = self.choose_junk_destination()
                self.log(
                    f"Sexing chamber appears occupied before auto flow. Attempting automatic recovery to {junk_destination.label}."
                )
                clear_info = self._attempt_chamber_clear(
                    destination=junk_destination,
                    reason="Clearing a specimen left behind before a new auto-flow run.",
                )
                if not bool(clear_info.get("cleared", False)):
                    raise OperatorFacingError(
                        "The sexing chamber still appears occupied before loading. Use Debug > Manual recovery and try again."
                    )

            capture_session_factory = getattr(self.channel, "capture_session", None)
            capture_session = capture_session_factory() if callable(capture_session_factory) else contextlib.nullcontext()

            with capture_session:
                current_result = self._capture_channel_impl()
                master_payload: Dict[str, Any] = {
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "initial_result": deepcopy(current_result),
                    "master_positions_mm": [float(v) for v in current_result.get("x_positions_mm", [])],
                    "attempt_history": [],
                    "skipped_positions_mm": [],
                }
                self.channel.save_auto_flow_master(master_payload)
                self.log(
                    f"Auto flow master saved with {int(current_result.get('count', 0) or 0)} detected flies."
                )

                attempt_index = 0
                routed_count = 0
                pickup_rejects = 0
                skipped_positions_mm: List[float] = []
                failure_records: List[Dict[str, float]] = []
                max_attempts = max(1, int(getattr(self.settings, "auto_max_pick_attempts_per_location", 2) or 2))

                while True:
                    self._check_cancelled(
                        "Auto flow stopped. If a fly may remain in the chamber, use Debug > Manual recovery."
                    )

                    if not current_result.get("fly_remaining"):
                        break

                    source_position = self._next_source_position(
                        current_result,
                        skipped_positions_mm=skipped_positions_mm,
                    )
                    if source_position is None:
                        remaining_positions = [float(v) for v in current_result.get("x_positions_mm", [])]
                        message = (
                            "Auto flow paused after repeated pickup misses. "
                            f"Remaining channel positions: {', '.join(f'{value:.1f}' for value in remaining_positions) or '--'}. "
                            f"{_auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))}"
                        )
                        self._set_stage(
                            WorkflowStage.READY,
                            "Auto flow paused",
                            next_action="Capture Channel",
                            message=message,
                        )
                        self.log(message)
                        master_payload["latest_result"] = deepcopy(current_result)
                        master_payload["skipped_positions_mm"] = [float(v) for v in skipped_positions_mm]
                        master_payload["stats"] = {
                            "routed_count": routed_count,
                            "pickup_rejects": pickup_rejects,
                            "skipped_count": len(skipped_positions_mm),
                        }
                        self.channel.save_auto_flow_master(master_payload)
                        return

                    attempt_index += 1
                    fly_count = int(current_result.get("count", 0) or 0)
                    self._set_stage(
                        WorkflowStage.ROUTING,
                        "Auto flow",
                        next_action="Stop",
                        message=(
                            f"{fly_count} flies detected. "
                            f"Working on fly {attempt_index} from {source_position:.1f} mm."
                        ),
                    )

                    attempt = self._auto_route_from_source_impl(
                        channel_result=current_result,
                        source_position_mm=float(source_position),
                        fly_index=attempt_index,
                    )
                    current_result = dict(attempt.get("channel_result", current_result))
                    history_entry = {
                        "attempt_index": int(attempt_index),
                        "source_position_mm": float(source_position),
                        "outcome": str(attempt.get("outcome", "unknown")),
                        "sex_label": str(attempt.get("sex_label", "UNCERTAIN")),
                        "confidence": float(attempt.get("confidence", 0.0) or 0.0),
                        "destination_label": str(attempt.get("destination_label", "") or ""),
                        "captured_after_count": int(attempt.get("after_count", 0) or 0),
                        "captured_after_still_present": bool(attempt.get("source_still_present", False)),
                        "chamber_clear_attempts": int(attempt.get("chamber_clear_attempts", 0) or 0),
                        "chamber_clear_success": bool(attempt.get("chamber_clear_success", True)),
                        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }

                    outcome = str(attempt.get("outcome", "unknown") or "unknown")
                    status_message = str(attempt.get("status_message", "") or "").strip()

                    if outcome == "routed":
                        routed_count += 1
                        self._clear_failure_record(failure_records, float(source_position))
                    elif outcome == "pickup_failed":
                        pickup_rejects += 1
                        failure_attempts = self._record_pick_failure(failure_records, float(source_position))
                        history_entry["failure_attempts"] = int(failure_attempts)
                        if (
                            failure_attempts >= max_attempts
                            and self._position_still_present(current_result, float(source_position))
                        ):
                            skipped_positions_mm.append(float(source_position))
                            skip_message = (
                                f"Source {source_position:.1f} mm was attempted {failure_attempts} times and is still present. "
                                "Skipping this location and moving to the next fly."
                            )
                            status_message = skip_message
                            self._update(current_target="", status_message=skip_message)
                            self.log(skip_message)
                    elif outcome == "chamber_blocked":
                        status_message = status_message or (
                            "The sexing chamber still appears occupied after automatic recovery. "
                            "Use Debug > Manual recovery before continuing."
                        )
                        history_entry["failure_attempts"] = int(history_entry.get("failure_attempts", 0) or 0)
                        master_payload.setdefault("attempt_history", []).append(history_entry)
                        master_payload["latest_result"] = deepcopy(current_result)
                        master_payload["skipped_positions_mm"] = [float(v) for v in skipped_positions_mm]
                        master_payload["stats"] = {
                            "routed_count": routed_count,
                            "pickup_rejects": pickup_rejects,
                            "skipped_count": len(skipped_positions_mm),
                        }
                        self.channel.save_auto_flow_master(master_payload)
                        self._set_stage(
                            WorkflowStage.ERROR,
                            "Chamber recovery needed",
                            next_action="Debug / Advanced",
                            message=f"{status_message} {_auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))}",
                        )
                        return

                    combined_message = (
                        f"{status_message} {_auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))}"
                        if status_message
                        else _auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))
                    )
                    self._update(status_message=combined_message)

                    master_payload.setdefault("attempt_history", []).append(history_entry)
                    master_payload["latest_result"] = deepcopy(current_result)
                    master_payload["skipped_positions_mm"] = [float(v) for v in skipped_positions_mm]
                    master_payload["stats"] = {
                        "routed_count": routed_count,
                        "pickup_rejects": pickup_rejects,
                        "skipped_count": len(skipped_positions_mm),
                    }
                    self.channel.save_auto_flow_master(master_payload)

                master_payload["latest_result"] = deepcopy(current_result)
                master_payload["skipped_positions_mm"] = [float(v) for v in skipped_positions_mm]
                master_payload["stats"] = {
                    "routed_count": routed_count,
                    "pickup_rejects": pickup_rejects,
                    "skipped_count": len(skipped_positions_mm),
                }
                self.channel.save_auto_flow_master(master_payload)

            if not self._has_loaded_flies():
                self._set_stage(
                    WorkflowStage.READY,
                    "Channel clear",
                    next_action="Capture Channel",
                    message=(
                        "No flies remain in the channel. "
                        f"{_auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))}"
                    ),
                )
                self.log("Auto flow finished with no flies detected.")
                return

            self.refresh_readiness()
            if self.state.readiness.assay_background_ready and self.state.readiness.assay_calibration_ready:
                self._check_cancelled(
                    "Auto flow stopped after loading. Use Run Assay when you are ready to continue."
                )
                self._set_stage(
                    WorkflowStage.ASSAY,
                    "Auto flow",
                    next_action="Stop",
                    message="Channel clear. Running assay automatically.",
                )
                self._run_assay_impl(finalize_stage=False)
                self._check_cancelled(
                    "Auto flow stopped after assay recording. Use Process Last to continue."
                )
                self._set_stage(
                    WorkflowStage.PROCESSING,
                    "Auto flow",
                    next_action="Stop",
                    message="Processing assay automatically.",
                )
                self._process_last_assay_impl(finalize_stage=False)
                self._set_stage(
                    WorkflowStage.RESULTS,
                    "Auto flow complete",
                    next_action="Upload Last Run",
                    message=(
                        "Auto flow finished. Assay processed and ready for review. "
                        f"{_auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))}"
                    ),
                )
                self.log("Auto flow finished through assay processing.")
                return

            if not self.state.readiness.assay_background_ready:
                next_action = "Capture Assay Background"
            elif not self.state.readiness.assay_calibration_ready:
                next_action = "Calibrate Assay"
            else:
                next_action = "Run Assay"
            self._set_stage(
                WorkflowStage.READY,
                "Loading complete",
                next_action=next_action,
                message=(
                    "Channel clear. Flies are loaded. Prepare or run the assay. "
                    f"{_auto_stats_message(routed_count=routed_count, pickup_rejects=pickup_rejects, skipped_count=len(skipped_positions_mm))}"
                ),
            )
            self.log("Auto flow finished at loading stage; assay setup is still required.")

        return self._begin_task("auto_flow", WorkflowStage.ROUTING, "Auto flow", "Starting automated workflow...", task)

    def start_capture_channel(self) -> bool:
        def task():
            result = self._capture_channel_impl()
            if result.get("fly_remaining"):
                self._set_stage(
                    WorkflowStage.CHANNEL,
                    "Channel captured",
                    next_action="Route Next Fly",
                    message=f"{int(result.get('count', 0))} flies detected.",
                )
            else:
                self._set_stage(
                    WorkflowStage.CHANNEL,
                    "Channel clear",
                    next_action="Run Assay",
                    message="No flies remain in the channel.",
                )
            self.log(f"Channel capture complete: {result.get('count', 0)} flies detected.")

        return self._begin_task("capture_channel", WorkflowStage.CHANNEL, "Capturing channel", "Capturing channel image...", task)

    def start_route_next_fly(self) -> bool:
        def task():
            self._route_next_fly_impl()

        return self._begin_task("route_next_fly", WorkflowStage.ROUTING, "Routing fly", "Routing next fly...", task)

    def start_capture_channel_background(self) -> bool:
        def task():
            self.log("Capturing channel background.")
            self.channel.capture_background()
            self.refresh_readiness()
            self._set_stage(
                WorkflowStage.CHANNEL,
                "Channel ready",
                next_action="Calibrate Channel",
                message="Channel background captured.",
            )
            self.log("Channel background saved.")

        return self._begin_task("channel_background", WorkflowStage.CHANNEL, "Channel setup", "Capturing channel background...", task)

    def start_calibrate_channel(self) -> bool:
        def task():
            self.log("Launching channel calibration.")
            self.channel.calibrate()
            self.refresh_readiness()
            self._set_stage(
                WorkflowStage.CHANNEL,
                "Channel calibrated",
                next_action="Capture Channel",
                message="Channel calibration saved.",
            )
            self.log("Channel calibration saved.")

        return self._begin_task("channel_calibrate", WorkflowStage.CHANNEL, "Channel calibration", "Calibrating channel...", task)

    def start_capture_assay_background(self) -> bool:
        def task():
            self.log("Capturing assay background.")
            self.assay.capture_background(logger=self.log)
            self.refresh_readiness()
            self._set_stage(
                WorkflowStage.ASSAY,
                "Assay ready",
                next_action="Calibrate Assay",
                message="Assay background captured.",
            )

        return self._begin_task("assay_background", WorkflowStage.ASSAY, "Assay setup", "Capturing assay background...", task)

    def start_restore_assay_background(self) -> bool:
        def task():
            self.log("Restoring previous assay background.")
            self.assay.restore_previous_background()
            self.refresh_readiness()
            self._set_stage(
                WorkflowStage.ASSAY,
                "Assay ready",
                next_action="Capture Preview",
                message="Previous assay background restored.",
            )

        return self._begin_task("assay_restore_background", WorkflowStage.ASSAY, "Assay setup", "Restoring previous background...", task)

    def start_calibrate_assay(self) -> bool:
        def task():
            self.log("Launching assay calibration.")
            path = self.assay.calibrate()
            self.refresh_readiness()
            self._set_stage(
                WorkflowStage.ASSAY,
                "Assay calibrated",
                next_action="Run Assay",
                message=f"Assay calibration saved: {path}",
            )
            self.log(f"Assay calibration saved: {path}")

        return self._begin_task("assay_calibrate", WorkflowStage.ASSAY, "Assay calibration", "Calibrating assay...", task)

    def start_capture_assay_preview(self) -> bool:
        def task():
            preview = self.assay.capture_preview()
            self._set_assay_state({"preview_path": preview.get("preview_path", "")})
            self._set_stage(
                WorkflowStage.ASSAY,
                "Assay preview",
                next_action="Run Assay",
                message="Assay preview updated.",
            )
            self.log("Assay preview updated.")

        return self._begin_task("assay_preview", WorkflowStage.ASSAY, "Assay preview", "Capturing assay preview...", task)

    def start_run_assay(self) -> bool:
        def task():
            self._run_assay_impl()

        return self._begin_task("run_assay", WorkflowStage.ASSAY, "Recording assay", "Recording assay...", task)

    def start_process_last_assay(self) -> bool:
        def task():
            self._process_last_assay_impl()

        return self._begin_task("process_assay", WorkflowStage.PROCESSING, "Processing assay", "Processing assay...", task)

    def start_upload_last_run(self) -> bool:
        def task():
            self.log("Uploading last assay artifacts.")
            result = self.assay.upload_last(logger=self.log)
            folder_id = result.get("folder_id") or result.get("upload_folder_id") or ""
            status = "Uploaded" if folder_id else "Upload complete"
            self._set_assay_state({"upload_status": status})
            self._set_stage(
                WorkflowStage.RESULTS,
                "Uploaded",
                next_action="Capture Channel",
                message="Last assay artifacts uploaded.",
            )
            self.log(f"Upload complete: {folder_id}")

        return self._begin_task("upload_last", WorkflowStage.RESULTS, "Uploading", "Uploading assay artifacts...", task)

    # ------------------------------------------------------------------
    # debug / recovery actions
    # ------------------------------------------------------------------
    def move_home_debug(self) -> None:
        self.hardware.home()
        self.periodic_refresh()
        self.log("Debug home complete.")

    def move_absolute_debug(self, position_mm: float) -> float:
        pos = self.hardware.move_absolute(position_mm)
        self.periodic_refresh()
        self.log(f"Debug move absolute: {pos:.2f} mm")
        return pos

    def move_relative_debug(self, delta_mm: float) -> float:
        pos = self.hardware.move_relative(delta_mm)
        self.periodic_refresh()
        self.log(f"Debug move relative: {delta_mm:+.2f} mm -> {pos:.2f} mm")
        return pos

    def vacuum_on_debug(self) -> None:
        self.hardware.vacuum_on()
        self.periodic_refresh()
        self.log("Debug vacuum ON")

    def vacuum_off_debug(self) -> None:
        self.hardware.vacuum_off()
        self.periodic_refresh()
        self.log("Debug vacuum OFF")

    def pulse_vibration_debug(self) -> None:
        self.hardware.vibration_pulse()
        self.log("Debug vibration pulse")

    def outputs_off_debug(self) -> None:
        self.hardware.reset_outputs()
        self.periodic_refresh()
        self.log("Debug outputs OFF")

    def manual_route_from_chamber(self, vial_id: str) -> None:
        destination = None
        with self._lock:
            for vial in self.state.vials:
                if vial.vial_id == vial_id:
                    destination = vial
                    break
        if destination is None:
            raise OperatorFacingError(f"Unknown vial: {vial_id}")
        self.log(f"Manual recovery routing from chamber to {destination.label}.")
        self.hardware.reacquire_from_chamber(
            vacuum_pick_delay_s=self._chamber_reacquire_hold_s(),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            chamber_position_mm=float(self.settings.chamber_position_mm),
        )
        self.hardware.drop_into_vial(
            float(destination.position_mm),
            vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s),
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
        )
        self.increment_vial(destination.vial_id)
        self.hardware.park_for_channel_capture(
            vacuum_release_settle_s=float(self.settings.vacuum_release_settle_s),
            channel_camera_position_mm=float(self.settings.channel_camera_position_mm),
        )
        self._update(selected_destination=destination.label, current_target="")
        self._set_stage(
            WorkflowStage.READY,
            "Recovered",
            next_action="Capture Channel",
            message=f"Manual recovery sent fly to {destination.label}.",
        )

    def save_settings_json(self, text: str) -> None:
        if self.snapshot().busy:
            raise OperatorFacingError("Wait for the current task to finish before saving settings.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OperatorFacingError(f"Settings JSON is invalid: {exc}") from exc
        updated = OperatorSettings.from_dict(payload)
        self._apply_settings(updated, preserve_counts=True, log_message="Operator settings saved.")


    def patch_settings_fields(self, **fields: Any) -> None:
        if self.snapshot().busy:
            raise OperatorFacingError("Wait for the current task to finish before updating settings.")
        payload = self.settings.to_dict()
        payload.update(fields)
        updated = OperatorSettings.from_dict(payload)
        self._apply_settings(updated, preserve_counts=True, log_message="Operator tuning updated.")

    def reload_settings_from_disk(self) -> None:
        if self.snapshot().busy:
            raise OperatorFacingError("Wait for the current task to finish before reloading settings.")
        settings = self.settings_store.load()
        self._apply_settings(settings, preserve_counts=True, log_message="Operator settings reloaded from disk.")


    def assay_profile_summary(self) -> Dict[str, Any]:
        return self.assay.profile_summary()

    def patch_assay_profile_fields(self, **fields: Any) -> None:
        if self.snapshot().busy:
            raise OperatorFacingError("Wait for the current task to finish before updating assay settings.")
        self.assay.patch_profile_fields(**fields)
        self.refresh_readiness()
        self.log("Assay profile settings saved.")

    def seed_box_templates(self, *, overwrite: bool = True) -> Dict[str, str]:
        if self.snapshot().busy:
            raise OperatorFacingError("Wait for the current task to finish before writing Box templates.")
        result = self.assay.seed_box_templates(overwrite=overwrite)
        self.log("Box template files refreshed.")
        return result

    def set_model_path(self, path_text: str) -> None:
        self.settings.sexing_model_path = str(path_text).strip()
        self._apply_settings(self.settings, preserve_counts=True, log_message=f"Sexing model path updated: {self.settings.sexing_model_path}")

    def set_active_profile(self, profile_name: str) -> None:
        self.settings.active_assay_profile = profile_name
        self._apply_settings(self.settings, preserve_counts=True, log_message=f"Active assay profile set to {profile_name}")
