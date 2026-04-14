#!/usr/bin/env python3
"""
Optional Box upload support for assay outputs.

Box credentials can come from:
- the assay profile
- a Box JSON config file
- environment variables
- legacy repo-level Box settings already used by capture.py / box_login.py

The legacy fallback keeps fin6 aligned with the existing image-upload workflow so
the operator does not need to re-enter the same Box credentials in every cloned
test directory.
"""

from __future__ import annotations

import ast
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shared_utils import ensure_dir, load_json, save_json


DEFAULT_BOX_DIR = Path('/home/team8/z.avi_assay_tests/assayfinal')
DEFAULT_BOX_CONFIG_PATH = str(DEFAULT_BOX_DIR / 'box_config.json')
DEFAULT_BOX_TOKENS_PATH = str(DEFAULT_BOX_DIR / 'box_tokens.json')


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
        "The default assayfinal Box tokens path is used here to match the working Pi setup.",
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
    "tokens_file": DEFAULT_BOX_TOKENS_PATH,
}

DEFAULT_BOX_TOKENS_TEMPLATE: Dict[str, Any] = {
    "access_token": "PASTE_BOX_ACCESS_TOKEN_HERE",
    "refresh_token": "PASTE_BOX_REFRESH_TOKEN_HERE",
}

DEFAULT_BOX_ENV_TEMPLATE = """# Optional environment-variable alternative for fin6 Box uploads
export BOX_CLIENT_ID=\"PASTE_BOX_CLIENT_ID_HERE\"
export BOX_CLIENT_SECRET=\"PASTE_BOX_CLIENT_SECRET_HERE\"
export BOX_PARENT_FOLDER_ID=\"PASTE_BOX_PARENT_FOLDER_ID_HERE\"
export BOX_TOKENS_FILE=\"/home/team8/z.avi_assay_tests/assayfinal/box_tokens.json\"
export BOX_FOLDER_PREFIX=\"fly_assay\"
"""


_PLACEHOLDER_SNIPPETS = (
    "paste_",
    "your_",
    "example",
    "replace_me",
)


def _repo_root_for_legacy(repo_root: str | Path | None = None) -> Path:
    if repo_root:
        return Path(repo_root).expanduser().resolve()

    current = Path(__file__).resolve().parent
    markers = ('box_login.py', 'capture.py', 'box_tokens.json', 'box_config.json')
    if any((current / name).exists() for name in markers):
        return current

    parent = current.parent.resolve()
    if any((parent / name).exists() for name in markers):
        return parent

    return current


def _parse_python_constants(path: Path, names: set[str]) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception:
        return {}
    values: Dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in names:
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except Exception:
                continue
    return values


def _resolve_legacy_token_path(raw_value: str, *, relative_to: Path) -> Path:
    candidate = Path(os.path.expandvars(str(raw_value))).expanduser()
    if not candidate.is_absolute():
        candidate = (relative_to / candidate).resolve()
    return candidate


def discover_legacy_box_settings(repo_root: str | Path | None = None) -> Dict[str, Any]:
    """Discover repo-level Box credentials already used by capture.py / box_login.py."""
    root = _repo_root_for_legacy(repo_root)
    capture_path = root / "capture.py"
    login_path = root / "box_login.py"
    names = {"CLIENT_ID", "CLIENT_SECRET", "BOX_PARENT_FOLDER_ID", "BOX_FOLDER_NAME", "TOKENS_FILE"}

    capture_values = _parse_python_constants(capture_path, names)
    login_values = _parse_python_constants(login_path, names)

    raw_values: Dict[str, Any] = {}
    for source in (capture_values, login_values):
        for key, value in source.items():
            if key not in raw_values and value not in (None, ""):
                raw_values[key] = value

    token_candidates: List[Path] = []
    repo_tokens = root / "box_tokens.json"
    if repo_tokens.exists():
        token_candidates.append(repo_tokens)
    for source_path, values in ((capture_path, capture_values), (login_path, login_values)):
        raw_token_path = values.get("TOKENS_FILE")
        if isinstance(raw_token_path, str) and raw_token_path.strip():
            token_candidates.append(_resolve_legacy_token_path(raw_token_path, relative_to=source_path.parent))

    legacy_tokens_path = next((path for path in token_candidates if path.exists()), None)
    if legacy_tokens_path is None and token_candidates:
        legacy_tokens_path = token_candidates[0]

    result: Dict[str, Any] = {}
    if raw_values.get("CLIENT_ID"):
        result["client_id"] = str(raw_values["CLIENT_ID"])
    if raw_values.get("CLIENT_SECRET"):
        result["client_secret"] = str(raw_values["CLIENT_SECRET"])
    if raw_values.get("BOX_PARENT_FOLDER_ID"):
        result["parent_folder_id"] = str(raw_values["BOX_PARENT_FOLDER_ID"])
    if raw_values.get("BOX_FOLDER_NAME"):
        result["legacy_folder_name"] = str(raw_values["BOX_FOLDER_NAME"])
    if legacy_tokens_path is not None:
        result["tokens_file"] = str(legacy_tokens_path)

    source_files = [path.name for path in (capture_path, login_path) if path.exists()]
    if source_files:
        result["legacy_source"] = ", ".join(source_files)
    if result:
        result["repo_root"] = str(root)
    return result


