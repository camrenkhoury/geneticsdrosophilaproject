from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


ControllerPayload = dict[str, Any]


class ControllerError(RuntimeError):
    """Base exception for controller-layer failures."""


class ControllerConnectionError(ControllerError):
    """Raised when the host cannot reach the remote backend."""


class ControllerCommandRejected(ControllerError):
    def __init__(self, message: str, payload: ControllerPayload | None = None):
        self.payload = payload or {}
        super().__init__(message)


class BaseController(ABC):
    @abstractmethod
    def home(self) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def move_absolute(self, mm: float) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def move_relative(self, mm: float) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def set_vacuum(self, enabled: bool) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def set_vibration(self, enabled: bool) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def get_status(self) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def get_health(self) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def classify_fly(self) -> ControllerPayload:
        raise NotImplementedError

    @abstractmethod
    def run_assay(self) -> ControllerPayload:
        raise NotImplementedError

    def run_automated(self) -> ControllerPayload:
        raise NotImplementedError("Automated endpoint is not wired in this phase.")
