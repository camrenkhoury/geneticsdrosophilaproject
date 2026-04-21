from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from pi_backend.api.auth import require_api_key
from pi_backend.api.models import (
    CommandResponse,
    HealthResponse,
    MoveAbsoluteRequest,
    MoveRelativeRequest,
    StatusResponse,
    VacuumRequest,
    VibrationRequest,
)


router = APIRouter()


def _status_etag(status_revision: int) -> str:
    return f'W/"status-{status_revision}"'


def _reject_busy(context, action_description: str) -> dict[str, Any] | None:
    if not context.is_busy():
        return None
    return {
        "ok": False,
        "message": f"Machine is busy running {context._active_command}. Wait for it to finish before {action_description}.",
    }


@router.get("/health", response_model=HealthResponse, dependencies=[Depends(require_api_key)])
def get_health(request: Request) -> HealthResponse:
    context = request.app.state.backend_context
    snapshot = context.runtime_state.snapshot()
    return context.build_health_response(snapshot)


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_api_key)])
def get_status(request: Request) -> Response:
    context = request.app.state.backend_context
    context.machine_service.refresh_detection_summary()
    snapshot = context.runtime_state.snapshot()
    etag = _status_etag(snapshot.status_revision)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})

    payload = StatusResponse.from_snapshot(snapshot)
    return JSONResponse(
        content=jsonable_encoder(payload),
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


@router.get("/artifacts/channel/annotated", dependencies=[Depends(require_api_key)])
def get_channel_annotated_preview(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    preview_path = context.machine_service.get_channel_annotated_preview_path()
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Channel annotated preview is not available yet.")
    return FileResponse(preview_path, media_type="image/png", filename=preview_path.name)


@router.get("/artifacts/channel/background", dependencies=[Depends(require_api_key)])
def get_channel_background_preview(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    preview_path = context.machine_service.get_channel_background_preview_path()
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Channel background preview is not available yet.")
    return FileResponse(preview_path, media_type="image/png", filename=preview_path.name)


@router.get("/artifacts/channel/setup_preview", dependencies=[Depends(require_api_key)])
def get_channel_setup_preview(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    preview_path = context.machine_service.get_channel_setup_preview_path()
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Channel setup preview is not available yet.")
    return FileResponse(preview_path, media_type="image/jpeg", filename=preview_path.name)


@router.get("/artifacts/classification/latest", dependencies=[Depends(require_api_key)])
def get_classification_preview(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    preview_path = context.machine_service.get_latest_classification_preview_path()
    if preview_path is None or not preview_path.exists():
        raise HTTPException(status_code=404, detail="Classification preview is not available yet.")
    return FileResponse(preview_path, media_type="image/jpeg", filename=preview_path.name)


@router.get("/fin6/setup_status", dependencies=[Depends(require_api_key)])
def get_fin6_setup_status(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.get_fin6_setup_status()


@router.post("/fin6/launch_setup", dependencies=[Depends(require_api_key)])
def post_fin6_launch_setup(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.launch_fin6_setup()


@router.get("/fin6/assay/status", dependencies=[Depends(require_api_key)])
def get_fin6_assay_status(request: Request) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.get_assay_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/fin6/assay/profile_summary", dependencies=[Depends(require_api_key)])
def get_fin6_assay_profile_summary(request: Request) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.get_assay_profile_summary()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/fin6/assay/profiles", dependencies=[Depends(require_api_key)])
def get_fin6_assay_profiles(request: Request) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.list_assay_profiles()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/run", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_fin6_assay_run(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "integrated3_assay",
        context.machine_service.run_integrated3_assay,
        precheck=context.machine_service.validate_assay_command,
    )


@router.post("/fin6/assay/profile/activate", dependencies=[Depends(require_api_key)])
def post_fin6_assay_profile_activate(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    try:
        profile_name = str(payload.get("profile_name", "") or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required.")
        return context.machine_service.activate_assay_profile(profile_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/profile/patch", dependencies=[Depends(require_api_key)])
def post_fin6_assay_profile_patch(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.patch_assay_profile_fields(**dict(payload or {}))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/box_templates", dependencies=[Depends(require_api_key)])
def post_fin6_assay_box_templates(request: Request, payload: dict[str, Any] | None = None) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "writing assay Box templates")
    if busy_response is not None:
        return busy_response
    try:
        overwrite = True if payload is None else bool(payload.get("overwrite", True))
        return context.machine_service.seed_assay_box_templates(overwrite=overwrite)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/background/capture", dependencies=[Depends(require_api_key)])
def post_fin6_assay_background_capture(request: Request) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "capturing an assay background")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.capture_assay_background()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/background/import", dependencies=[Depends(require_api_key)])
def post_fin6_assay_background_import(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "importing an assay background")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.import_assay_background(
            source_path=None if payload.get("source_path") is None else str(payload.get("source_path")),
            image_base64=None if payload.get("image_base64") is None else str(payload.get("image_base64")),
            filename=None if payload.get("filename") is None else str(payload.get("filename")),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/background/restore", dependencies=[Depends(require_api_key)])
def post_fin6_assay_background_restore(request: Request) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "restoring the previous assay background")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.restore_previous_assay_background()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/background/rebuild", dependencies=[Depends(require_api_key)])
def post_fin6_assay_background_rebuild(request: Request) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "rebuilding the assay background transform")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.rebuild_assay_background_transform()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/preview/capture", dependencies=[Depends(require_api_key)])
def post_fin6_assay_preview_capture(request: Request, payload: dict[str, Any] | None = None) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "capturing an assay preview")
    if busy_response is not None:
        return busy_response
    try:
        body = dict(payload or {})
        mode = str(body.get("mode", "calibration") or "calibration")
        calibration_override = body.get("calibration")
        if calibration_override is not None and not isinstance(calibration_override, dict):
            raise ValueError("calibration must be a JSON object when provided.")
        return context.machine_service.capture_assay_preview(
            mode=mode,
            calibration_override=calibration_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/fin6/assay/calibration", dependencies=[Depends(require_api_key)])
def get_fin6_assay_calibration(request: Request) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.load_assay_calibration()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/calibration", dependencies=[Depends(require_api_key)])
def post_fin6_assay_calibration(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "saving assay calibration")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.save_assay_calibration(dict(payload or {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/calibration/test", dependencies=[Depends(require_api_key)])
def post_fin6_assay_calibration_test(request: Request, payload: dict[str, Any] | None = None) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "testing assay calibration")
    if busy_response is not None:
        return busy_response
    try:
        body = None
        if payload is not None and payload:
            calibration_payload = payload.get("calibration")
            if calibration_payload is not None and not isinstance(calibration_payload, dict):
                raise ValueError("calibration must be a JSON object when provided.")
            body = calibration_payload if calibration_payload is not None else dict(payload)
        return context.machine_service.test_assay_calibration(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/process_last", dependencies=[Depends(require_api_key)])
def post_fin6_assay_process_last(request: Request) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "processing the latest assay run")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.process_last_assay()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/process_selected", dependencies=[Depends(require_api_key)])
def post_fin6_assay_process_selected(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "processing the selected assay run")
    if busy_response is not None:
        return busy_response
    try:
        run_dir = str(payload.get("run_dir", "") or "").strip()
        if not run_dir:
            raise ValueError("run_dir is required.")
        return context.machine_service.process_selected_assay_run(run_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/process_batch", dependencies=[Depends(require_api_key)])
def post_fin6_assay_process_batch(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "batch-processing assay runs")
    if busy_response is not None:
        return busy_response
    try:
        folder = str(payload.get("folder", "") or "").strip()
        if not folder:
            raise ValueError("folder is required.")
        return context.machine_service.batch_process_assay_runs(folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fin6/assay/upload_last", dependencies=[Depends(require_api_key)])
def post_fin6_assay_upload_last(request: Request) -> dict:
    context = request.app.state.backend_context
    busy_response = _reject_busy(context, "uploading the latest assay run")
    if busy_response is not None:
        return busy_response
    try:
        return context.machine_service.upload_last_assay()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/artifacts/assay/preview/{mode}", dependencies=[Depends(require_api_key)])
def get_assay_preview_artifact(request: Request, mode: str) -> FileResponse:
    context = request.app.state.backend_context
    try:
        preview_path = context.machine_service.get_assay_preview_artifact_path(mode)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(preview_path, media_type="image/png", filename=preview_path.name)


@router.get("/artifacts/assay/background/{which}", dependencies=[Depends(require_api_key)])
def get_assay_background_artifact(request: Request, which: str) -> FileResponse:
    context = request.app.state.backend_context
    try:
        background_path = context.machine_service.get_assay_background_artifact_path(which)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(background_path, media_type="image/png", filename=background_path.name)


@router.get("/artifacts/assay/run/latest/manifest", dependencies=[Depends(require_api_key)])
def get_assay_run_manifest(request: Request) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.get_latest_assay_run_manifest()
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/artifacts/assay/run/latest/raw_video", dependencies=[Depends(require_api_key)])
def get_assay_raw_video(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("raw_video")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/artifacts/assay/run/latest/annotated_video", dependencies=[Depends(require_api_key)])
def get_assay_annotated_video(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("annotated_video")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/artifacts/assay/run/latest/mask_video", dependencies=[Depends(require_api_key)])
def get_assay_mask_video(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("mask_video")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/artifacts/assay/run/latest/per_vial_summary_csv", dependencies=[Depends(require_api_key)])
def get_assay_per_vial_summary_csv(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("per_vial_summary_csv")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="text/csv", filename=path.name)


@router.get("/artifacts/assay/run/latest/per_fly_summary_csv", dependencies=[Depends(require_api_key)])
def get_assay_per_fly_summary_csv(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("per_fly_summary_csv")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="text/csv", filename=path.name)


@router.get("/artifacts/assay/run/latest/report_pdf", dependencies=[Depends(require_api_key)])
def get_assay_report_pdf(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("report_pdf")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@router.get("/artifacts/assay/run/latest/processing_json", dependencies=[Depends(require_api_key)])
def get_assay_processing_json(request: Request) -> FileResponse:
    context = request.app.state.backend_context
    try:
        path = context.machine_service.get_latest_assay_artifact_path("processing_json")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get("/channel_setup/cameras", dependencies=[Depends(require_api_key)])
def get_channel_setup_cameras(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.machine_service.list_channel_setup_cameras()


@router.get("/camera_roles", dependencies=[Depends(require_api_key)])
def get_camera_roles(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.machine_service.list_camera_roles()


@router.post("/camera_roles", dependencies=[Depends(require_api_key)])
def post_camera_roles(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    try:
        return context.machine_service.save_camera_roles(
            channel_device=str(payload.get("channel_device", "") or ""),
            channel_preferred_hint=str(payload.get("channel_preferred_hint", "") or ""),
            sexing_camera_index=int(payload.get("sexing_camera_index", 0)),
            assay_device=str(payload.get("assay_device", "") or ""),
            assay_preferred_hint=str(payload.get("assay_preferred_hint", "") or ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/channel_setup/select_camera", dependencies=[Depends(require_api_key)])
def post_channel_setup_select_camera(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    try:
        device_reference = str(payload.get("device_reference", "") or "")
        preferred_hint = str(payload.get("preferred_hint", "") or "")
        return context.machine_service.update_channel_setup_camera(device_reference, preferred_hint=preferred_hint)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/channel_setup/capture_background", dependencies=[Depends(require_api_key)])
def post_channel_setup_capture_background(request: Request) -> dict:
    context = request.app.state.backend_context
    if context.is_busy():
        return {
            "ok": False,
            "message": f"Machine is busy running {context._active_command}. Wait for it to finish before capturing a setup background.",
        }
    try:
        return context.machine_service.capture_channel_setup_background()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/channel_setup/capture_preview", dependencies=[Depends(require_api_key)])
def post_channel_setup_capture_preview(request: Request) -> dict:
    context = request.app.state.backend_context
    if context.is_busy():
        return {
            "ok": False,
            "message": f"Machine is busy running {context._active_command}. Wait for it to finish before capturing a setup preview.",
        }
    try:
        return context.machine_service.capture_channel_setup_preview()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/channel_setup/save_calibration", dependencies=[Depends(require_api_key)])
def post_channel_setup_save_calibration(request: Request, payload: dict[str, Any]) -> dict:
    context = request.app.state.backend_context
    if context.is_busy():
        return {
            "ok": False,
            "message": f"Machine is busy running {context._active_command}. Wait for it to finish before saving setup calibration.",
        }
    try:
        left_raw = payload.get("left_point_px")
        right_raw = payload.get("right_point_px")
        if not isinstance(left_raw, (list, tuple)) or len(left_raw) != 2:
            raise ValueError("left_point_px must contain two pixel coordinates.")
        if not isinstance(right_raw, (list, tuple)) or len(right_raw) != 2:
            raise ValueError("right_point_px must contain two pixel coordinates.")
        channel_mm_raw = payload.get("channel_mm")
        channel_mm = float(channel_mm_raw) if channel_mm_raw is not None else None
        return context.machine_service.save_channel_setup_calibration(
            (int(left_raw[0]), int(left_raw[1])),
            (int(right_raw[0]), int(right_raw[1])),
            channel_mm=channel_mm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/home", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_home(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "home",
        context.machine_service.home,
        precheck=context.machine_service.validate_motion_command,
    )


@router.post("/move_absolute", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_move_absolute(request: Request, payload: MoveAbsoluteRequest) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "move_absolute",
        lambda: context.machine_service.move_absolute(payload.target_mm, payload.move_time),
        precheck=context.machine_service.validate_motion_command,
    )


@router.post("/move_relative", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_move_relative(request: Request, payload: MoveRelativeRequest) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "move_relative",
        lambda: context.machine_service.move_relative(payload.distance_mm, payload.move_time),
        precheck=context.machine_service.validate_motion_command,
    )


@router.post("/vacuum", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_vacuum(request: Request, payload: VacuumRequest) -> CommandResponse:
    context = request.app.state.backend_context
    return context.apply_actuator_command(
        "vacuum",
        lambda: context.machine_service.set_vacuum(payload.enabled),
        precheck=context.machine_service.validate_vacuum_command,
    )


@router.post("/vibration", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_vibration(request: Request, payload: VibrationRequest) -> CommandResponse:
    context = request.app.state.backend_context
    return context.apply_actuator_command(
        "vibration",
        lambda: context.machine_service.set_vibration(payload.enabled),
        precheck=context.machine_service.validate_vibration_command,
    )


@router.post("/stop", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_stop(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.request_stop()


@router.post("/classify", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_classify(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "classify",
        context.machine_service.classify_fly,
        precheck=context.machine_service.validate_classifier_command,
    )


@router.post("/detect_channel", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_detect_channel(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "detect_channel",
        context.machine_service.detect_channel,
        precheck=context.machine_service.validate_detect_channel_command,
    )


@router.post("/run_assay", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_run_assay(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "run_assay",
        context.machine_service.run_assay,
        precheck=context.machine_service.validate_assay_command,
    )
