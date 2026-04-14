#!/usr/bin/env python3
"""
Optional Box upload support for assay outputs.

Credentials are loaded from environment variables and/or a JSON config file.
No secrets are hardcoded in the assay workflow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from shared_utils import load_json, save_json


class BoxUploadError(RuntimeError):
    """Raised when Box upload cannot be completed."""


@dataclass
class ResolvedBoxConfig:
    client_id: str
    client_secret: str
    parent_folder_id: str
    tokens_file: Path
    folder_prefix: str = "fly_assay"


def _load_optional_config_file(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        raise BoxUploadError(f"Box config file does not exist: {cfg_path}")
    return dict(load_json(cfg_path))


def resolve_box_config(settings: Any) -> ResolvedBoxConfig:
    settings_dict = settings.to_dict() if hasattr(settings, "to_dict") else dict(settings or {})
    file_cfg = _load_optional_config_file(settings_dict.get("config_file"))

    def pick(key: str, env_name: str, default: str = "") -> str:
        value = settings_dict.get(key)
        if value:
            return str(value)
        value = file_cfg.get(key)
        if value:
            return str(value)
        return str(os.environ.get(env_name, default) or default)

    client_id = pick("client_id", "BOX_CLIENT_ID")
    client_secret = pick("client_secret", "BOX_CLIENT_SECRET")
    parent_folder_id = pick("parent_folder_id", "BOX_PARENT_FOLDER_ID")
    tokens_file = pick("tokens_file", "BOX_TOKENS_FILE")
    folder_prefix = pick("folder_prefix", "BOX_FOLDER_PREFIX", default=settings_dict.get("folder_prefix", "fly_assay") or "fly_assay")

    missing = [name for name, value in {
        "client_id": client_id,
        "client_secret": client_secret,
        "parent_folder_id": parent_folder_id,
        "tokens_file": tokens_file,
    }.items() if not value]
    if missing:
        raise BoxUploadError(
            "Missing Box configuration values: " + ", ".join(missing) + ". "
            "Set them in the profile, a config file, or environment variables."
        )
    token_path = Path(tokens_file).expanduser()
    if not token_path.exists():
        raise BoxUploadError(f"Box tokens file does not exist: {token_path}")
    return ResolvedBoxConfig(
        client_id=client_id,
        client_secret=client_secret,
        parent_folder_id=parent_folder_id,
        tokens_file=token_path,
        folder_prefix=folder_prefix,
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
        save_json(resolved.tokens_file, {
            "access_token": new_tokens.access_token,
            "refresh_token": new_tokens.refresh_token,
        })
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


def _upload_file(client, folder_id: str, file_path: Path) -> None:
    _AccessToken, _BoxClient, _BoxOAuth, _OAuthConfig, UploadFileAttributes, UploadFileAttributesParentField = _import_box_sdk()
    with open(file_path, "rb") as handle:
        attrs = UploadFileAttributes(name=file_path.name, parent=UploadFileAttributesParentField(id=folder_id))
        client.uploads.upload_file(attrs, handle)


def collect_artifacts(run_dir: str | Path, mode: str = "summaries") -> List[Path]:
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
    return sorted(path for path in files if path.suffix.lower() in allowed_suffixes)


def upload_run_artifacts(
    run_dir: str | Path,
    settings: Any,
    *,
    artifact_mode: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    if logger is None:
        logger = lambda _msg: None
    client, resolved = get_box_client(settings)
    me = client.users.get_user_me()
    logger(f"Connected to Box as {me.name}")

    run_dir = Path(run_dir)
    profile_folder_id = _get_or_create_child_folder(client, resolved.parent_folder_id, resolved.folder_prefix)
    session_folder_id = _get_or_create_child_folder(client, profile_folder_id, run_dir.name)
    mode = artifact_mode or getattr(settings, "artifact_mode", None) or getattr(settings, "upload_artifacts", None) or "summaries"
    files = collect_artifacts(run_dir, mode=mode)
    if not files:
        raise BoxUploadError(f"No uploadable artifacts were found in {run_dir}")

    uploaded: List[str] = []
    for file_path in files:
        logger(f"Uploading {file_path.name} ...")
        _upload_file(client, session_folder_id, file_path)
        uploaded.append(str(file_path))

    return {
        "session_folder_id": session_folder_id,
        "parent_folder_id": profile_folder_id,
        "uploaded_files": uploaded,
        "artifact_mode": str(mode),
    }
