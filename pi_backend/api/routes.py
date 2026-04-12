from __future__ import annotations

from fastapi import APIRouter, Depends, Request

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


@router.get("/health", response_model=HealthResponse, dependencies=[Depends(require_api_key)])
def get_health(request: Request) -> HealthResponse:
    context = request.app.state.backend_context
    snapshot = context.runtime_state.snapshot()
    return context.build_health_response(snapshot)


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_api_key)])
def get_status(request: Request) -> StatusResponse:
    context = request.app.state.backend_context
    context.machine_service.refresh_detection_summary()
    snapshot = context.runtime_state.snapshot()
    return StatusResponse.from_snapshot(snapshot)


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


@router.post("/run_assay", response_model=CommandResponse, dependencies=[Depends(require_api_key)])
def post_run_assay(request: Request) -> CommandResponse:
    context = request.app.state.backend_context
    return context.submit_machine_task(
        "run_assay",
        context.machine_service.run_assay,
        precheck=context.machine_service.validate_assay_command,
    )
