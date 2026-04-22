from __future__ import annotations

import importlib
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from shared.config.project_paths import DETECTION_RESULT_PATH, ensure_code_directory_on_path
from shared.debug.operation_trace import append_operation_trace

CODE_DIR = ensure_code_directory_on_path()
REPO_ROOT = CODE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config

HOST_OPERATION_TRACE_FILENAME = ".host_operation_trace.log"


class OperationCancelled(Exception):
    """Raised when the operator stops the automated flow."""


class OperationalReferenceLostError(RuntimeError):
    """Raised when automated operation loses absolute reference after startup."""


@dataclass
class TubeState:
    key: str
    label: str
    role: str
    position_mm: float
    capacity: int = 10
    count: int = 0


def _noop(*_args, **_kwargs) -> None:
    return None


def _console_yes_no(title: str, message: str) -> bool:
    print(f"\n[{title}]")
    print(message)
    while True:
        answer = input("Continue? (y/n): ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _sleep_with_stop(seconds: float, stop_requested: Callable[[], bool] | None) -> None:
    end_time = time.monotonic() + seconds
    while True:
        if stop_requested is not None and stop_requested():
            raise OperationCancelled
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _estimate_move_time_for_step_delay(distance_mm: float, step_delay: float) -> float | None:
    distance = abs(float(distance_mm))
    if distance <= 0.0:
        return None
    timing_factor = float(getattr(config, "TIMING_FACTOR", 1.0) or 1.0)
    total_steps = max(1, int(round(distance / float(config.MM_PER_STEP))))
    return (2.0 * total_steps * float(step_delay)) / timing_factor


def _publish_snapshot(
    tube_states: dict[str, TubeState],
    *,
    snapshot_callback: Callable[[dict[str, Any]], None] | None,
    cycle_index: int = 0,
    detection_count: int | None = None,
    pickup_position_mm: float | None = None,
    classification_result: dict[str, Any] | None = None,
    destination_key: str | None = None,
    destination_label: str | None = None,
    stage: str | None = None,
    lost_count: int = 0,
    retry_count: int = 0,
    discard_count: int = 0,
) -> None:
    if snapshot_callback is None:
        return
    normalized_classification = (
        None if classification_result is None else _normalize_classification_result(classification_result)
    )
    snapshot_callback(
        {
            "cycle_index": int(cycle_index),
            "detection_count": None if detection_count is None else int(detection_count),
            "pickup_position_mm": pickup_position_mm,
            "stage": stage or "",
            "classification": None
            if normalized_classification is None
            else {
                "class": str(normalized_classification.get("class", "UNCERTAIN")),
                "count": int(normalized_classification.get("count", 0) or 0),
                "confidence": float(normalized_classification.get("confidence", 0.0)),
                "errors": list(normalized_classification.get("errors", []) or []),
                "image_path": normalized_classification.get("image_path"),
                "raw": dict(normalized_classification.get("raw", {}) or {}),
                "preview_key": (
                    f"{str(normalized_classification.get('class', 'UNCERTAIN')).strip().lower()}:"
                    f"{float(normalized_classification.get('confidence', 0.0) or 0.0):.8f}:"
                    f"{int(normalized_classification.get('count', 0) or 0)}:"
                    f"{'|'.join(str(error) for error in list(normalized_classification.get('errors', []) or []))}"
                ),
            },
            "destination_key": destination_key,
            "destination_label": destination_label,
            "lost_count": int(lost_count),
            "retry_count": int(retry_count),
            "discard_count": int(discard_count),
            "tube_counts": {
                key: {
                    "label": tube.label,
                    "role": tube.role,
                    "count": int(tube.count),
                    "capacity": int(tube.capacity),
                }
                for key, tube in tube_states.items()
            },
        }
    )


def _build_tube_states() -> dict[str, TubeState]:
    return {
        "T1": TubeState("T1", "Tube 1", "Damaged / Rejected", config.TUBE_1_CENTER),
        "T2": TubeState("T2", "Tube 2", "Male", config.TUBE_2_CENTER),
        "T3": TubeState("T3", "Tube 3", "Female", config.TUBE_3_CENTER),
        "T4": TubeState("T4", "Tube 4", "Male", config.TUBE_4_CENTER),
        "T5": TubeState("T5", "Tube 5", "Female", config.TUBE_5_CENTER),
    }


def _seed_tube_counts(tube_states: dict[str, TubeState], initial_tube_counts: dict[str, int] | None) -> None:
    if not initial_tube_counts:
        return
    for key, count in initial_tube_counts.items():
        tube = tube_states.get(str(key))
        if tube is None:
            continue
        try:
            normalized_count = int(count)
        except Exception:
            continue
        tube.count = max(0, min(normalized_count, int(tube.capacity)))


def _normalize_chamber_count(count: Any) -> int:
    try:
        normalized = int(count or 0)
    except Exception:
        return 0
    return max(0, normalized)


def _normalize_classification_result(classification_result: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(classification_result or {})
    normalized["count"] = _normalize_chamber_count(normalized.get("count", 0))
    return normalized


def _sorted_pickup_positions(result: dict[str, Any], clamp_operational: Callable[[float], float]) -> list[float] | str | None:
    if not result.get("fly_remaining", False):
        return "done"

    raw_positions = result.get("corrected_positions_mm")
    apply_pickup_correction = False
    if raw_positions is None or not isinstance(raw_positions, list):
        raw_positions = result.get("x_positions_mm")
        apply_pickup_correction = True
    if raw_positions is None or not isinstance(raw_positions, list):
        return None

    try:
        numeric_positions = [float(value) for value in raw_positions]
    except (TypeError, ValueError):
        return None

    if not numeric_positions:
        return "done"

    if apply_pickup_correction:
        adjusted = [
            clamp_operational(float(value) + float(getattr(config, "PICKUP_POSITION_CORRECTION_MM", 0.0)))
            for value in numeric_positions
        ]
    else:
        adjusted = [clamp_operational(float(value)) for value in numeric_positions]
    return sorted(adjusted, reverse=True)


def _load_detection_from_json_interactive(log_callback: Callable[[str], None]) -> dict[str, Any]:
    while True:
        ready = input("Channel Detection Finished (Y/N)? ").strip().upper()
        if ready == "N":
            log_callback("Waiting for channel detection to finish...")
            continue
        if ready != "Y":
            log_callback("Invalid input. Enter Y or N.")
            continue
        try:
            import json

            with open(DETECTION_RESULT_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            log_callback(f"JSON file not found: {DETECTION_RESULT_PATH}")
        except json.JSONDecodeError:
            log_callback(f"Invalid JSON format in: {DETECTION_RESULT_PATH}")


def _default_launch_assay_gui() -> Any:
    try:
        operator_bridge = importlib.import_module("host_app.operator_bridge")
        return operator_bridge.launch_fin6_gui()
    except Exception:
        script_path = REPO_ROOT / "vision" / "fin6" / "fly_tracking_gui.py"
        return subprocess.Popen([sys.executable, str(script_path)], cwd=str(script_path.parent))


def _default_detect_channel(log_callback: Callable[[str], None]) -> dict[str, Any]:
    try:
        operator_bridge = importlib.import_module("host_app.operator_bridge")
        return operator_bridge.detect_channel_once_from_saved_settings()
    except Exception as exc:
        log_callback(f"Saved channel detection failed, falling back to detection JSON: {exc}")
    result = _load_detection_from_json_interactive(log_callback)
    return {
        "result": result,
        "result_path": DETECTION_RESULT_PATH,
    }


def _resolve_destination(
    classification_result: dict[str, Any],
    tube_states: dict[str, TubeState],
) -> tuple[TubeState, str]:
    normalized_result = _normalize_classification_result(classification_result)
    class_name = str(normalized_result.get("class") or "UNCERTAIN").strip().lower()
    errors = list(normalized_result.get("errors", []) or [])
    confidence = float(normalized_result.get("confidence", 0.0) or 0.0)
    chamber_count = int(normalized_result.get("count", 0) or 0)

    if chamber_count >= 2:
        reject_tube = tube_states["T1"]
        if reject_tube.count < reject_tube.capacity:
            return reject_tube, "multiple flies in chamber"
        raise RuntimeError("No destination tube with remaining capacity is available.")

    if class_name not in {"male", "female"} or errors or confidence <= 0.0:
        reject_tube = tube_states["T1"]
        if reject_tube.count < reject_tube.capacity:
            return reject_tube, "damaged/rejected"
        raise RuntimeError("No destination tube with remaining capacity is available.")

    if class_name == "male":
        candidates = ["T2", "T4"]
        reject_reason = "male overflow"
    elif class_name == "female":
        candidates = ["T3", "T5"]
        reject_reason = "female overflow"
    else:
        candidates = []
        reject_reason = "damaged/rejected"

    for key in candidates:
        tube = tube_states[key]
        if tube.count < tube.capacity:
            return tube, tube.role

    reject_tube = tube_states["T1"]
    if reject_tube.count < reject_tube.capacity:
        return reject_tube, reject_reason

    raise RuntimeError("No destination tube with remaining capacity is available.")


def _return_grouped_flies_to_channel(
    *,
    cycle_index: int,
    move_absolute: Callable[[float], Any],
    set_vacuum: Callable[[bool], Any],
    clamp_operational: Callable[[float], float],
    status: Callable[[str, str], None],
    log: Callable[[str], None],
    stop_requested: Callable[[], bool] | None,
    chamber_release_settle_s: float,
    chamber_pickup_s: float,
) -> None:
    channel_end_position = clamp_operational(float(config.CHANNEL_LOCATION_END))
    channel_home_position = clamp_operational(float(config.CHANNEL_LOCATION_START))
    pulse_count = 5

    status("moving", f"Cycle {cycle_index}: returning to chamber for grouped-fly recovery.")
    move_absolute(float(config.CHAMBER_CENTER))
    _sleep_with_stop(chamber_release_settle_s, stop_requested)

    status("picking", f"Cycle {cycle_index}: picking grouped flies from chamber.")
    set_vacuum(True)
    _sleep_with_stop(chamber_pickup_s, stop_requested)

    status("moving", f"Cycle {cycle_index}: moving grouped flies back to channel end.")
    log(
        f"Cycle {cycle_index}: chamber count >= 3. Returning grouped flies to channel from "
        f"{channel_end_position:.2f} mm toward home with {pulse_count} vacuum pulses."
    )
    move_absolute(channel_end_position)
    _sleep_with_stop(0.4, stop_requested)
    set_vacuum(False)
    _sleep_with_stop(0.4, stop_requested)

    span = max(channel_end_position - channel_home_position, 0.0)
    for pulse_index in range(1, pulse_count + 1):
        pulse_target = clamp_operational(channel_end_position - (span * pulse_index / pulse_count))
        vacuum_enabled = bool(random.getrandbits(1))
        set_vacuum(vacuum_enabled)
        status("moving", f"Cycle {cycle_index}: channel recovery sweep {pulse_index}/{pulse_count}.")
        log(
            f"Cycle {cycle_index}: recovery pulse {pulse_index}/{pulse_count} "
            f"vacuum={'on' if vacuum_enabled else 'off'} target={pulse_target:.2f} mm."
        )
        move_absolute(pulse_target)
        _sleep_with_stop(0.25, stop_requested)

    set_vacuum(False)
    _sleep_with_stop(0.25, stop_requested)


def run_operation(
    *,
    home: Callable[[], Any] | None = None,
    move_absolute: Callable[[float], Any] | None = None,
    set_vacuum: Callable[[bool], Any] | None = None,
    get_operational_max_mm: Callable[[], float] | None = None,
    detect_channel: Callable[[], dict[str, Any]] | None = None,
    classify_fly: Callable[[], dict[str, Any]] | None = None,
    ask_yes_no: Callable[[str, str], bool] | None = None,
    launch_assay_gui: Callable[[], Any] | None = None,
    status_callback: Callable[[str, str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    snapshot_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    initial_tube_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    # This function is the single orchestration loop for the automated sorter.
    # The host GUI should only trigger it and display snapshots/logs. Hardware
    # authority, motion sequencing, channel detection, chamber classification,
    # and tube routing all flow through this routine.
    motion = None
    vacuum = None
    if home is None or move_absolute is None or set_vacuum is None or get_operational_max_mm is None:
        motion = importlib.import_module("motion")
        vacuum = importlib.import_module("vacuum")

    classify_callable = classify_fly or importlib.import_module("fly_classifier").classify_fly
    detect_callable = detect_channel or (lambda: _default_detect_channel(log))
    ask_callable = ask_yes_no or _console_yes_no
    launch_assay_callable = launch_assay_gui or _default_launch_assay_gui
    status = status_callback or _noop
    log = log_callback or print
    home_callable = home or motion.home_to_zero
    move_absolute_callable = move_absolute or motion.move_to_absolute
    set_vacuum_callable = set_vacuum or vacuum.set_enabled
    get_operational_max_mm_callable = get_operational_max_mm or motion.get_operational_max_mm

    def op_trace(event: str, **fields: Any) -> None:
        trace_fields = {
            "cycle_index": cycle_index,
            "pending_pickup_positions": list(pending_pickup_positions),
            "flies_taken_from_current_detection": flies_taken_from_current_detection,
            "first_pickup_after_detection": first_pickup_after_detection,
            "last_detection_count": last_detection_count,
            "lost_fly_count": lost_fly_count,
            "retry_pickup_count": retry_pickup_count,
            "discarded_overflow_count": discarded_overflow_count,
            "last_destination": None if last_destination is None else last_destination.key,
            "last_classification_class": None if last_classification is None else last_classification.get("class"),
            "last_classification_count": None if last_classification is None else last_classification.get("count"),
            "next_detection_cycle_kind": next_detection_cycle_kind,
            "position_reference_state": position_reference_state,
        }
        trace_fields.update(fields)
        append_operation_trace(
            HOST_OPERATION_TRACE_FILENAME,
            "final_operation",
            event,
            **trace_fields,
        )

    chamber_drop_s = 2.0
    chamber_release_settle_s = 0.25
    chamber_drop_arrival_settle_s = chamber_release_settle_s + 0.5
    chamber_settle_s = 6.0
    chamber_pickup_s = 2.0
    tube_drop_s = 2.0
    # Re-detect after every two flies from the same batch to reduce repeated
    # channel image captures while still keeping the pickup list reasonably fresh.
    max_flies_per_detection = 2

    tube_states = _build_tube_states()
    _seed_tube_counts(tube_states, initial_tube_counts)
    cycle_index = 0
    last_detection_count = 0
    last_classification: dict[str, Any] | None = None
    last_destination: TubeState | None = None
    pending_pickup_positions: list[float] = []
    flies_taken_from_current_detection = 0
    first_pickup_after_detection = False
    recent_route_history: list[str] = []
    repeated_count_signature: tuple[int, int, int] | None = None
    repeated_count_streak = 0
    lost_fly_count = 0
    retry_pickup_count = 0
    discarded_overflow_count = 0
    DETECTION_CYCLE_STARTUP = "startup"
    DETECTION_CYCLE_NORMAL_AFTER_ROUTE = "normal_after_route"
    DETECTION_CYCLE_RETRY_EMPTY = "retry_from_chamber_empty"
    DETECTION_CYCLE_RETRY_GROUPED = "retry_from_grouped_return"
    POSITION_REFERENCE_UNKNOWN = "unknown"
    POSITION_REFERENCE_HOME = "home"
    POSITION_REFERENCE_PHOTO = "photo_position"
    POSITION_REFERENCE_KNOWN_ABSOLUTE = "known_absolute"
    next_detection_cycle_kind = DETECTION_CYCLE_STARTUP
    position_reference_state = POSITION_REFERENCE_UNKNOWN
    startup_home_consumed = False

    def publish_snapshot(**kwargs: Any) -> None:
        _publish_snapshot(
            tube_states,
            snapshot_callback=snapshot_callback,
            lost_count=lost_fly_count,
            retry_count=retry_pickup_count,
            discard_count=discarded_overflow_count,
            **kwargs,
        )

    def check_stop() -> None:
        if stop_requested is not None and stop_requested():
            raise OperationCancelled

    def clamp_operational(position_mm: float) -> float:
        return max(0.0, min(float(position_mm), float(get_operational_max_mm_callable())))

    def set_position_reference(state: str) -> None:
        nonlocal position_reference_state
        position_reference_state = str(state)

    def _raise_operational_reference_loss(reason: str) -> None:
        message = (
            f"Operational reference lost during automated run ({reason}). "
            "Automatic homing is disabled after startup. Reset to safe idle and restart the run."
        )
        status("error", message)
        log(message)
        op_trace(
            "reference_lost",
            reason=reason,
            reference_state=position_reference_state,
            next_detection_cycle_kind=next_detection_cycle_kind,
            startup_home_consumed=startup_home_consumed,
        )
        raise OperationalReferenceLostError(message)

    def prepare_for_detection_cycle() -> None:
        nonlocal next_detection_cycle_kind
        set_vacuum_callable(False)
        if position_reference_state == POSITION_REFERENCE_PHOTO:
            status("running", f"Cycle {cycle_index}: retrying channel detection from photo position.")
            log(
                f"Cycle {cycle_index}: detection cycle '{next_detection_cycle_kind}' already has the nozzle at the "
                "channel photo position. Skipping redundant home."
            )
            op_trace("detect_cycle_skip_home", reason="photo_position_ready")
        elif position_reference_state == POSITION_REFERENCE_HOME:
            status("running", f"Cycle {cycle_index}: reusing existing home reference for channel detection.")
            log(
                f"Cycle {cycle_index}: detection cycle '{next_detection_cycle_kind}' already has a fresh home "
                "reference. Skipping redundant home."
            )
            op_trace("detect_cycle_skip_home", reason="home_reference_ready")
        elif position_reference_state == POSITION_REFERENCE_KNOWN_ABSOLUTE:
            status("running", f"Cycle {cycle_index}: reusing known absolute position for channel detection.")
            log(
                f"Cycle {cycle_index}: detection cycle '{next_detection_cycle_kind}' is starting from a known "
                "absolute position. Skipping redundant home and moving directly to the channel photo position."
            )
            op_trace("detect_cycle_skip_home", reason="known_absolute_position")
        else:
            op_trace("detect_cycle_home", reason="position_unknown")
            ensure_home_reference(
                "detect_cycle_position_unknown",
                f"Cycle {cycle_index}: homing gantry.",
                f"Cycle {cycle_index}: homing gantry.",
            )
        next_detection_cycle_kind = DETECTION_CYCLE_NORMAL_AFTER_ROUTE

    def ensure_home_reference(reason: str, status_message: str, log_message: str) -> None:
        nonlocal startup_home_consumed
        if position_reference_state == POSITION_REFERENCE_HOME:
            log(f"Cycle {cycle_index}: skipping redundant home ({reason}); already at home reference.")
            op_trace("home_skip", reason=reason)
            return
        if position_reference_state == POSITION_REFERENCE_KNOWN_ABSOLUTE:
            log(
                f"Cycle {cycle_index}: skipping redundant home ({reason}); current absolute position is known "
                "and the next commanded move can be reached directly."
            )
            op_trace("home_skip", reason=reason, reference_state=position_reference_state)
            return
        allow_startup_home = (
            reason == "detect_cycle_position_unknown"
            and next_detection_cycle_kind == DETECTION_CYCLE_STARTUP
            and not startup_home_consumed
        )
        if not allow_startup_home:
            _raise_operational_reference_loss(reason)
        status("running", status_message)
        log(log_message)
        op_trace("home_enter", reason=reason)
        home_callable()
        startup_home_consumed = True
        set_position_reference(POSITION_REFERENCE_HOME)
        op_trace("home_complete", reason=reason)

    def move_absolute_with_profile(position_mm: float, move_time: float | None = None) -> Any:
        if move_time is None:
            return move_absolute_callable(position_mm)
        try:
            return move_absolute_callable(position_mm, move_time=move_time)
        except TypeError:
            try:
                return move_absolute_callable(position_mm, move_time)
            except TypeError:
                return move_absolute_callable(position_mm)

    # Channel detection photo and chamber observation both currently use the
    # same explicit machine position. This is intentionally not derived from
    # chamber center so the observation point does not drift with later tuning.
    channel_photo_position_mm = 191.0
    chamber_observe_position_mm = 191.0
    camera_photo_position = clamp_operational(channel_photo_position_mm)
    chamber_observe_position = clamp_operational(chamber_observe_position_mm)
    channel_photo_step_delay = float(
        getattr(
            config,
            "CHANNEL_PHOTO_STEP_DELAY",
            getattr(config, "HOME_STEP_DELAY", getattr(config, "DEFAULT_STEP_DELAY", 0.00010)),
        )
    )
    channel_photo_move_time = _estimate_move_time_for_step_delay(camera_photo_position, channel_photo_step_delay)
    publish_snapshot(stage="idle")
    op_trace(
        "run_operation_enter",
        camera_photo_position=camera_photo_position,
        chamber_observe_position=chamber_observe_position,
        channel_photo_step_delay=channel_photo_step_delay,
        channel_photo_move_time=channel_photo_move_time,
        initial_tube_counts=initial_tube_counts,
        max_flies_per_detection=max_flies_per_detection,
        chamber_center=float(config.CHAMBER_CENTER),
    )

    try:
        while True:
            check_stop()
            cycle_index += 1
            op_trace("cycle_enter")

            if not pending_pickup_positions or flies_taken_from_current_detection >= max_flies_per_detection:
                previous_detection_count = last_detection_count
                attempted_from_previous_detection = flies_taken_from_current_detection
                op_trace(
                    "detection_cycle_enter",
                    previous_detection_count=previous_detection_count,
                    attempted_from_previous_detection=attempted_from_previous_detection,
                )

                # Each detection cycle starts from a known-safe reference state:
                # either a true startup home, an existing home reference, or the
                # already-confirmed channel photo position from a retry path.
                prepare_for_detection_cycle()

                status("moving", f"Cycle {cycle_index}: moving to channel photo position.")
                log(f"Cycle {cycle_index}: channel photo target {camera_photo_position:.2f} mm.")
                move_absolute_with_profile(camera_photo_position, move_time=channel_photo_move_time)
                set_position_reference(POSITION_REFERENCE_PHOTO)

                status("detecting", f"Cycle {cycle_index}: capturing channel detection image.")
                log(f"Cycle {cycle_index}: running channel detection.")
                detection_payload = detect_callable()
                detection_result = dict(detection_payload.get("result") or {})
                pickup_positions = _sorted_pickup_positions(detection_result, clamp_operational)
                current_detection_count = int(detection_result.get("count", 0) or 0)
                op_trace(
                    "detection_result",
                    detection_payload=detection_payload,
                    detection_result=detection_result,
                    pickup_positions=pickup_positions,
                    current_detection_count=current_detection_count,
                )

                if previous_detection_count > 0 and attempted_from_previous_detection > 0:
                    expected_remaining = max(previous_detection_count - attempted_from_previous_detection, 0)
                    if current_detection_count > expected_remaining:
                        mismatch_signature = (
                            int(previous_detection_count),
                            int(expected_remaining),
                            int(current_detection_count),
                        )
                        if mismatch_signature == repeated_count_signature:
                            repeated_count_streak += 1
                        else:
                            repeated_count_signature = mismatch_signature
                            repeated_count_streak = 1

                        log(
                            f"Cycle {cycle_index}: detection count mismatch. Expected about "
                            f"{expected_remaining} remaining after sorting {attempted_from_previous_detection} "
                            f"from a batch of {previous_detection_count}, but detected {current_detection_count}."
                        )

                        immediate_lost_fly_case = (
                            previous_detection_count == 1
                            and attempted_from_previous_detection >= 1
                            and current_detection_count == 1
                            and len(recent_route_history) >= 1
                        )

                        if immediate_lost_fly_case or repeated_count_streak >= 2:
                            missed_pickups = min(
                                max(current_detection_count - expected_remaining, 0),
                                len(recent_route_history),
                            )
                            if missed_pickups > 0:
                                reconciled_keys: list[str] = []
                                for _ in range(missed_pickups):
                                    routed_key = recent_route_history.pop()
                                    routed_tube = tube_states[routed_key]
                                    if routed_tube.count > 0:
                                        routed_tube.count -= 1
                                    reconciled_keys.append(routed_key)
                                lost_fly_count += missed_pickups
                                log(
                                    f"Cycle {cycle_index}: detected the same channel count after a completed sort. "
                                    f"Marked {missed_pickups} fly/flies as lost and reverted the recent tube count(s): "
                                    f"{', '.join(reconciled_keys)}. Lost total={lost_fly_count}."
                                )
                                publish_snapshot(
                                    cycle_index=cycle_index,
                                    detection_count=current_detection_count,
                                    classification_result=last_classification,
                                    destination_key=None if last_destination is None else last_destination.key,
                                    destination_label=None if last_destination is None else last_destination.label,
                                    stage="lost",
                                )
                                op_trace(
                                    "lost_fly_reconciliation",
                                    missed_pickups=missed_pickups,
                                    reconciled_keys=reconciled_keys,
                                    mismatch_signature=mismatch_signature,
                                    repeated_count_streak=repeated_count_streak,
                                )
                            repeated_count_signature = None
                            repeated_count_streak = 0
                    else:
                        repeated_count_signature = None
                        repeated_count_streak = 0

                last_detection_count = current_detection_count
                publish_snapshot(
                    cycle_index=cycle_index,
                    detection_count=last_detection_count,
                    stage="detecting",
                )

                # "done" means the detector explicitly reported no flies remaining.
                # That is the only normal path that ends the sorting loop.
                if pickup_positions == "done":
                    log("Detection reported no flies remaining. Sorting run is complete.")
                    op_trace("detection_done")
                    break
                if pickup_positions is None:
                    op_trace("detection_result_invalid", detection_result=detection_result)
                    raise RuntimeError("Channel detection did not return usable x_positions_mm data.")

                pending_pickup_positions = list(pickup_positions)
                flies_taken_from_current_detection = 0
                first_pickup_after_detection = True
                op_trace("pickup_batch_loaded", pending_pickup_positions=pending_pickup_positions)

            pickup_position = float(pending_pickup_positions.pop(0))
            flies_taken_from_current_detection += 1
            log(
                f"Cycle {cycle_index}: selected pickup position {pickup_position:.2f} mm "
                f"(detection batch {flies_taken_from_current_detection}/{max_flies_per_detection})."
            )
            op_trace("pickup_selected", pickup_position=pickup_position)

            if first_pickup_after_detection:
                # After a fresh channel photo, go directly to the first pickup
                # position without an extra home so we do not add avoidable drift.
                status("moving", f"Cycle {cycle_index}: moving directly from detection position to pickup.")
                log(f"Cycle {cycle_index}: skipping redundant home before first pickup after detection.")
                first_pickup_after_detection = False
            else:
                set_vacuum_callable(False)
                ensure_home_reference(
                    "pickup_accuracy_reset",
                    f"Cycle {cycle_index}: reset home before pickup.",
                    f"Cycle {cycle_index}: reset home before pickup.",
                )

            status("moving", f"Cycle {cycle_index}: moving to pickup position.")
            move_absolute_callable(pickup_position)
            set_position_reference(POSITION_REFERENCE_UNKNOWN)

            status("picking", f"Cycle {cycle_index}: picking fly.")
            set_vacuum_callable(True)
            _sleep_with_stop(2.0, stop_requested)

            # Chamber center is the true drop/pick location. Offsets are used only
            # for observation, not for the actual release or pickup position.
            status("moving", f"Cycle {cycle_index}: moving to chamber.")
            move_absolute_callable(config.CHAMBER_CENTER)
            set_position_reference(POSITION_REFERENCE_UNKNOWN)

            status("running", f"Cycle {cycle_index}: dropping in chamber.")
            _sleep_with_stop(chamber_drop_arrival_settle_s, stop_requested)
            set_vacuum_callable(False)
            _sleep_with_stop(chamber_drop_s, stop_requested)

            # Move away from chamber center so the chamber camera can observe the
            # specimen without the nozzle/cover blocking the view.
            status("moving", f"Cycle {cycle_index}: moving nozzle out of chamber view.")
            move_absolute_callable(chamber_observe_position)
            set_position_reference(POSITION_REFERENCE_PHOTO)

            status("running", f"Cycle {cycle_index}: waiting for chamber classification window.")
            _sleep_with_stop(chamber_settle_s, stop_requested)

            publish_snapshot(
                cycle_index=cycle_index,
                detection_count=last_detection_count,
                pickup_position_mm=pickup_position,
                classification_result=last_classification,
                destination_key=None if last_destination is None else last_destination.key,
                destination_label=None if last_destination is None else last_destination.label,
                stage="classifying",
            )
            status("running", f"Cycle {cycle_index}: classifying fly.")
            last_classification = _normalize_classification_result(dict(classify_callable() or {}))
            confidence = float(last_classification.get("confidence", 0.0) or 0.0)
            chamber_count = int(last_classification.get("count", 0) or 0)
            class_name = str(last_classification.get("class", "UNCERTAIN") or "UNCERTAIN").strip().lower()
            classification_errors = list(last_classification.get("errors", []) or [])
            count_detail = str(last_classification.get("count_detail", "") or "").strip()
            op_trace(
                "classification_result",
                classification_result=last_classification,
                confidence=confidence,
                chamber_count=chamber_count,
                class_name=class_name,
                classification_errors=classification_errors,
                count_detail=count_detail,
            )
            log(
                f"Cycle {cycle_index}: classification={last_classification.get('class', 'UNCERTAIN')} "
                f"confidence={confidence:.4f} count={chamber_count} errors={last_classification.get('errors', [])} "
                f"count_detail={count_detail}"
            )

            if chamber_count <= 0:
                retry_pickup_count += 1
                log(
                    f"Cycle {cycle_index}: chamber classification detected 0 flies. "
                    "Skipping chamber pickup/routing and restarting the normal channel detection cycle."
                )
                status("moving", f"Cycle {cycle_index}: chamber appears empty. Returning to channel photo position.")
                set_vacuum_callable(False)
                move_absolute_callable(chamber_observe_position)
                set_position_reference(POSITION_REFERENCE_PHOTO)
                pending_pickup_positions = []
                flies_taken_from_current_detection = max_flies_per_detection
                first_pickup_after_detection = False
                next_detection_cycle_kind = DETECTION_CYCLE_RETRY_EMPTY
                last_destination = None
                publish_snapshot(
                    cycle_index=cycle_index,
                    detection_count=last_detection_count,
                    pickup_position_mm=None,
                    classification_result=None,
                    destination_key=None,
                    destination_label="Channel Retry",
                    stage="detecting",
                )
                op_trace(
                    "channel_retry_zero_count",
                    retry_pickup_count=retry_pickup_count,
                    chamber_observe_position=chamber_observe_position,
                )
                continue

            # Tube routing is determined strictly from the classification result
            # plus current tube capacities.
            destination_tube, destination_reason = _resolve_destination(last_classification, tube_states)
            if chamber_count >= 2:
                discarded_overflow_count += 1
                op_trace(
                    "overflow_discard",
                    chamber_count=chamber_count,
                    destination_key=destination_tube.key,
                    destination_label=destination_tube.label,
                    discard_count=discarded_overflow_count,
                )
            last_destination = destination_tube
            op_trace(
                "route_selected",
                destination_key=destination_tube.key,
                destination_label=destination_tube.label,
                destination_reason=destination_reason,
            )
            publish_snapshot(
                cycle_index=cycle_index,
                detection_count=last_detection_count,
                pickup_position_mm=pickup_position,
                classification_result=last_classification,
                destination_key=destination_tube.key,
                destination_label=destination_tube.label,
                stage="classified",
            )

            status("moving", f"Cycle {cycle_index}: returning to chamber for pickup.")
            if stop_requested is not None and stop_requested():
                raise OperationCancelled
            move_absolute_callable(config.CHAMBER_CENTER)
            set_position_reference(POSITION_REFERENCE_UNKNOWN)
            _sleep_with_stop(chamber_release_settle_s, stop_requested)

            status("picking", f"Cycle {cycle_index}: picking fly from chamber.")
            if stop_requested is not None and stop_requested():
                raise OperationCancelled
            set_vacuum_callable(True)
            _sleep_with_stop(chamber_pickup_s, stop_requested)

            log(
                f"Cycle {cycle_index}: routing classification "
                f"{last_classification.get('class', 'UNCERTAIN')} to {destination_tube.label} "
                f"at {destination_tube.position_mm:.2f} mm."
            )
            status("moving", f"Cycle {cycle_index}: moving to {destination_tube.label}.")
            if stop_requested is not None and stop_requested():
                raise OperationCancelled
            move_absolute_callable(destination_tube.position_mm)
            set_position_reference(POSITION_REFERENCE_KNOWN_ABSOLUTE)
            log(
                f"Cycle {cycle_index}: arrived for drop at {destination_tube.label} target "
                f"{destination_tube.position_mm:.2f} mm."
            )

            status("running", f"Cycle {cycle_index}: dropping into {destination_tube.label}.")
            if stop_requested is not None and stop_requested():
                raise OperationCancelled
            set_vacuum_callable(False)
            _sleep_with_stop(tube_drop_s, stop_requested)
            destination_tube.count += 1
            recent_route_history.append(destination_tube.key)
            op_trace(
                "route_complete",
                destination_key=destination_tube.key,
                destination_label=destination_tube.label,
                destination_reason=destination_reason,
                tube_count=destination_tube.count,
                recent_route_history=recent_route_history,
            )

            log(
                f"Cycle {cycle_index}: routed to {destination_tube.label} "
                f"({destination_reason}). Count now {destination_tube.count}/{destination_tube.capacity}."
            )
            publish_snapshot(
                cycle_index=cycle_index,
                detection_count=last_detection_count,
                pickup_position_mm=pickup_position,
                classification_result=last_classification,
                destination_key=destination_tube.key,
                destination_label=destination_tube.label,
                stage="routed",
            )

            status("running", f"Cycle {cycle_index}: route complete. Holding current absolute position.")
            log(
                f"Cycle {cycle_index}: route complete at {destination_tube.position_mm:.2f} mm. "
                "Skipping automatic post-route home; the next step will decide whether a home reference is required."
            )
            op_trace(
                "post_route_hold_position",
                destination_key=destination_tube.key,
                destination_label=destination_tube.label,
                destination_position_mm=destination_tube.position_mm,
            )
            next_detection_cycle_kind = DETECTION_CYCLE_NORMAL_AFTER_ROUTE

        # Assay handoff occurs once, after the sorting loop genuinely ends.
        status("running", "No more flies detected. Awaiting assay confirmation.")
        should_launch_assay = ask_callable(
            "Start Assay",
            "No more flies were detected in the channel.\n\nOpen the current assay GUI now?",
        )
        op_trace("assay_prompt_result", should_launch_assay=should_launch_assay)
        if should_launch_assay:
            log("Opening assay GUI.")
            status("assaying", "Opening assay GUI.")
            launch_assay_callable()
            op_trace("assay_launch")
        else:
            log("Operator declined assay GUI launch.")
            op_trace("assay_declined")
    except OperationCancelled:
        op_trace("run_operation_cancelled")
        raise
    except Exception as exc:
        op_trace("run_operation_exception", exception_type=type(exc).__name__, exception_message=str(exc))
        raise
    finally:
        try:
            set_vacuum_callable(False)
        except Exception:
            pass
        op_trace("run_operation_finally")

    publish_snapshot(
        cycle_index=cycle_index,
        detection_count=last_detection_count,
        classification_result=last_classification,
        destination_key=None if last_destination is None else last_destination.key,
        destination_label=None if last_destination is None else last_destination.label,
        stage="complete",
    )
    status("idle", "Automated run complete.")
    op_trace(
        "run_operation_exit",
        result={
            "tube_counts": {
                key: {"count": int(tube.count), "capacity": int(tube.capacity), "role": tube.role}
                for key, tube in tube_states.items()
            },
            "lost_count": int(lost_fly_count),
            "retry_count": int(retry_pickup_count),
            "discard_count": int(discarded_overflow_count),
            "last_classification": last_classification,
            "last_destination": None if last_destination is None else last_destination.key,
        },
    )
    return {
        "tube_counts": {
            key: {"count": int(tube.count), "capacity": int(tube.capacity), "role": tube.role}
            for key, tube in tube_states.items()
        },
        "lost_count": int(lost_fly_count),
        "retry_count": int(retry_pickup_count),
        "discard_count": int(discarded_overflow_count),
        "last_classification": last_classification,
        "last_destination": None if last_destination is None else last_destination.key,
    }


def main() -> None:
    print("=== FinalOperation: automated gantry workflow ===")
    print(f"Detection JSON path: {DETECTION_RESULT_PATH}")
    try:
        run_operation()
    except OperationCancelled:
        print("Automated run stopped by operator.")


if __name__ == "__main__":
    main()
