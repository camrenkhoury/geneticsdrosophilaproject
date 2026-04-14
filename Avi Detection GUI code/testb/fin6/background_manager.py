#!/usr/bin/env python3
"""
Background capture, versioning, and restore helpers.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import cv2

from assay_profile import AssayProfile
from camera_sources import capture_background_image, open_assay_camera
from shared_utils import ensure_dir, load_json, save_json, timestamp_slug
from transform_utils import TransformSettings, apply_image_transform


class BackgroundError(RuntimeError):
    """Raised when background capture or restoration fails."""


@dataclass
class BackgroundRecord:
    schema_version: int = 1
    profile_name: str = ""
    captured_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    source: str = "camera"
    raw_path: str = ""
    transformed_path: str = ""
    transform_settings: Dict[str, Any] = field(default_factory=dict)
    transform_signature: str = ""
    image_shape_hw: list[int] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BackgroundRecord":
        payload = dict(data or {})
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            profile_name=str(payload.get("profile_name", "") or ""),
            captured_at=str(payload.get("captured_at", time.strftime("%Y-%m-%dT%H:%M:%S"))),
            source=str(payload.get("source", "camera")),
            raw_path=str(payload.get("raw_path", "") or ""),
            transformed_path=str(payload.get("transformed_path", "") or ""),
            transform_settings=dict(payload.get("transform_settings", {})),
            transform_signature=str(payload.get("transform_signature", "") or ""),
            image_shape_hw=[int(v) for v in payload.get("image_shape_hw", [])],
            notes=str(payload.get("notes", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BackgroundStore:
    def __init__(self, root_dir: str | Path, profile_name: str):
        self.root_dir = ensure_dir(root_dir)
        self.profile_name = str(profile_name)
        self.profile_slug = self._slugify(profile_name)
        self.profile_dir = ensure_dir(self.root_dir / self.profile_slug)
        self.archive_dir = ensure_dir(self.profile_dir / "archive")
        self.current_raw_path = self.profile_dir / "current_raw.png"
        self.current_transformed_path = self.profile_dir / "current_transformed.png"
        self.current_meta_path = self.profile_dir / "current_meta.json"
        self.previous_raw_path = self.profile_dir / "previous_raw.png"
        self.previous_transformed_path = self.profile_dir / "previous_transformed.png"
        self.previous_meta_path = self.profile_dir / "previous_meta.json"

    def _slugify(self, text: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(text).strip()) or "profile"

    def load_current(self) -> Optional[BackgroundRecord]:
        if not self.current_meta_path.exists():
            return None
        return BackgroundRecord.from_dict(load_json(self.current_meta_path))

    def load_previous(self) -> Optional[BackgroundRecord]:
        if not self.previous_meta_path.exists():
            return None
        return BackgroundRecord.from_dict(load_json(self.previous_meta_path))

    def _copy_if_exists(self, src: Path, dst: Path) -> None:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _rotate_current_to_previous(self) -> None:
        if not self.current_meta_path.exists():
            return
        self._copy_if_exists(self.current_raw_path, self.previous_raw_path)
        self._copy_if_exists(self.current_transformed_path, self.previous_transformed_path)
        self._copy_if_exists(self.current_meta_path, self.previous_meta_path)

    def _archive_paths(self, slug: str) -> tuple[Path, Path, Path]:
        return (
            self.archive_dir / f"{slug}_raw.png",
            self.archive_dir / f"{slug}_transformed.png",
            self.archive_dir / f"{slug}_meta.json",
        )

    def save_background(self, raw_bgr, transform: TransformSettings, *, source: str, notes: str = "") -> BackgroundRecord:
        if raw_bgr is None:
            raise BackgroundError("No background image data was provided.")
        slug = timestamp_slug()
        archive_raw_path, archive_transformed_path, archive_meta_path = self._archive_paths(slug)
        transformed_bgr = apply_image_transform(raw_bgr, transform)
        h, w = transformed_bgr.shape[:2]

        ok_raw = cv2.imwrite(str(archive_raw_path), raw_bgr)
        ok_xf = cv2.imwrite(str(archive_transformed_path), transformed_bgr)
        if not ok_raw or not ok_xf:
            raise BackgroundError("Could not save the background image files.")

        record = BackgroundRecord(
            profile_name=self.profile_name,
            source=str(source),
            raw_path=str(archive_raw_path.resolve()),
            transformed_path=str(archive_transformed_path.resolve()),
            transform_settings=transform.to_dict(),
            transform_signature=transform.signature(),
            image_shape_hw=[int(h), int(w)],
            notes=str(notes or ""),
        )
        save_json(archive_meta_path, record.to_dict())

        self._rotate_current_to_previous()
        shutil.copy2(archive_raw_path, self.current_raw_path)
        shutil.copy2(archive_transformed_path, self.current_transformed_path)
        save_json(self.current_meta_path, record.to_dict())
        return record

    def restore_previous(self) -> BackgroundRecord:
        if not self.previous_meta_path.exists() or not self.previous_transformed_path.exists():
            raise BackgroundError("There is no previous background to restore.")
        current = self.load_current()
        restored = self.load_previous()
        if restored is None:
            raise BackgroundError("The previous background metadata could not be read.")
        if current is not None:
            slug = f"restore_backup_{timestamp_slug()}"
            archive_raw_path, archive_transformed_path, archive_meta_path = self._archive_paths(slug)
            if self.current_raw_path.exists():
                shutil.copy2(self.current_raw_path, archive_raw_path)
            if self.current_transformed_path.exists():
                shutil.copy2(self.current_transformed_path, archive_transformed_path)
            save_json(archive_meta_path, current.to_dict())
        shutil.copy2(self.previous_raw_path, self.current_raw_path)
        shutil.copy2(self.previous_transformed_path, self.current_transformed_path)
        save_json(self.current_meta_path, restored.to_dict())
        return restored

    def rebuild_current_transform(self, transform: TransformSettings) -> Optional[BackgroundRecord]:
        current = self.load_current()
        if current is None or not self.current_raw_path.exists():
            return None
        raw_bgr = cv2.imread(str(self.current_raw_path), cv2.IMREAD_COLOR)
        if raw_bgr is None:
            raise BackgroundError(f"Could not read current raw background: {self.current_raw_path}")
        transformed_bgr = apply_image_transform(raw_bgr, transform)
        ok = cv2.imwrite(str(self.current_transformed_path), transformed_bgr)
        if not ok:
            raise BackgroundError(f"Could not write transformed background: {self.current_transformed_path}")
        current.transform_settings = transform.to_dict()
        current.transform_signature = transform.signature()
        current.transformed_path = str(self.current_transformed_path.resolve())
        current.image_shape_hw = [int(transformed_bgr.shape[0]), int(transformed_bgr.shape[1])]
        save_json(self.current_meta_path, current.to_dict())
        return current


def get_background_store(profile: AssayProfile, project_root: str | Path) -> BackgroundStore:
    return BackgroundStore(Path(project_root) / profile.outputs.background_root, profile.name)


def capture_profile_background(
    profile: AssayProfile,
    project_root: str | Path,
    frame_count: int = 25,
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> BackgroundRecord:
    if logger is None:
        logger = lambda _msg: None
    store = get_background_store(profile, project_root)
    logger("Opening assay camera for background capture...")
    with open_assay_camera(
        camera_backend=profile.assay_camera.backend,
        width=int(profile.assay_camera.width),
        height=int(profile.assay_camera.height),
        fps=float(profile.assay_camera.fps),
        camera_index=int(profile.assay_camera.camera_index),
        camera_device=profile.assay_camera.device,
        preferred_hint=profile.assay_camera.preferred_hint,
        role="assay",
    ) as camera:
        background_bgr = capture_background_image(camera, frame_count=frame_count, frame_sleep_s=0.03)
    record = store.save_background(background_bgr, profile.transform, source="camera")
    logger(f"Captured background: {record.transformed_path}")
    return record


def import_profile_background(
    profile: AssayProfile,
    project_root: str | Path,
    image_path: str | Path,
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> BackgroundRecord:
    if logger is None:
        logger = lambda _msg: None
    store = get_background_store(profile, project_root)
    background_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if background_bgr is None:
        raise BackgroundError(f"Could not read background image: {image_path}")
    record = store.save_background(background_bgr, profile.transform, source="file", notes=str(Path(image_path)))
    logger(f"Imported background: {record.transformed_path}")
    return record


def restore_previous_background(profile: AssayProfile, project_root: str | Path) -> BackgroundRecord:
    store = get_background_store(profile, project_root)
    return store.restore_previous()


def current_background_preview_path(profile: AssayProfile, project_root: str | Path) -> Optional[Path]:
    store = get_background_store(profile, project_root)
    if store.current_transformed_path.exists():
        return store.current_transformed_path
    return None
