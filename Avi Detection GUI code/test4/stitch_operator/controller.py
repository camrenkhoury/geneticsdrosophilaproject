from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .settings import OperatorSettings, OperatorSettingsStore, VialDefinition
from .state import AssayRunState, ChannelState, OperatorState, ReadinessState, SexingState, VialState, WorkflowStage
from .services.assay import AssayService, BackgroundError, ProcessingError, RecordingError
from .services.channel import ChannelError, ChannelService
from .services.hardware import Destination, HardwareError, HardwareService
from .services.sexing import SexingService


class OperatorFacingError(RuntimeError):
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

    def _set_stage(self, stage: WorkflowStage, label: str, *, next_action: Optional[str] = None, message: Optional[str] = None) -> None:
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
            pdf_path=str(payload.get("pdf_path", payload.get("summary_pdf", self.state.assay.pdf_path)) or self.state.assay.pdf_path),
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
            except OperatorFacingError as exc:
                self.log(f"Operator error: {exc}")
                self._set_stage(WorkflowStage.ERROR, "Attention needed", next_action="Review status", message=str(exc))
                self._update(brief_error=str(exc), error_detail=str(exc))
            except (ChannelError, HardwareError, BackgroundError, ProcessingError, RecordingError) as exc:
                detail = traceback.format_exc()
                self.log(f"Workflow error: {exc}")
                self._set_stage(WorkflowStage.ERROR, "Attention needed", next_action="Review status", message=str(exc))
                self._update(brief_error=str(exc), error_detail=detail)
            except Exception as exc:
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
        self.hardware.stop()
        self.log("Stop requested.")

    def _check_cancelled(self) -> None:
        if self._task_stop_event.is_set():
            raise OperatorFacingError("Operation cancelled.")

    # ------------------------------------------------------------------
    # confirmation bridge
    # ------------------------------------------------------------------
    def request_choice(self, *, title: str, message: str, options: List[str]) -> Optional[str]:
        request_id = uuid.uuid4().hex
        response_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1)
        self._pending_requests[request_id] = response_queue
        self.events.put({"type": "choice", "request_id": request_id, "title": title, "message": message, "options": options})
        timeout_s = max(1.0, float(self.settings.confirmation_timeout_s))
        try:
            return response_queue.get(timeout=timeout_s)
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
        target = str(sex).lower()
        with self._lock:
            return [item for item in self.state.vials if item.target_sex == target and item.current_count < item.max_count]

    def choose_destination_for_sex(self, sex: str) -> VialState:
        candidates = self._available_vials_for_sex(sex)
        if not candidates:
            raise OperatorFacingError(
                f"No available {sex} vial. Adjust vial capacities or use Debug > Manual recovery."
            )
        candidates.sort(key=lambda item: (item.current_count, item.label))
        return candidates[0]

    def increment_vial(self, vial_id: str) -> None:
        with self._lock:
            for vial in self.state.vials:
                if vial.vial_id == vial_id:
                    vial.current_count += 1
                    vial.last_routed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    vial.status = "READY" if vial.current_count < vial.max_count else "FULL"
                    break
            self.state.updated_at = time.time()

    def reset_vial_counts(self) -> None:
        with self._lock:
            for vial in self.state.vials:
                vial.current_count = 0
                vial.status = "READY"
                vial.last_routed_at = ""
            self.state.updated_at = time.time()
        self.log("Vial counts reset.")

    # ------------------------------------------------------------------
    # public workflow actions
    # ------------------------------------------------------------------
    def start_initialize(self) -> bool:
        def task():
            self.log("Initializing gantry and outputs.")
            self.hardware.reset_outputs()
            self.hardware.home()
            self.refresh_readiness()
            self._set_stage(WorkflowStage.READY, "Ready", next_action="Capture Channel", message="System initialized and homed.")
            self.log("System initialized.")

        return self._begin_task("initialize", WorkflowStage.INITIALIZING, "Initializing", "Initializing system...", task)

    def start_capture_channel(self) -> bool:
        def task():
            if not self.hardware.homed:
                self.log("System not homed; homing before channel capture.")
                self.hardware.home()
            self.hardware.vacuum_off()
            self.hardware.move_to_channel_camera()
            self.log("Capturing channel frame.")
            result = self.channel.capture_channel()
            result["result_json_path"] = str(self.channel.result_json_path.resolve())
            self._set_channel_state(result, stale=False)
            if result.get("fly_remaining"):
                self._set_stage(WorkflowStage.CHANNEL, "Channel captured", next_action="Route Next Fly", message=f"{int(result.get('count', 0))} flies detected.")
            else:
                self._set_stage(WorkflowStage.CHANNEL, "Channel clear", next_action="Run Assay", message="No flies remain in the channel.")
            self.log(f"Channel capture complete: {result.get('count', 0)} flies detected.")

        return self._begin_task("capture_channel", WorkflowStage.CHANNEL, "Capturing channel", "Capturing channel image...", task)

    def start_route_next_fly(self) -> bool:
        def task():
            self._check_cancelled()
            channel_result = self.channel.load_last_result()
            if channel_result is None:
                raise OperatorFacingError("Capture the channel before routing the next fly.")
            if channel_result.get("fly_remaining") is not True:
                self._set_channel_state(channel_result, stale=False)
                raise OperatorFacingError("No flies remain in the channel. Run the assay or capture again.")
            self._set_channel_state(channel_result, stale=False)

            pickup_position = self.hardware.pickup_position_from_channel(
                channel_result.get("x_positions_mm", []),
                pickup_offset_mm=float(self.settings.pickup_offset_mm),
            )
            self.log(f"Routing next fly from {pickup_position:.2f} mm.")
            self._update(current_target=f"Pickup {pickup_position:.1f} mm")
            self.hardware.move_to_pickup(pickup_position, vacuum_pick_delay_s=float(self.settings.vacuum_pick_delay_s))
            self._check_cancelled()

            self._update(current_target="Sexing chamber")
            self.hardware.drop_in_chamber_and_clear(vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s))
            time.sleep(max(0.0, float(self.settings.classification_delay_s)))
            self._check_cancelled()

            sex_result = self.sexing.classify()
            self._set_sexing_state(sex_result)
            self.log(f"Sexing result: {sex_result['label']} ({sex_result['confidence']:.2f}).")

            chosen_sex = str(sex_result.get("label", "UNCERTAIN")).lower()
            if chosen_sex not in {"male", "female"}:
                choice = self.request_choice(
                    title="Manual sex routing",
                    message=(
                        "The sexing model was missing or uncertain. The fly is waiting in the chamber. "
                        "Choose a destination sex for routing."
                    ),
                    options=["Male", "Female"],
                )
                if choice is None:
                    raise OperatorFacingError(
                        "Manual route required. The fly remains in the chamber. Use Debug > Manual recovery to continue."
                    )
                chosen_sex = str(choice).strip().lower()
                self.log(f"Operator selected manual route: {chosen_sex}.")

            destination = self.choose_destination_for_sex(chosen_sex)
            self._update(selected_destination=destination.label, current_target=destination.label)
            self.hardware.reacquire_from_chamber(vacuum_pick_delay_s=float(self.settings.vacuum_pick_delay_s))
            self._check_cancelled()
            self.hardware.drop_into_vial(float(destination.position_mm), vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s))
            self.increment_vial(destination.vial_id)
            self.log(f"Fly routed to {destination.label} ({destination.target_sex}).")
            self.hardware.home()

            updated_channel = self.channel.load_last_result() or channel_result
            count = max(0, int(updated_channel.get("count", 0)) - 1)
            updated_channel["count"] = count
            updated_channel["fly_remaining"] = count > 0
            self._set_channel_state(updated_channel, stale=True)
            self._set_stage(
                WorkflowStage.READY,
                "Fly routed",
                next_action="Capture Channel",
                message=f"Fly routed to {destination.label}. Capture the channel again for the next specimen.",
            )

        return self._begin_task("route_next_fly", WorkflowStage.ROUTING, "Routing fly", "Routing next fly...", task)

    def start_capture_channel_background(self) -> bool:
        def task():
            self.log("Capturing channel background.")
            self.channel.capture_background()
            self.refresh_readiness()
            self._set_stage(WorkflowStage.CHANNEL, "Channel ready", next_action="Calibrate Channel", message="Channel background captured.")
            self.log("Channel background saved.")

        return self._begin_task("channel_background", WorkflowStage.CHANNEL, "Channel setup", "Capturing channel background...", task)

    def start_calibrate_channel(self) -> bool:
        def task():
            self.log("Launching channel calibration.")
            self.channel.calibrate()
            self.refresh_readiness()
            self._set_stage(WorkflowStage.CHANNEL, "Channel calibrated", next_action="Capture Channel", message="Channel calibration saved.")
            self.log("Channel calibration saved.")

        return self._begin_task("channel_calibrate", WorkflowStage.CHANNEL, "Channel calibration", "Calibrating channel...", task)

    def start_capture_assay_background(self) -> bool:
        def task():
            self.log("Capturing assay background.")
            self.assay.capture_background(logger=self.log)
            self.refresh_readiness()
            self._set_stage(WorkflowStage.ASSAY, "Assay ready", next_action="Calibrate Assay", message="Assay background captured.")

        return self._begin_task("assay_background", WorkflowStage.ASSAY, "Assay setup", "Capturing assay background...", task)

    def start_restore_assay_background(self) -> bool:
        def task():
            self.log("Restoring previous assay background.")
            self.assay.restore_previous_background()
            self.refresh_readiness()
            self._set_stage(WorkflowStage.ASSAY, "Assay ready", next_action="Capture Preview", message="Previous assay background restored.")

        return self._begin_task("assay_restore_background", WorkflowStage.ASSAY, "Assay setup", "Restoring previous background...", task)

    def start_calibrate_assay(self) -> bool:
        def task():
            self.log("Launching assay calibration.")
            path = self.assay.calibrate()
            self.refresh_readiness()
            self._set_stage(WorkflowStage.ASSAY, "Assay calibrated", next_action="Run Assay", message=f"Assay calibration saved: {path}")
            self.log(f"Assay calibration saved: {path}")

        return self._begin_task("assay_calibrate", WorkflowStage.ASSAY, "Assay calibration", "Calibrating assay...", task)

    def start_capture_assay_preview(self) -> bool:
        def task():
            preview = self.assay.capture_preview()
            self._set_assay_state({"preview_path": preview.get("preview_path", "")})
            self._set_stage(WorkflowStage.ASSAY, "Assay preview", next_action="Run Assay", message="Assay preview updated.")
            self.log("Assay preview updated.")

        return self._begin_task("assay_preview", WorkflowStage.ASSAY, "Assay preview", "Capturing assay preview...", task)

    def start_run_assay(self) -> bool:
        def task():
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

            manifest = self.assay.run_assay(stop_event=self._task_stop_event, logger=self.log, preview_callback=preview_callback)
            self._set_assay_state({
                "run_dir": manifest.get("run_dir", ""),
                "duration_s": manifest.get("duration_s", 0.0),
                "preview_path": str(self.assay.live_preview_path.resolve()) if self.assay.live_preview_path.exists() else "",
            })
            self._set_stage(WorkflowStage.ASSAY, "Assay recorded", next_action="Process Last Assay", message="Assay recording complete.")
            self.log(f"Assay recorded: {manifest.get('run_dir', '')}")

        return self._begin_task("run_assay", WorkflowStage.ASSAY, "Recording assay", "Recording assay...", task)

    def start_process_last_assay(self) -> bool:
        def task():
            self.log("Processing last assay.")

            def progress_callback(payload: Dict[str, Any]) -> None:
                stage = str(payload.get("stage", "processing"))
                self._update(status_message=f"Processing assay: {stage}")

            result = self.assay.process_last(logger=self.log, progress_callback=progress_callback)
            self._set_assay_state(result)
            self._set_stage(WorkflowStage.RESULTS, "Results ready", next_action="Upload Last Run", message="Assay processing complete.")
            self.log("Assay processing complete.")

        return self._begin_task("process_assay", WorkflowStage.PROCESSING, "Processing assay", "Processing assay...", task)

    def start_upload_last_run(self) -> bool:
        def task():
            self.log("Uploading last assay artifacts.")
            result = self.assay.upload_last(logger=self.log)
            folder_id = result.get("folder_id") or result.get("upload_folder_id") or ""
            status = "Uploaded" if folder_id else "Upload complete"
            self._set_assay_state({"upload_status": status})
            self._set_stage(WorkflowStage.RESULTS, "Uploaded", next_action="Capture Channel", message="Last assay artifacts uploaded.")
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
        self.hardware.reacquire_from_chamber(vacuum_pick_delay_s=float(self.settings.vacuum_pick_delay_s))
        self.hardware.drop_into_vial(float(destination.position_mm), vacuum_drop_delay_s=float(self.settings.vacuum_drop_delay_s))
        self.increment_vial(destination.vial_id)
        self.hardware.home()
        self._set_stage(WorkflowStage.READY, "Recovered", next_action="Capture Channel", message=f"Manual recovery sent fly to {destination.label}.")

    def set_model_path(self, path_text: str) -> None:
        self.settings.sexing_model_path = str(path_text).strip()
        self.settings_store.save(self.settings)
        self.sexing = SexingService(self.settings)
        self.refresh_readiness()
        self.log(f"Sexing model path updated: {self.settings.sexing_model_path}")

    def set_active_profile(self, profile_name: str) -> None:
        self.assay.load_profile(profile_name)
        self.settings.active_assay_profile = profile_name
        self.settings_store.save(self.settings)
        self.refresh_readiness()
        self.log(f"Active assay profile set to {profile_name}")