def _load_legacy_tokens(repo_root: str | Path | None = None) -> Dict[str, Any]:
    legacy = discover_legacy_box_settings(repo_root)
    token_path_text = str(legacy.get("tokens_file", "") or "")
    if not token_path_text:
        return {}
    token_path = Path(token_path_text).expanduser()
    if not token_path.exists():
        return {}
    try:
        payload = dict(load_json(token_path) or {})
    except Exception:
        return {}
    access_token = str(payload.get("access_token", "") or "")
    refresh_token = str(payload.get("refresh_token", "") or "")
    if not access_token or not refresh_token:
        return {}
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


def _is_placeholder_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if any(snippet in lowered for snippet in _PLACEHOLDER_SNIPPETS):
        return True
    if lowered.startswith("paste ") or lowered.startswith("your "):
        return True
    return False


def _token_payload_is_usable(payload: Dict[str, Any]) -> bool:
    access_token = str(payload.get("access_token", "") or "").strip()
    refresh_token = str(payload.get("refresh_token", "") or "").strip()
    if not access_token or not refresh_token:
        return False
    if _is_placeholder_text(access_token) or _is_placeholder_text(refresh_token):
        return False
    return True


def _token_file_is_usable(path: str | Path | None) -> bool:
    if not path:
        return False
    token_path = Path(str(path)).expanduser()
    if not token_path.exists():
        return False
    try:
        payload = dict(load_json(token_path) or {})
    except Exception:
        return False
    return _token_payload_is_usable(payload)


def _build_box_env_template(
    *,
    client_id: str,
    client_secret: str,
    parent_folder_id: str,
    tokens_file: str,
    folder_prefix: str,
) -> str:
    return (
        "# Optional environment-variable alternative for fin6 Box uploads\n"
        f'export BOX_CLIENT_ID="{client_id}"\n'
        f'export BOX_CLIENT_SECRET="{client_secret}"\n'
        f'export BOX_PARENT_FOLDER_ID="{parent_folder_id}"\n'
        f'export BOX_TOKENS_FILE="{tokens_file}"\n'
        f'export BOX_FOLDER_PREFIX="{folder_prefix}"\n'
    )


def _replace_if_placeholder(base_value: str, replacement: str) -> str:
    return replacement if _is_placeholder_text(base_value) and replacement else base_value


def _write_tokens_file(target_path: Path, payload: Dict[str, Any], *, source_path: Optional[Path] = None) -> str:
    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()
    if source_path and source_path.exists() and source_path.resolve() != target_path.resolve():
        try:
            target_path.symlink_to(source_path)
            return "symlink"
        except Exception:
            pass
    save_json(target_path, payload)
    return "copied" if source_path and source_path.exists() else "written"


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


