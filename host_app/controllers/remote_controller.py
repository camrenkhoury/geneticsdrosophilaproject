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
    _FEATURE_ROUTE_HINTS: dict[str, str] = {
        "/fin6/setup_status": "Pi-side fin6 setup status",
        "/fin6/launch_setup": "Pi-side fin6 setup launch",
        "/detect_channel": "Pi-side channel detection",
        "/channel_setup/cameras": "Pi-side channel camera discovery",
        "/channel_setup/select_camera": "Pi-side channel camera selection",
        "/channel_setup/capture_background": "Pi-side channel setup background capture",
        "/channel_setup/capture_preview": "Pi-side channel setup preview capture",
        "/channel_setup/save_calibration": "Pi-side channel setup calibration save",
    }

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
        self._status_etag: str | None = None

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

    def detect_channel(self) -> ControllerPayload:
        return self._command_request("POST", "/detect_channel")

    def get_fin6_setup_status(self) -> ControllerPayload:
        return self._request_json("GET", "/fin6/setup_status")

    def launch_fin6_setup(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/launch_setup")

    def get_channel_setup_cameras(self) -> ControllerPayload:
        return self._request_json("GET", "/channel_setup/cameras")

    def select_channel_setup_camera(self, device_reference: str, *, preferred_hint: str = "") -> ControllerPayload:
        return self._request_json(
            "POST",
            "/channel_setup/select_camera",
            json_payload={
                "device_reference": str(device_reference or ""),
                "preferred_hint": str(preferred_hint or ""),
            },
        )

    def capture_channel_setup_background(self) -> ControllerPayload:
        return self._request_json("POST", "/channel_setup/capture_background")

    def capture_channel_setup_preview(self) -> ControllerPayload:
        return self._request_json("POST", "/channel_setup/capture_preview")

    def save_channel_setup_calibration(
        self,
        left_point_px: tuple[int, int],
        right_point_px: tuple[int, int],
        *,
        channel_mm: float,
    ) -> ControllerPayload:
        return self._request_json(
            "POST",
            "/channel_setup/save_calibration",
            json_payload={
                "left_point_px": [int(left_point_px[0]), int(left_point_px[1])],
                "right_point_px": [int(right_point_px[0]), int(right_point_px[1])],
                "channel_mm": float(channel_mm),
            },
        )

    def get_status(self) -> ControllerPayload | None:
        url = f"{self.base_url}/status"
        headers = {"X-API-Key": self.api_key}
        if self._status_etag:
            headers["If-None-Match"] = self._status_etag

        try:
            response = self.session.request(
                method="GET",
                url=url,
                headers=headers,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise ControllerConnectionError(f"Failed to reach Pi backend at {self.base_url}: {exc}") from exc

        if response.status_code == 304:
            return None

        payload = self._decode_json_response(response, "/status")
        etag = response.headers.get("ETag")
        if etag:
            self._status_etag = etag
        return payload

    def get_status_fresh(self) -> ControllerPayload:
        return self._request_json("GET", "/status")

    def get_health(self) -> ControllerPayload:
        return self._request_json("GET", "/health")

    def get_channel_preview_image(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/channel/annotated")

    def get_channel_background_image(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/channel/background")

    def get_channel_setup_preview_image(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/channel/setup_preview")

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

        return self._decode_json_response(response, path)

    def _request_bytes(
        self,
        method: str,
        path: str,
    ) -> bytes | None:
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key}

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise ControllerConnectionError(f"Failed to reach Pi backend at {self.base_url}: {exc}") from exc

        if response.status_code == 401:
            try:
                payload = response.json()
                detail = str(payload.get("detail", "Remote authentication failed."))
            except ValueError:
                detail = "Remote authentication failed."
            raise ControllerConnectionError(detail)

        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
                detail = str(payload.get("detail") or payload.get("message") or detail)
            except ValueError:
                pass
            raise ControllerError(f"Pi backend returned HTTP {response.status_code}: {detail}")

        return response.content

    def _decode_json_response(self, response: requests.Response, path: str) -> ControllerPayload:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ControllerError(f"Pi backend returned non-JSON response for {path}.") from exc

        if response.status_code == 401:
            self._status_etag = None
            raise ControllerConnectionError(str(payload.get("detail", "Remote authentication failed.")))

        if response.status_code == 404 and path in self._FEATURE_ROUTE_HINTS:
            feature_name = self._FEATURE_ROUTE_HINTS[path]
            raise ControllerError(
                f"The connected Pi backend does not expose {feature_name} ({path}). "
                "The host GUI is newer than the running backend. "
                "Update/pull the repo copy the Pi service actually uses and restart the Pi backend service."
            )

        if response.status_code >= 400:
            detail = payload.get("detail") or payload.get("message") or response.text
            raise ControllerError(f"Pi backend returned HTTP {response.status_code}: {detail}")

        return payload
