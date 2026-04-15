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


@router.get("/fin6/setup_status", dependencies=[Depends(require_api_key)])
def get_fin6_setup_status(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.get_fin6_setup_status()


@router.post("/fin6/launch_setup", dependencies=[Depends(require_api_key)])
def post_fin6_launch_setup(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.launch_fin6_setup()


@router.get("/channel_setup/cameras", dependencies=[Depends(require_api_key)])
def get_channel_setup_cameras(request: Request) -> dict:
    context = request.app.state.backend_context
    return context.machine_service.list_channel_setup_cameras()


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
