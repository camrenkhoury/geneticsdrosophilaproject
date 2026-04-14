#!/usr/bin/env python3
"""
Optional Box upload support for assay outputs.

Credentials are loaded from environment variables and/or a JSON config file.
No secrets are hardcoded in the assay workflow.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shared_utils import ensure_dir, load_json, save_json


class BoxUploadError(RuntimeError):
    """Raised when Box upload cannot be completed."""


_BOX_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "client_id": "",
    "client_secret": "",
    "parent_folder_id": "",
    "tokens_file": "",
    "config_file": "",
    "folder_prefix": "fly_assay",
    "artifact_mode": "summaries",
    "upload_after_processing": False,
    "upload_after_recording": False,
    "upload_backgrounds": False,
}


@dataclass
class EffectiveBoxSettings:
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    parent_folder_id: str = ""
    tokens_file: str = ""
    config_file: str = ""
    folder_prefix: str = "fly_assay"
    artifact_mode: str = "summaries"
    upload_after_processing: bool = False
    upload_after_recording: bool = False
    upload_backgrounds: bool = False


@dataclass
class ResolvedBoxConfig:
    client_id: str
    client_secret: str
    parent_folder_id: str
    tokens_file: Path
    folder_prefix: str = "fly_assay"
    artifact_mode: str = "summaries"
    upload_after_processing: bool = False
    upload_after_recording: bool = False
    upload_backgrounds: bool = False


DEFAULT_BOX_CONFIG_TEMPLATE: Dict[str, Any] = {
    "_instructions": [
        "Replace the placeholder values below.",
        "Point the fin6 profile Box config file at this JSON file.",
        "This template enables auto-upload after processing by default.",
    ],
    "enabled": True,
    "upload_after_processing": True,
    "upload_after_recording": False,
    "upload_backgrounds": False,
    "artifact_mode": "summaries+videos",
    "folder_prefix": "fly_assay",
    "parent_folder_id": "PASTE_BOX_PARENT_FOLDER_ID_HERE",
    "client_id": "PASTE_BOX_CLIENT_ID_HERE",
    "client_secret": "PASTE_BOX_CLIENT_SECRET_HERE",
    "tokens_file": "~/.config/fin6/box_tokens.json",
}

DEFAULT_BOX_TOKENS_TEMPLATE: Dict[str, Any] = {
    "access_token": "PASTE_BOX_ACCESS_TOKEN_HERE",
    "refresh_token": "PASTE_BOX_REFRESH_TOKEN_HERE",
}

DEFAULT_BOX_ENV_TEMPLATE = """# Optional environment-variable alternative for fin6 Box uploads
export BOX_CLIENT_ID=\"PASTE_BOX_CLIENT_ID_HERE\"
export BOX_CLIENT_SECRET=\"PASTE_BOX_CLIENT_SECRET_HERE\"
export BOX_PARENT_FOLDER_ID=\"PASTE_BOX_PARENT_FOLDER_ID_HERE\"
export BOX_TOKENS_FILE=\"$HOME/.config/fin6/box_tokens.json\"
export BOX_FOLDER_PREFIX=\"fly_assay\"
"""


def _settings_dict(settings: Any) -> Dict[str, Any]:
    if settings is None:
        return {}
    if hasattr(settings, "to_dict"):
        try:
            return dict(settings.to_dict())
        except Exception:
            pass
    return dict(settings or {})


def _load_optional_config_file(path: Optional[str], *, strict: bool = True) -> Dict[str, Any]:
    if not path:
        return {}
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        if strict:
            raise BoxUploadError(f"Box config file does not exist: {cfg_path}")
        return {}
    return dict(load_json(cfg_path))


def _pick_text(base: Dict[str, Any], file_cfg: Dict[str, Any], key: str, env_name: Optional[str] = None) -> str:
    baseline = str(_BOX_DEFAULTS.get(key, "") or "")
    base_value = base.get(key, "")
    if base_value not in (None, "") and str(base_value) != baseline:
        return str(base_value)
    file_value = file_cfg.get(key, "")
    if file_value not in (None, ""):
        return str(file_value)
    if base_value not in (None, ""):
        return str(base_value)
    if env_name:
        env_value = os.environ.get(env_name, "")
        if env_value not in (None, ""):
            return str(env_value)
    return baseline


def _pick_bool(base: Dict[str, Any], file_cfg: Dict[str, Any], key: str) -> bool:
    baseline = bool(_BOX_DEFAULTS.get(key, False))
    if key in base and bool(base.get(key)) != baseline:
        return bool(base.get(key))
    if key in file_cfg:
        return bool(file_cfg.get(key))
    if key in base:
        return bool(base.get(key))
    return baseline


def resolve_effective_box_settings(settings: Any) -> EffectiveBoxSettings:
    settings_dict = _settings_dict(settings)
    config_file = str(settings_dict.get("config_file", "") or "")
    file_cfg = _load_optional_config_file(config_file, strict=False)
    return EffectiveBoxSettings(
        enabled=_pick_bool(settings_dict, file_cfg, "enabled"),
        client_id=_pick_text(settings_dict, file_cfg, "client_id", "BOX_CLIENT_ID"),
        client_secret=_pick_text(settings_dict, file_cfg, "client_secret", "BOX_CLIENT_SECRET"),
        parent_folder_id=_pick_text(settings_dict, file_cfg, "parent_folder_id", "BOX_PARENT_FOLDER_ID"),
        tokens_file=_pick_text(settings_dict, file_cfg, "tokens_file", "BOX_TOKENS_FILE"),
        config_file=config_file,
        folder_prefix=_pick_text(settings_dict, file_cfg, "folder_prefix", "BOX_FOLDER_PREFIX") or "fly_assay",
        artifact_mode=_pick_text(settings_dict, file_cfg, "artifact_mode") or "summaries",
        upload_after_processing=_pick_bool(settings_dict, file_cfg, "upload_after_processing"),
        upload_after_recording=_pick_bool(settings_dict, file_cfg, "upload_after_recording"),
        upload_backgrounds=_pick_bool(settings_dict, file_cfg, "upload_backgrounds"),
    )


def should_auto_upload(settings: Any, stage: str) -> bool:
    effective = resolve_effective_box_settings(settings)
    if not effective.enabled:
        return False
    stage_norm = str(stage or "").strip().lower()
    if stage_norm == "processing":
        return bool(effective.upload_after_processing)
    if stage_norm == "recording":
        return bool(effective.upload_after_recording)
    raise ValueError(f"Unknown Box auto-upload stage: {stage}")


def write_box_templates(target_dir: str | Path, *, overwrite: bool = False) -> Dict[str, str]:
    target = ensure_dir(Path(target_dir).expanduser())
    config_path = target / "box_config.json"
    tokens_path = target / "box_tokens.json"
    env_path = target / "box.env"

    for path in (config_path, tokens_path, env_path):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing Box template file: {path}")

    config_payload = dict(DEFAULT_BOX_CONFIG_TEMPLATE)
    config_payload["tokens_file"] = str(tokens_path)
    save_json(config_path, config_payload)
    save_json(tokens_path, DEFAULT_BOX_TOKENS_TEMPLATE)
    env_path.write_text(DEFAULT_BOX_ENV_TEMPLATE, encoding="utf-8")
    return {
        "config_file": str(config_path),
        "tokens_file": str(tokens_path),
        "env_file": str(env_path),
    }


def resolve_box_config(settings: Any) -> ResolvedBoxConfig:
    effective = resolve_effective_box_settings(settings)
    if effective.config_file:
        cfg_path = Path(effective.config_file).expanduser()
        if not cfg_path.exists():
            raise BoxUploadError(f"Box config file does not exist: {cfg_path}")

    missing = [name for name, value in {
        "client_id": effective.client_id,
        "client_secret": effective.client_secret,
        "parent_folder_id": effective.parent_folder_id,
        "tokens_file": effective.tokens_file,
    }.items() if not value]
    if missing:
        raise BoxUploadError(
            "Missing Box configuration values: " + ", ".join(missing) + ". "
            "Set them in the profile, a config file, or environment variables."
        )
    token_path = Path(effective.tokens_file).expanduser()
    if not token_path.exists():
        raise BoxUploadError(f"Box tokens file does not exist: {token_path}")
    return ResolvedBoxConfig(
        client_id=effective.client_id,
        client_secret=effective.client_secret,
        parent_folder_id=effective.parent_folder_id,
        tokens_file=token_path,
        folder_prefix=effective.folder_prefix,
        artifact_mode=effective.artifact_mode,
        upload_after_processing=effective.upload_after_processing,
        upload_after_recording=effective.upload_after_recording,
        upload_backgrounds=effective.upload_backgrounds,
    )


def _import_box_sdk():
    try:
        from box_sdk_gen import (  # type: ignore
            AccessToken,
            BoxClient,
            BoxOAuth,
            OAuthConfig,
            UploadFileAttributes,
            UploadFileAttributesParentField,
        )
    except Exception as exc:
        raise BoxUploadError(
            "Box upload requires the box-sdk-gen package. Install it and configure tokens before uploading."
        ) from exc
    return AccessToken, BoxClient, BoxOAuth, OAuthConfig, UploadFileAttributes, UploadFileAttributesParentField


def get_box_client(settings: Any):
    AccessToken, BoxClient, BoxOAuth, OAuthConfig, _UploadFileAttributes, _UploadFileAttributesParentField = _import_box_sdk()
    resolved = resolve_box_config(settings)
    tokens = load_json(resolved.tokens_file)
    access_token = str(tokens.get("access_token", "") or "")
    refresh_token = str(tokens.get("refresh_token", "") or "")
    if not access_token or not refresh_token:
        raise BoxUploadError(f"Tokens file is missing access_token or refresh_token: {resolved.tokens_file}")

    oauth = BoxOAuth(OAuthConfig(client_id=resolved.client_id, client_secret=resolved.client_secret))
    oauth.token_storage.store(AccessToken(access_token=access_token, refresh_token=refresh_token))
    original_refresh = oauth.refresh_token

    def refresh_and_save(*args, **kwargs):
        new_tokens = original_refresh(*args, **kwargs)
        save_json(
            resolved.tokens_file,
            {
                "access_token": new_tokens.access_token,
                "refresh_token": new_tokens.refresh_token,
            },
        )
        return new_tokens

    oauth.refresh_token = refresh_and_save
    client = BoxClient(oauth)
    return client, resolved


def _get_or_create_child_folder(client, parent_folder_id: str, folder_name: str) -> str:
    _AccessToken, _BoxClient, _BoxOAuth, _OAuthConfig, _UploadFileAttributes, UploadFileAttributesParentField = _import_box_sdk()
    items = client.folders.get_folder_items(parent_folder_id)
    for item in items.entries:
        if getattr(item, "type", "folder") == "folder" and item.name == folder_name:
            return str(item.id)
    folder = client.folders.create_folder(folder_name, UploadFileAttributesParentField(id=parent_folder_id))
    return str(folder.id)


def _get_or_create_folder_path(client, root_folder_id: str, relative_dir: Path, cache: Dict[str, str]) -> str:
    key = relative_dir.as_posix().strip(".") or "."
    if key in cache:
        return cache[key]
    current_id = root_folder_id
    current_parts: List[str] = []
    for part in relative_dir.parts:
        if part in ("", "."):
            continue
        current_parts.append(part)
        current_key = "/".join(current_parts)
        if current_key in cache:
            current_id = cache[current_key]
            continue
        current_id = _get_or_create_child_folder(client, current_id, part)
        cache[current_key] = current_id
    cache[key] = current_id
    return current_id


def _upload_file(client, folder_id: str, file_path: Path, *, remote_name: Optional[str] = None) -> None:
    _AccessToken, _BoxClient, _BoxOAuth, _OAuthConfig, UploadFileAttributes, UploadFileAttributesParentField = _import_box_sdk()
    with open(file_path, "rb") as handle:
        attrs = UploadFileAttributes(name=remote_name or file_path.name, parent=UploadFileAttributesParentField(id=folder_id))
        client.uploads.upload_file(attrs, handle)


def _upload_manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "processed" / "box_upload_manifest.json"


def _load_upload_manifest(run_dir: str | Path) -> Dict[str, Any]:
    path = _upload_manifest_path(run_dir)
    if not path.exists():
        return {"profile_folder_id": "", "session_folder_id": "", "uploaded_relpaths": {}}
    try:
        payload = dict(load_json(path) or {})
    except Exception:
        return {"profile_folder_id": "", "session_folder_id": "", "uploaded_relpaths": {}}
    payload.setdefault("profile_folder_id", "")
    payload.setdefault("session_folder_id", "")
    payload.setdefault("uploaded_relpaths", {})
    return payload


def _save_upload_manifest(run_dir: str | Path, manifest: Dict[str, Any]) -> Path:
    return save_json(_upload_manifest_path(run_dir), manifest)


def _is_background_artifact(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("background_") or "background_snapshot" in name or "background_meta" in name


def collect_artifacts(run_dir: str | Path, mode: str = "summaries", *, include_backgrounds: bool = False) -> List[Path]:
    run_dir = Path(run_dir)
    if not run_dir.exists():
        raise BoxUploadError(f"Run directory does not exist: {run_dir}")
    mode_norm = str(mode or "summaries").strip().lower()
    files = [path for path in run_dir.rglob("*") if path.is_file()]
    if mode_norm in {"full", "full_session", "full-folder", "full_folder"}:
        return sorted(files)

    allowed_suffixes = {".csv", ".json", ".pdf", ".png", ".sqlite", ".db"}
    if mode_norm in {"summaries+videos", "summaries_videos", "videos", "summary+videos"}:
        allowed_suffixes.update({".mp4", ".avi"})
    selected = [path for path in files if path.suffix.lower() in allowed_suffixes]
    if not include_backgrounds:
        selected = [path for path in selected if not _is_background_artifact(path)]
    return sorted(selected)


def upload_run_artifacts(
    run_dir: str | Path,
    settings: Any,
    *,
    artifact_mode: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    if logger is None:
        logger = lambda _msg: None

    effective = resolve_effective_box_settings(settings)
    client, resolved = get_box_client(settings)
    me = client.users.get_user_me()
    logger(f"Connected to Box as {me.name}")

    run_dir = Path(run_dir)
    mode = str(artifact_mode or effective.artifact_mode or "summaries")
    files = collect_artifacts(run_dir, mode=mode, include_backgrounds=bool(effective.upload_backgrounds))
    if not files:
        raise BoxUploadError(f"No uploadable artifacts were found in {run_dir}")

    manifest = _load_upload_manifest(run_dir)
    profile_folder_id = str(manifest.get("profile_folder_id", "") or "")
    session_folder_id = str(manifest.get("session_folder_id", "") or "")
    if not profile_folder_id:
        profile_folder_id = _get_or_create_child_folder(client, resolved.parent_folder_id, resolved.folder_prefix)
        manifest["profile_folder_id"] = profile_folder_id
    if not session_folder_id:
        session_folder_id = _get_or_create_child_folder(client, profile_folder_id, run_dir.name)
        manifest["session_folder_id"] = session_folder_id

    uploaded_relpaths = dict(manifest.get("uploaded_relpaths", {}) or {})
    folder_cache: Dict[str, str] = {".": session_folder_id}
    uploaded: List[str] = []
    skipped: List[str] = []

    for file_path in files:
        rel_path = file_path.relative_to(run_dir)
        rel_key = rel_path.as_posix()
        if rel_key in uploaded_relpaths:
            logger(f"Skipping already-uploaded {rel_key}")
            skipped.append(rel_key)
            continue
        remote_parent_id = _get_or_create_folder_path(client, session_folder_id, rel_path.parent, folder_cache)
        logger(f"Uploading {rel_key} ...")
        _upload_file(client, remote_parent_id, file_path, remote_name=file_path.name)
        uploaded.append(str(file_path))
        uploaded_relpaths[rel_key] = {
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "size_bytes": int(file_path.stat().st_size),
            "remote_parent": rel_path.parent.as_posix().strip(".") or ".",
        }
        manifest["uploaded_relpaths"] = uploaded_relpaths
        _save_upload_manifest(run_dir, manifest)

    return {
        "session_folder_id": session_folder_id,
        "parent_folder_id": profile_folder_id,
        "uploaded_files": uploaded,
        "skipped_files": skipped,
        "artifact_mode": mode,
        "upload_backgrounds": bool(effective.upload_backgrounds),
        "manifest_path": str(_upload_manifest_path(run_dir)),
    }
