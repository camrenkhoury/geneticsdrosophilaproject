from __future__ import annotations

from typing import Any

import requests

from host_app.controllers.base_controller import (
    BaseController,
    ControllerCommandRejected,
    ControllerConnectionError,
    ControllerError,
    ControllerPayload,
)


class RemoteController(BaseController):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float = 5.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.session = session or requests.Session()

    def home(self) -> ControllerPayload:
        return self._command_request("POST", "/home")

    def move_absolute(self, mm: float) -> ControllerPayload:
        return self._command_request("POST", "/move_absolute", json_payload={"target_mm": float(mm)})

    def move_relative(self, mm: float) -> ControllerPayload:
        return self._command_request("POST", "/move_relative", json_payload={"distance_mm": float(mm)})

    def set_vacuum(self, enabled: bool) -> ControllerPayload:
        return self._command_request("POST", "/vacuum", json_payload={"enabled": bool(enabled)})

    def set_vibration(self, enabled: bool) -> ControllerPayload:
        return self._command_request("POST", "/vibration", json_payload={"enabled": bool(enabled)})

    def stop(self) -> ControllerPayload:
        return self._command_request("POST", "/stop")

    def classify_fly(self) -> ControllerPayload:
        return self._command_request("POST", "/classify")

    def run_assay(self) -> ControllerPayload:
        return self._command_request("POST", "/run_assay")

    def get_status(self) -> ControllerPayload:
        return self._request_json("GET", "/status")

    def get_health(self) -> ControllerPayload:
        return self._request_json("GET", "/health")

    def _command_request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
    ) -> ControllerPayload:
        payload = self._request_json(method, path, json_payload=json_payload)
        if not payload.get("ok", False) or not payload.get("accepted", False):
            raise ControllerCommandRejected(str(payload.get("message", "Command rejected.")), payload)
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
    ) -> ControllerPayload:
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key}

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_payload,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise ControllerConnectionError(f"Failed to reach Pi backend at {self.base_url}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ControllerError(f"Pi backend returned non-JSON response for {path}.") from exc

        if response.status_code == 401:
            raise ControllerConnectionError(str(payload.get("detail", "Remote authentication failed.")))

        if response.status_code >= 400:
            detail = payload.get("detail") or payload.get("message") or response.text
            raise ControllerError(f"Pi backend returned HTTP {response.status_code}: {detail}")

        return payload