def _pick_text(
    base: Dict[str, Any],
    file_cfg: Dict[str, Any],
    key: str,
    env_name: Optional[str] = None,
    *,
    legacy_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    baseline = str(_BOX_DEFAULTS.get(key, "") or "")
    legacy_value = "" if legacy_cfg is None else str(legacy_cfg.get(key, "") or "")

    def _candidate_is_usable(candidate: str) -> bool:
        if candidate in (None, ""):
            return False
        if key != "tokens_file" and _is_placeholder_text(candidate):
            return False
        if key == "tokens_file":
            if _is_placeholder_text(candidate):
                return False
            if _token_file_is_usable(candidate):
                return True
            if legacy_value and _token_file_is_usable(legacy_value):
                return False
            try:
                return Path(candidate).expanduser().exists()
            except Exception:
                return False
        return True

    base_value = str(base.get(key, "") or "")
    if base_value != baseline and _candidate_is_usable(base_value):
        return str(base_value)
    file_value = str(file_cfg.get(key, "") or "")
    if _candidate_is_usable(file_value):
        return str(file_value)
    if _candidate_is_usable(base_value):
        return str(base_value)
    if env_name:
        env_value = os.environ.get(env_name, "")
        if _candidate_is_usable(env_value):
            return str(env_value)
    if _candidate_is_usable(legacy_value):
        return str(legacy_value)
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


def resolve_effective_box_settings(settings: Any, *, legacy_repo_root: str | Path | None = None) -> EffectiveBoxSettings:
    settings_dict = _settings_dict(settings)
    config_file = str(settings_dict.get("config_file", "") or "")
    file_cfg = _load_optional_config_file(config_file, strict=False)
    legacy_cfg = discover_legacy_box_settings(legacy_repo_root)
    return EffectiveBoxSettings(
        enabled=_pick_bool(settings_dict, file_cfg, "enabled"),
        client_id=_pick_text(settings_dict, file_cfg, "client_id", "BOX_CLIENT_ID", legacy_cfg=legacy_cfg),
        client_secret=_pick_text(settings_dict, file_cfg, "client_secret", "BOX_CLIENT_SECRET", legacy_cfg=legacy_cfg),
        parent_folder_id=_pick_text(settings_dict, file_cfg, "parent_folder_id", "BOX_PARENT_FOLDER_ID", legacy_cfg=legacy_cfg),
        tokens_file=_pick_text(settings_dict, file_cfg, "tokens_file", "BOX_TOKENS_FILE", legacy_cfg=legacy_cfg),
        config_file=config_file,
        folder_prefix=_pick_text(settings_dict, file_cfg, "folder_prefix", "BOX_FOLDER_PREFIX", legacy_cfg=legacy_cfg) or "fly_assay",
        artifact_mode=_pick_text(settings_dict, file_cfg, "artifact_mode") or "summaries",
        upload_after_processing=_pick_bool(settings_dict, file_cfg, "upload_after_processing"),
        upload_after_recording=_pick_bool(settings_dict, file_cfg, "upload_after_recording"),
        upload_backgrounds=_pick_bool(settings_dict, file_cfg, "upload_backgrounds"),
    )


def should_auto_upload(settings: Any, stage: str, *, legacy_repo_root: str | Path | None = None) -> bool:
    effective = resolve_effective_box_settings(settings, legacy_repo_root=legacy_repo_root)
    if not effective.enabled:
        return False
    stage_norm = str(stage or "").strip().lower()
    if stage_norm == "processing":
        return bool(effective.upload_after_processing)
    if stage_norm == "recording":
        return bool(effective.upload_after_recording)
    raise ValueError(f"Unknown Box auto-upload stage: {stage}")


def write_box_templates(
    target_dir: str | Path,
    *,
    overwrite: bool = False,
    legacy_repo_root: str | Path | None = None,
) -> Dict[str, str]:
    target = ensure_dir(Path(target_dir).expanduser())
    config_path = target / "box_config.json"
    tokens_path = target / "box_tokens.json"
    env_path = target / "box.env"

    for path in (config_path, tokens_path, env_path):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing Box template file: {path}")

    config_payload = dict(DEFAULT_BOX_CONFIG_TEMPLATE)
    legacy_cfg = discover_legacy_box_settings(legacy_repo_root)
    legacy_tokens = _load_legacy_tokens(legacy_repo_root)
    legacy_source_tokens = Path(str(legacy_cfg.get("tokens_file", "") or "")).expanduser() if legacy_cfg.get("tokens_file") else None

    seeded_from = str(legacy_cfg.get("legacy_source", "") or "")
    if legacy_cfg:
        config_payload["_instructions"] = [
            f"Auto-seeded from the repo's existing Box uploader settings ({seeded_from or 'legacy Box config'}).",
            "fin6 shares the same Box app credentials used for captured fly-image uploads.",
            "Uploads after processing are enabled by default; change any values here if you need a different Box destination.",
        ]
        config_payload["client_id"] = _replace_if_placeholder(config_payload["client_id"], str(legacy_cfg.get("client_id", "") or ""))
        config_payload["client_secret"] = _replace_if_placeholder(config_payload["client_secret"], str(legacy_cfg.get("client_secret", "") or ""))
        config_payload["parent_folder_id"] = _replace_if_placeholder(config_payload["parent_folder_id"], str(legacy_cfg.get("parent_folder_id", "") or ""))
    tokens_write_mode = _write_tokens_file(tokens_path, legacy_tokens or DEFAULT_BOX_TOKENS_TEMPLATE, source_path=legacy_source_tokens)
    config_payload["tokens_file"] = str(tokens_path)
    save_json(config_path, config_payload)
    env_path.write_text(
        _build_box_env_template(
            client_id=str(config_payload.get("client_id", "") or "PASTE_BOX_CLIENT_ID_HERE"),
            client_secret=str(config_payload.get("client_secret", "") or "PASTE_BOX_CLIENT_SECRET_HERE"),
            parent_folder_id=str(config_payload.get("parent_folder_id", "") or "PASTE_BOX_PARENT_FOLDER_ID_HERE"),
            tokens_file=str(tokens_path),
            folder_prefix=str(config_payload.get("folder_prefix", "fly_assay") or "fly_assay"),
        ),
        encoding="utf-8",
    )
    return {
        "config_file": str(config_path),
        "tokens_file": str(tokens_path),
        "env_file": str(env_path),
        "seeded_from": seeded_from,
        "tokens_write_mode": tokens_write_mode,
        "shared_tokens_source": str(legacy_source_tokens) if legacy_source_tokens else "",
    }


def resolve_box_config(settings: Any, *, legacy_repo_root: str | Path | None = None) -> ResolvedBoxConfig:
    effective = resolve_effective_box_settings(settings, legacy_repo_root=legacy_repo_root)
    client_id = effective.client_id
    client_secret = effective.client_secret
    parent_folder_id = effective.parent_folder_id
    settings_dict = _settings_dict(settings)
    config_file = str(settings_dict.get("config_file", "") or "")
    file_cfg = _load_optional_config_file(config_file, strict=False)
    legacy_cfg = discover_legacy_box_settings(legacy_repo_root)
    legacy_token_path = Path(str(legacy_cfg.get("tokens_file", "") or "")).expanduser() if legacy_cfg.get("tokens_file") else None

    placeholder_fields = [
        name
        for name, value in {
            "client_id": effective.client_id,
            "client_secret": effective.client_secret,
            "parent_folder_id": effective.parent_folder_id,
        }.items()
        if value and _is_placeholder_text(value)
    ]
    if placeholder_fields:
        raise BoxUploadError(
            "Box configuration still contains placeholder values for: "
            + ", ".join(placeholder_fields)
            + ". Regenerate the template from the repo's legacy Box settings or paste in the real values."
        )

    missing = [name for name, value in {
        "client_id": client_id,
        "client_secret": client_secret,
        "parent_folder_id": parent_folder_id,
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

    configured_token_path = str(file_cfg.get("tokens_file", settings_dict.get("tokens_file", "")) or "")
    configured_token_usable = _token_file_is_usable(configured_token_path)
    using_legacy_tokens = bool(
        legacy_token_path and legacy_token_path.exists() and token_path.resolve() == legacy_token_path.resolve()
    )
    if using_legacy_tokens and not configured_token_usable:
        legacy_client_id = str(legacy_cfg.get("client_id", "") or "")
        legacy_client_secret = str(legacy_cfg.get("client_secret", "") or "")
        legacy_parent_folder_id = str(legacy_cfg.get("parent_folder_id", "") or "")
        if legacy_client_id and legacy_client_secret:
            client_id = legacy_client_id
            client_secret = legacy_client_secret
        if (not parent_folder_id or _is_placeholder_text(parent_folder_id)) and legacy_parent_folder_id:
            parent_folder_id = legacy_parent_folder_id

    if not _token_file_is_usable(token_path):
        if legacy_token_path and legacy_token_path.exists() and _token_file_is_usable(legacy_token_path):
            token_path = legacy_token_path
            legacy_client_id = str(legacy_cfg.get("client_id", "") or "")
            legacy_client_secret = str(legacy_cfg.get("client_secret", "") or "")
            legacy_parent_folder_id = str(legacy_cfg.get("parent_folder_id", "") or "")
            if legacy_client_id and legacy_client_secret:
                client_id = legacy_client_id
                client_secret = legacy_client_secret
            if (not parent_folder_id or _is_placeholder_text(parent_folder_id)) and legacy_parent_folder_id:
                parent_folder_id = legacy_parent_folder_id
        else:
            raise BoxUploadError(
                "The Box tokens file does not contain a usable access/refresh token pair. "
                f"Current file: {token_path}. If this is still a template, point fin6 at the repo's real box_tokens.json or paste in real OAuth tokens."
            )
    return ResolvedBoxConfig(
        client_id=client_id,
        client_secret=client_secret,
        parent_folder_id=parent_folder_id,
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
    try:
        AccessToken, BoxClient, BoxOAuth, OAuthConfig, _UploadFileAttributes, _UploadFileAttributesParentField = _import_box_sdk()
        resolved = resolve_box_config(settings)
        tokens = load_json(resolved.tokens_file)
        if not _token_payload_is_usable(dict(tokens or {})):
            raise BoxUploadError(
                f"Tokens file is missing a usable access_token/refresh_token pair: {resolved.tokens_file}"
            )
        access_token = str(tokens.get("access_token", "") or "")
        refresh_token = str(tokens.get("refresh_token", "") or "")

        oauth = BoxOAuth(OAuthConfig(client_id=resolved.client_id, client_secret=resolved.client_secret))
        oauth.token_storage.store(AccessToken(access_token=access_token, refresh_token=refresh_token))
        original_refresh = oauth.refresh_token

        def refresh_and_save(*args, **kwargs):
            try:
                new_tokens = original_refresh(*args, **kwargs)
            except Exception as exc:
                raise BoxUploadError(
                    "Box token refresh failed. The refresh token may be stale or this config may still point at a template token file."
                ) from exc
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
    except BoxUploadError:
        raise
    except Exception as exc:
        raise BoxUploadError(f"Could not create the Box client: {exc}") from exc


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


def _resolve_processing_dir_candidate(candidate: str | Path | None, run_dir: Path) -> Optional[Path]:
    if not candidate:
        return None
    try:
        path = Path(candidate).expanduser()
    except Exception:
        return None
    if not path.is_absolute():
        path = (run_dir / path).resolve()
    if path.exists() and path.is_dir():
        return path
    return None


def _load_run_manifest_json(run_dir: Path) -> Dict[str, Any]:
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        payload = load_json(manifest_path)
    except Exception:
        return {}
    return dict(payload or {})


def _resolve_latest_processing_dir(run_dir: Path) -> Optional[Path]:
    latest_path = run_dir / "processed" / "latest_processing.json"
    if latest_path.exists():
        try:
            latest_payload = dict(load_json(latest_path) or {})
        except Exception:
            latest_payload = {}
        resolved = _resolve_processing_dir_candidate(latest_payload.get("processing_dir"), run_dir)
        if resolved is not None:
            return resolved

    manifest = _load_run_manifest_json(run_dir)
    resolved = _resolve_processing_dir_candidate(manifest.get("processing_dir"), run_dir)
    if resolved is not None:
        return resolved

    processed_root = run_dir / "processed"
    if processed_root.exists():
        proc_dirs = sorted([p for p in processed_root.glob("proc_*") if p.is_dir()])
        if proc_dirs:
            return proc_dirs[-1]
    return None


def _resolve_raw_video_path(run_dir: Path) -> Optional[Path]:
    manifest = _load_run_manifest_json(run_dir)
    raw_candidate = manifest.get("raw_video_path")
    resolved = _resolve_processing_dir_candidate(None, run_dir)
    if raw_candidate:
        raw_path = Path(str(raw_candidate)).expanduser()
        if not raw_path.is_absolute():
            raw_path = (run_dir / raw_path).resolve()
        if raw_path.exists() and raw_path.is_file():
            return raw_path
    for candidate in (run_dir / "raw_video.mp4", run_dir / "raw_video.avi"):
        if candidate.exists():
            return candidate
    return None


def _collect_core_assay_artifacts(run_dir: Path) -> List[Path]:
    selected: List[Path] = []
    raw_video = _resolve_raw_video_path(run_dir)
    if raw_video is not None and raw_video.exists():
        selected.append(raw_video)

    processing_dir = _resolve_latest_processing_dir(run_dir)
    if processing_dir is not None:
        for candidate in (processing_dir / "annotated_video.mp4", processing_dir / "annotated_video.avi", processing_dir / "report.pdf"):
            if candidate.exists() and candidate.is_file():
                selected.append(candidate)

    deduped: Dict[str, Path] = {}
    for item in selected:
        deduped[str(item.resolve())] = item.resolve()
    return sorted(deduped.values())


def collect_artifacts(run_dir: str | Path, mode: str = "summaries", *, include_backgrounds: bool = False) -> List[Path]:
    run_dir = Path(run_dir)
    if not run_dir.exists():
        raise BoxUploadError(f"Run directory does not exist: {run_dir}")
    mode_norm = str(mode or "summaries").strip().lower()
    files = [path for path in run_dir.rglob("*") if path.is_file()]
    if mode_norm in {"full", "full_session", "full-folder", "full_folder"}:
        return sorted(files)

    if mode_norm in {"raw+annotated+pdf", "raw_annotated_pdf", "core", "minimal", "assay-core", "assay_core"}:
        return _collect_core_assay_artifacts(run_dir)

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

    try:
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
    except BoxUploadError:
        raise
    except Exception as exc:
        raise BoxUploadError(f"Box upload failed: {exc}") from exc
