from __future__ import annotations

import time
from typing import Any
import base64

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
        "/fin6/assay/status": "Pi-side assay status",
        "/fin6/assay/profile_summary": "Pi-side assay profile summary",
        "/fin6/assay/profiles": "Pi-side assay profiles",
        "/fin6/assay/profile/activate": "Pi-side assay profile activation",
        "/fin6/assay/profile/patch": "Pi-side assay profile update",
        "/fin6/assay/run": "Pi-side Integrated3 assay run",
        "/fin6/assay/background/capture": "Pi-side assay background capture",
        "/fin6/assay/background/import": "Pi-side assay background import",
        "/fin6/assay/background/restore": "Pi-side assay background restore",
        "/fin6/assay/background/rebuild": "Pi-side assay background rebuild",
        "/fin6/assay/preview/capture": "Pi-side assay preview capture",
        "/fin6/assay/calibration": "Pi-side assay calibration load/save",
        "/fin6/assay/calibration/test": "Pi-side assay calibration test",
        "/fin6/assay/process_last": "Pi-side assay process last",
        "/fin6/assay/process_selected": "Pi-side assay process selected",
        "/fin6/assay/process_batch": "Pi-side assay process batch",
        "/fin6/assay/upload_last": "Pi-side assay upload last",
        "/fin6/assay/box_templates": "Pi-side assay Box template write",
        "/artifacts/assay/preview/calibration": "Pi-side assay preview image",
        "/artifacts/assay/background/current": "Pi-side assay background image",
        "/artifacts/assay/run/latest/raw_video": "Pi-side assay raw video",
        "/artifacts/assay/run/latest/annotated_video": "Pi-side assay annotated video",
        "/artifacts/assay/run/latest/mask_video": "Pi-side assay mask video",
        "/artifacts/assay/run/latest/per_vial_summary_csv": "Pi-side assay per-vial summary CSV",
        "/artifacts/assay/run/latest/per_fly_summary_csv": "Pi-side assay per-fly summary CSV",
        "/artifacts/assay/run/latest/report_pdf": "Pi-side assay report PDF",
        "/artifacts/assay/run/latest/graphs_report_pdf": "Pi-side assay graph report PDF",
        "/artifacts/assay/run/latest/processing_json": "Pi-side assay processing JSON",
        "/artifacts/assay/run/latest/tube_overlay_graph": "Pi-side assay tube overlay graph",
        "/artifacts/assay/run/latest/individual_fly_graph": "Pi-side assay individual-fly graph",
        "/artifacts/assay/run/latest/per_fly_max_height_graph": "Pi-side assay per-fly max-height graph",
        "/artifacts/assay/run/latest/velocity_plot": "Pi-side assay velocity plot",
        "/detect_channel": "Pi-side channel detection",
        "/channel_setup/cameras": "Pi-side channel camera discovery",
        "/channel_setup/select_camera": "Pi-side channel camera selection",
        "/channel_setup/capture_background": "Pi-side channel setup background capture",
        "/channel_setup/capture_preview": "Pi-side channel setup preview capture",
        "/channel_setup/save_calibration": "Pi-side channel setup calibration save",
        "/camera_roles": "Pi-side camera role assignments",
        "/artifacts/classification/latest": "Pi-side classification preview image",
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

    def move_absolute(self, mm: float, move_time: float | None = None) -> ControllerPayload:
        payload = {"target_mm": float(mm)}
        if move_time is not None:
            payload["move_time"] = float(move_time)
        return self._command_request("POST", "/move_absolute", json_payload=payload)

    def move_relative(self, mm: float, move_time: float | None = None) -> ControllerPayload:
        payload = {"distance_mm": float(mm)}
        if move_time is not None:
            payload["move_time"] = float(move_time)
        return self._command_request("POST", "/move_relative", json_payload=payload)

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

    def run_integrated3_assay(self) -> ControllerPayload:
        return self._command_request("POST", "/fin6/assay/run")

    def detect_channel(self) -> ControllerPayload:
        return self._command_request("POST", "/detect_channel", timeout_s=20.0)

    def get_fin6_setup_status(self) -> ControllerPayload:
        return self._request_json_with_retries(
            "GET",
            "/fin6/setup_status",
            timeout_sequence=((2.5, 5.0), (3.5, 6.0), (5.0, 8.0)),
            retry_delays_s=(0.35, 0.75),
        )

    def launch_fin6_setup(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/launch_setup")

    def get_assay_status(self) -> ControllerPayload:
        return self._request_json("GET", "/fin6/assay/status")

    def get_assay_profile_summary(self) -> ControllerPayload:
        return self._request_json("GET", "/fin6/assay/profile_summary")

    def get_assay_profiles(self) -> ControllerPayload:
        return self._request_json("GET", "/fin6/assay/profiles")

    def activate_assay_profile(self, profile_name: str) -> ControllerPayload:
        return self._request_json(
            "POST",
            "/fin6/assay/profile/activate",
            json_payload={"profile_name": str(profile_name or "")},
        )

    def patch_assay_profile_fields(self, **fields: Any) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/profile/patch", json_payload=dict(fields))

    def seed_assay_box_templates(self, *, overwrite: bool = True) -> ControllerPayload:
        return self._request_json(
            "POST",
            "/fin6/assay/box_templates",
            json_payload={"overwrite": bool(overwrite)},
        )

    def capture_assay_background(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/background/capture", timeout_s=30.0)

    def import_assay_background(
        self,
        *,
        source_path: str | None = None,
        image_bytes: bytes | None = None,
        filename: str | None = None,
    ) -> ControllerPayload:
        payload: dict[str, Any] = {}
        if source_path:
            payload["source_path"] = str(source_path)
        if image_bytes is not None:
            payload["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
            payload["filename"] = str(filename or "assay_background.png")
        return self._request_json("POST", "/fin6/assay/background/import", json_payload=payload, timeout_s=30.0)

    def restore_previous_assay_background(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/background/restore")

    def rebuild_assay_background(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/background/rebuild")

    def capture_assay_preview(
        self,
        *,
        mode: str = "calibration",
        calibration: dict[str, Any] | None = None,
    ) -> ControllerPayload:
        payload: dict[str, Any] = {"mode": str(mode or "calibration")}
        if calibration is not None:
            payload["calibration"] = calibration
        return self._request_json("POST", "/fin6/assay/preview/capture", json_payload=payload, timeout_s=30.0)

    def get_assay_calibration(self) -> ControllerPayload:
        return self._request_json("GET", "/fin6/assay/calibration")

    def save_assay_calibration(self, calibration: dict[str, Any]) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/calibration", json_payload=calibration, timeout_s=30.0)

    def test_assay_calibration(self, calibration: dict[str, Any] | None = None) -> ControllerPayload:
        payload: dict[str, Any] = {}
        if calibration is not None:
            payload["calibration"] = calibration
        return self._request_json("POST", "/fin6/assay/calibration/test", json_payload=payload, timeout_s=30.0)

    def process_last_assay(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/process_last", timeout_s=600.0)

    def process_selected_assay(self, run_dir: str) -> ControllerPayload:
        return self._request_json(
            "POST",
            "/fin6/assay/process_selected",
            json_payload={"run_dir": str(run_dir or "")},
            timeout_s=600.0,
        )

    def batch_process_assay(self, folder: str) -> ControllerPayload:
        return self._request_json(
            "POST",
            "/fin6/assay/process_batch",
            json_payload={"folder": str(folder or "")},
            timeout_s=900.0,
        )

    def upload_last_assay(self) -> ControllerPayload:
        return self._request_json("POST", "/fin6/assay/upload_last", timeout_s=300.0)

    def get_latest_assay_manifest(self) -> ControllerPayload:
        return self._request_json("GET", "/artifacts/assay/run/latest/manifest")

    def get_channel_setup_cameras(self) -> ControllerPayload:
        return self._request_json("GET", "/channel_setup/cameras")

    def get_camera_roles(self) -> ControllerPayload:
        return self._request_json("GET", "/camera_roles")

    def save_camera_roles(
        self,
        *,
        channel_device: str,
        channel_preferred_hint: str,
        sexing_camera_index: int,
        assay_device: str,
        assay_preferred_hint: str,
    ) -> ControllerPayload:
        return self._request_json(
            "POST",
            "/camera_roles",
            json_payload={
                "channel_device": str(channel_device or ""),
                "channel_preferred_hint": str(channel_preferred_hint or ""),
                "sexing_camera_index": int(sexing_camera_index),
                "assay_device": str(assay_device or ""),
                "assay_preferred_hint": str(assay_preferred_hint or ""),
            },
        )

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
        return self._request_json("POST", "/channel_setup/capture_background", timeout_s=20.0)

    def capture_channel_setup_preview(self) -> ControllerPayload:
        return self._request_json("POST", "/channel_setup/capture_preview", timeout_s=20.0)

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
        return self._request_bytes("GET", "/artifacts/channel/background", timeout_s=15.0)

    def get_channel_setup_preview_image(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/channel/setup_preview", timeout_s=15.0)

    def get_classification_preview_image(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/classification/latest", timeout_s=15.0)

    def get_assay_preview_image(self, mode: str) -> bytes | None:
        return self._request_bytes("GET", f"/artifacts/assay/preview/{str(mode or 'calibration').strip().lower()}", timeout_s=20.0)

    def get_assay_background_image(self, which: str = "current") -> bytes | None:
        return self._request_bytes("GET", f"/artifacts/assay/background/{str(which or 'current').strip().lower()}", timeout_s=20.0)

    def get_latest_assay_raw_video(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/raw_video", timeout_s=60.0)

    def get_latest_assay_annotated_video(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/annotated_video", timeout_s=60.0)

    def get_latest_assay_mask_video(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/mask_video", timeout_s=60.0)

    def get_latest_assay_per_vial_summary_csv(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/per_vial_summary_csv", timeout_s=20.0)

    def get_latest_assay_per_fly_summary_csv(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/per_fly_summary_csv", timeout_s=20.0)

    def get_latest_assay_report_pdf(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/report_pdf", timeout_s=30.0)

    def get_latest_assay_graphs_report_pdf(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/graphs_report_pdf", timeout_s=30.0)

    def get_latest_assay_processing_json(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/processing_json", timeout_s=20.0)

    def get_latest_assay_tube_overlay_graph(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/tube_overlay_graph", timeout_s=20.0)

    def get_latest_assay_individual_fly_graph(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/individual_fly_graph", timeout_s=20.0)

    def get_latest_assay_per_fly_max_height_graph(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/per_fly_max_height_graph", timeout_s=20.0)

    def get_latest_assay_velocity_plot(self) -> bytes | None:
        return self._request_bytes("GET", "/artifacts/assay/run/latest/velocity_plot", timeout_s=20.0)

    def _command_request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> ControllerPayload:
        payload = self._request_json(method, path, json_payload=json_payload, timeout_s=timeout_s)
        if not payload.get("ok", False) or not payload.get("accepted", False):
            raise ControllerCommandRejected(str(payload.get("message", "Command rejected.")), payload)
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        timeout_s: float | tuple[float, float] | None = None,
    ) -> ControllerPayload:
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key}
        if timeout_s is None:
            request_timeout_s: float | tuple[float, float] = float(self.timeout_s)
        else:
            request_timeout_s = timeout_s

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_payload,
                timeout=request_timeout_s,
            )
        except requests.RequestException as exc:
            raise ControllerConnectionError(f"Failed to reach Pi backend at {self.base_url}: {exc}") from exc

        return self._decode_json_response(response, path)

    def _request_json_with_retries(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        timeout_sequence: tuple[float | tuple[float, float], ...] = (5.0,),
        retry_delays_s: tuple[float, ...] = (),
    ) -> ControllerPayload:
        attempt_count = max(1, len(timeout_sequence))
        last_exception: ControllerError | None = None

        for attempt_index, request_timeout in enumerate(timeout_sequence, start=1):
            normalized_timeout: float | tuple[float, float]
            if isinstance(request_timeout, tuple):
                if len(request_timeout) != 2:
                    raise ValueError("timeout_sequence entries must be floats or (connect, read) tuples.")
                normalized_timeout = (float(request_timeout[0]), float(request_timeout[1]))
            else:
                normalized_timeout = float(request_timeout)
            try:
                return self._request_json(
                    method,
                    path,
                    json_payload=json_payload,
                    timeout_s=normalized_timeout,
                )
            except ControllerConnectionError as exc:
                last_exception = exc
            except ControllerError as exc:
                last_exception = exc
                if "HTTP 5" not in str(exc):
                    raise

            if attempt_index < attempt_count:
                delay = retry_delays_s[min(attempt_index - 1, len(retry_delays_s) - 1)] if retry_delays_s else 0.0
                if delay > 0.0:
                    time.sleep(float(delay))

        if isinstance(last_exception, ControllerConnectionError):
            raise ControllerConnectionError(
                f"Pi backend was unreachable while reading {path} after {attempt_count} attempts: {last_exception}"
            ) from last_exception
        if last_exception is not None:
            raise ControllerError(
                f"Pi backend returned an error while reading {path} after {attempt_count} attempts: {last_exception}"
            ) from last_exception
        raise ControllerError(f"Failed to read {path}.")

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float | None = None,
    ) -> bytes | None:
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key}
        request_timeout_s = float(timeout_s if timeout_s is not None else self.timeout_s)

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                timeout=request_timeout_s,
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
            body_preview = response.text.strip()
            if len(body_preview) > 300:
                body_preview = body_preview[:300] + "..."
            if not body_preview:
                body_preview = "<empty body>"
            raise ControllerError(
                f"Pi backend returned non-JSON response for {path} "
                f"(HTTP {response.status_code}): {body_preview}"
            ) from exc

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
