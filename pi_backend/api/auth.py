from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from shared.state.state_enums import BackendLifecycleState, ClientControllerState


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    context = request.app.state.backend_context
    runtime_config = context.runtime_config
    expected_api_key = runtime_config.expected_api_key

    if not expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backend API key is not configured via {runtime_config.api_key_env_var}.",
        )

    if x_api_key != expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )

    context.runtime_state.set_controller_state(
        ClientControllerState.CLIENT_CONNECTED,
        "Remote client connected.",
    )
    if context.runtime_state.snapshot().backend_lifecycle_state == BackendLifecycleState.WAITING_FOR_CLIENT:
        context.runtime_state.set_backend_lifecycle_state(
            BackendLifecycleState.BACKEND_READY,
            "Backend ready for remote control.",
        )
