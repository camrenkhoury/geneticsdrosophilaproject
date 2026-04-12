from __future__ import annotations

import queue
import threading

from host_app.controllers.base_controller import ControllerConnectionError, ControllerError
from host_app.controllers.remote_controller import RemoteController
from host_app.sync.connection_state import ConnectionState


class RemoteSyncManager:
    def __init__(
        self,
        controller: RemoteController,
        ui_queue: queue.Queue,
        *,
        poll_interval_s: float | None = None,
        idle_poll_interval_s: float | None = None,
        active_poll_interval_s: float | None = None,
    ):
        self.controller = controller
        self.ui_queue = ui_queue
        base_idle_interval = idle_poll_interval_s if idle_poll_interval_s is not None else poll_interval_s
        if base_idle_interval is None:
            base_idle_interval = 1.5
        self.idle_poll_interval_s = max(0.25, float(base_idle_interval))
        if active_poll_interval_s is None:
            active_poll_interval_s = min(0.4, self.idle_poll_interval_s)
        self.active_poll_interval_s = max(0.25, min(float(active_poll_interval_s), self.idle_poll_interval_s))
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_connection_state: ConnectionState | None = None
        self._last_message: str = ""
        self._has_connected_once = False
        self._reconnect_pending = False
        self._last_status_revision: int | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.request_immediate_poll()
            return

        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._run, name="remote-status-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._last_connection_state = None
        self._last_message = ""
        self._has_connected_once = False
        self._reconnect_pending = False
        self._last_status_revision = None

    def request_immediate_poll(self) -> None:
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._last_connection_state != ConnectionState.CLIENT_CONNECTED:
                connect_state = (
                    ConnectionState.CONNECTING_TO_PI
                    if not self._has_connected_once
                    else ConnectionState.RECONNECT_ATTEMPT
                )
                self._emit_connection(connect_state, "Connecting to Pi backend.")

            try:
                status_payload = self.controller.get_status()
            except ControllerConnectionError as exc:
                if self._has_connected_once:
                    self._reconnect_pending = True
                self._last_status_revision = None
                self._emit_connection(ConnectionState.CLIENT_DISCONNECTED, str(exc))
                if self._wait_for_retry():
                    return
                continue
            except ControllerError as exc:
                if self._has_connected_once:
                    self._reconnect_pending = True
                self._last_status_revision = None
                self._emit_connection(ConnectionState.CLIENT_DISCONNECTED, str(exc))
                if self._wait_for_retry():
                    return
                continue

            if self._reconnect_pending:
                self._emit_connection(ConnectionState.CLIENT_RECONNECTED, "Reconnected to Pi backend.")
                self._reconnect_pending = False
            self._has_connected_once = True
            self._emit_connection(ConnectionState.CLIENT_CONNECTED, "Connected to Pi backend.")
            if self._should_publish_status(status_payload):
                self.ui_queue.put(("remote_status", status_payload))

            if self._wait_for_next_poll(self._poll_interval_for_status(status_payload)):
                return

    def _wait_for_retry(self) -> bool:
        self._emit_connection(
            ConnectionState.RETRY_WAIT,
            f"Retrying connection in {self.idle_poll_interval_s:.1f} seconds.",
        )
        return self._wait_for_next_poll(self.idle_poll_interval_s)

    def _wait_for_next_poll(self, interval_s: float) -> bool:
        self._wake_event.clear()
        self._wake_event.wait(interval_s)
        return self._stop_event.is_set()

    def _poll_interval_for_status(self, status_payload: dict) -> float:
        return self.active_poll_interval_s if self._status_is_active(status_payload) else self.idle_poll_interval_s

    def _status_is_active(self, status_payload: dict) -> bool:
        if status_payload.get("current_task"):
            return True

        busy_tokens = ("RUNNING", "REQUESTED", "STARTING", "VALIDATING", "APPLYING", "STOP")
        task_state = str(status_payload.get("task_state") or "").upper()
        orchestrator_state = str(status_payload.get("orchestrator_state") or "").upper()
        return any(token in task_state for token in busy_tokens) or any(
            token in orchestrator_state for token in busy_tokens
        )

    def _should_publish_status(self, status_payload: dict) -> bool:
        revision = status_payload.get("status_revision")
        if revision is None:
            return True
        if revision != self._last_status_revision:
            self._last_status_revision = int(revision)
            return True
        return False

    def _emit_connection(self, state: ConnectionState, message: str) -> None:
        if state == self._last_connection_state and message == self._last_message:
            return

        self._last_connection_state = state
        self._last_message = message
        self.ui_queue.put(("remote_connection", state.value, message))
