#!/usr/bin/env python3
"""
Command-line entry point for the fin6 assay workflow.

The GUI is the primary operator interface, but this CLI keeps the record-first /
process-later pipeline scriptable for debugging, batch work, and remote use over
SSH on the Raspberry Pi.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional

import cv2

from assay_processing import batch_process_folder, manual_upload_run, process_assay_run, process_last_assay
from assay_profile import AssayProfile, ProfileStore
from assay_recording import record_assay_run
from assay_tracking import calibrate_assay_interactive
from background_manager import (
    capture_profile_background,
    current_background_preview_path,
    get_background_store,
    import_profile_background,
    restore_previous_background,
)
from shared_utils import ensure_dir
from transform_utils import apply_image_transform, describe_transform
from camera_sources import open_assay_camera


PROJECT_ROOT = Path(__file__).resolve().parent
PROFILES_ROOT = PROJECT_ROOT / "profiles"


class CliError(RuntimeError):
    """Raised when a CLI command cannot complete."""


def _logger(message: str) -> None:
    print(message, flush=True)


def _profile_store() -> ProfileStore:
    return ProfileStore(PROFILES_ROOT)


def _resolve_profile(store: ProfileStore, profile_ref: Optional[str]) -> AssayProfile:
    if profile_ref:
        candidate = Path(profile_ref).expanduser()
        if candidate.exists() and candidate.suffix.lower() == ".json":
            return store.load_profile(candidate)
        return store.load_profile(profile_ref)
    profile = store.load_last_used()
    if profile is not None:
        return profile
    default_path = store.profile_path("default")
    if default_path.exists():
        return store.load_profile(default_path)
    raise CliError(
        "No assay profile was supplied and no last-used profile exists. "
        "Create one in the GUI or run `assay_workflow_cli.py profile-create default`."
    )


def _save_profile(store: ProfileStore, profile: AssayProfile) -> Path:
    path = store.save_profile(profile)
    print(f"Saved profile: {path}")
    return path


def _profile_summary(profile: AssayProfile) -> str:
    return (
        f"Profile: {profile.name}\n"
        f"  assay camera: {profile.assay_camera.device} ({profile.assay_camera.width}x{profile.assay_camera.height} @ {profile.assay_camera.fps:g} fps)\n"
        f"  duration: {profile.assay_duration_s:g} s\n"
        f"  analysis fps: {profile.analysis.analysis_fps:g}\n"
        f"  transform: {describe_transform(profile.transform)}\n"
        f"  calibration: {profile.calibration_path or 'none'}\n"
        f"  last run: {profile.last_run_dir or 'none'}"
    )


def _background_path_for_calibration(profile: AssayProfile) -> Path:
    preview = current_background_preview_path(profile, PROJECT_ROOT)
    if preview is None or not preview.exists():
        raise CliError("No active transformed background is available for this profile.")
    return preview


def _capture_test_frame(profile: AssayProfile):
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
        return camera.read()


def command_profile_list(args: argparse.Namespace) -> int:
    store = _profile_store()
    names = store.list_profile_names()
    if not names:
        print("No profiles found.")
        return 0
    print("Profiles:")
    for name in names:
        print(f"- {name}")
    return 0


def command_profile_create(args: argparse.Namespace) -> int:
    store = _profile_store()
    if args.copy_from:
        new_path = store.duplicate_profile(args.copy_from, args.name)
        print(f"Created profile copy: {new_path}")
        return 0
    profile = store.create_profile(args.name)
    if args.output_root:
        profile.outputs.output_root = args.output_root
    if args.calibration_path:
        profile.calibration_path = args.calibration_path
    _save_profile(store, profile)
    return 0


def command_profile_show(args: argparse.Namespace) -> int:
    profile = _resolve_profile(_profile_store(), args.profile)
    print(_profile_summary(profile))
    return 0


def command_background_capture(args: argparse.Namespace) -> int:
    store = _profile_store()
    profile = _resolve_profile(store, args.profile)
    record = capture_profile_background(profile, PROJECT_ROOT, frame_count=int(args.frames), logger=_logger)
    profile.current_background_path = record.transformed_path
    profile.background_meta_path = str(get_background_store(profile, PROJECT_ROOT).current_meta_path.resolve())
    _save_profile(store, profile)
    print(f"Current background: {record.transformed_path}")
    return 0


def command_background_import(args: argparse.Namespace) -> int:
    store = _profile_store()
    profile = _resolve_profile(store, args.profile)
    record = import_profile_background(profile, PROJECT_ROOT, args.image, logger=_logger)
    profile.current_background_path = record.transformed_path
    profile.background_meta_path = str(get_background_store(profile, PROJECT_ROOT).current_meta_path.resolve())
    _save_profile(store, profile)
    print(f"Imported background: {record.transformed_path}")
    return 0


def command_background_restore(args: argparse.Namespace) -> int:
    store = _profile_store()
    profile = _resolve_profile(store, args.profile)
    record = restore_previous_background(profile, PROJECT_ROOT)
    profile.current_background_path = record.transformed_path
    profile.background_meta_path = str(get_background_store(profile, PROJECT_ROOT).current_meta_path.resolve())
    _save_profile(store, profile)
    print(f"Restored background: {record.transformed_path}")
    return 0


def command_transform_test(args: argparse.Namespace) -> int:
    profile = _resolve_profile(_profile_store(), args.profile)
    if args.input:
        image = cv2.imread(str(args.input), cv2.IMREAD_COLOR)
        if image is None:
            raise CliError(f"Could not read image: {args.input}")
    else:
        image = _capture_test_frame(profile)
    transformed = apply_image_transform(image, profile.transform)
    output = Path(args.output) if args.output else ensure_dir(PROJECT_ROOT / "outputs" / "transform_tests") / f"{profile.slug}_transform_test.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), transformed):
        raise CliError(f"Could not write transform preview: {output}")
    print(f"Transform: {describe_transform(profile.transform)}")
    print(f"Saved transform preview: {output}")
    return 0


def command_calibrate(args: argparse.Namespace) -> int:
    store = _profile_store()
    profile = _resolve_profile(store, args.profile)
    background_path = Path(args.background) if args.background else _background_path_for_calibration(profile)
    output_json = Path(args.output) if args.output else Path(profile.calibration_path or (PROJECT_ROOT / "calibrations" / f"{profile.slug}_calibration.json"))
    output_json = output_json if output_json.is_absolute() else (PROJECT_ROOT / output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    calibration = calibrate_assay_interactive(
        background_path=background_path,
        output_json=output_json,
        total_vials=int(args.total_vials),
        ignored_physical_indices=(),
        tube_height_mm=args.tube_height_mm,
        tube_width_mm=args.tube_width_mm,
    )
    profile.calibration_path = str(output_json.resolve())
    _save_profile(store, profile)
    print(f"Saved calibration: {output_json}")
    print(f"Enabled vials: {len(calibration.enabled_vials)} / {len(calibration.vials)}")
    return 0


def command_record(args: argparse.Namespace) -> int:
    store = _profile_store()
    profile = _resolve_profile(store, args.profile)
    if args.duration is not None:
        profile.assay_duration_s = float(args.duration)
    if args.record_fps is not None:
        profile.assay_camera.fps = float(args.record_fps)
    manifest = record_assay_run(profile, PROJECT_ROOT, logger=_logger)
    run_dir = str(manifest["run_dir"])
    profile.last_run_dir = run_dir
    _save_profile(store, profile)
    print(f"Recorded run: {run_dir}")
    if args.process_after:
        result = process_assay_run(run_dir, profile_override=profile, logger=_logger)
        print(f"Processed run: {result['run_dir']}")
    return 0


def command_process(args: argparse.Namespace) -> int:
    profile = _resolve_profile(_profile_store(), args.profile) if args.profile else None
    result = process_assay_run(args.run, profile_override=profile, logger=_logger)
    print(f"Processed run: {result['run_dir']}")
    print(f"Per-vial summary: {result.get('per_vial_summary_csv', '')}")
    print(f"Report PDF: {result.get('report_pdf', '')}")
    return 0


def command_process_last(args: argparse.Namespace) -> int:
    store = _profile_store()
    profile = _resolve_profile(store, args.profile)
    result = process_last_assay(profile, PROJECT_ROOT, logger=_logger)
    profile.last_run_dir = str(result.get("run_dir", profile.last_run_dir or ""))
    _save_profile(store, profile)
    print(f"Processed last run: {result['run_dir']}")
    return 0


def command_batch_process(args: argparse.Namespace) -> int:
    profile = _resolve_profile(_profile_store(), args.profile) if args.profile else None
    results = batch_process_folder(args.folder, profile_override=profile, logger=_logger)
    okay = sum(1 for item in results if "error" not in item)
    failed = len(results) - okay
    print(f"Batch processed {len(results)} runs: ok={okay} failed={failed}")
    return 0 if failed == 0 else 1


def command_upload(args: argparse.Namespace) -> int:
    profile = _resolve_profile(_profile_store(), args.profile)
    run_dir = args.run or profile.last_run_dir
    if not run_dir:
        raise CliError("No run directory was provided and the profile has no last_run_dir.")
    result = manual_upload_run(run_dir, profile.box_upload, artifact_mode=args.artifact_mode or profile.box_upload.artifact_mode, logger=_logger)
    print(f"Uploaded artifacts for: {run_dir}")
    print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="fin6 assay workflow CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("profile-list", help="List available assay profiles")
    p.set_defaults(func=command_profile_list)

    p = subparsers.add_parser("profile-create", help="Create a new profile or duplicate an existing one")
    p.add_argument("name")
    p.add_argument("--copy-from", default="")
    p.add_argument("--output-root", default="")
    p.add_argument("--calibration-path", default="")
    p.set_defaults(func=command_profile_create)

    p = subparsers.add_parser("profile-show", help="Show one profile summary")
    p.add_argument("profile", nargs="?", default="")
    p.set_defaults(func=command_profile_show)

    p = subparsers.add_parser("background-capture", help="Capture a new assay background using the profile camera")
    p.add_argument("--profile", default="")
    p.add_argument("--frames", type=int, default=25)
    p.set_defaults(func=command_background_capture)

    p = subparsers.add_parser("background-import", help="Import an assay background image from disk")
    p.add_argument("image")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_background_import)

    p = subparsers.add_parser("background-restore", help="Restore the previous assay background")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_background_restore)

    p = subparsers.add_parser("transform-test", help="Apply the profile transform to a frame or image and save a preview")
    p.add_argument("--profile", default="")
    p.add_argument("--input", default="")
    p.add_argument("--output", default="")
    p.set_defaults(func=command_transform_test)

    p = subparsers.add_parser("calibrate", help="Run guided assay calibration against the current background")
    p.add_argument("--profile", default="")
    p.add_argument("--background", default="")
    p.add_argument("--output", default="")
    p.add_argument("--total-vials", type=int, default=4)
    p.add_argument("--tube-height-mm", type=float, default=None)
    p.add_argument("--tube-width-mm", type=float, default=None)
    p.set_defaults(func=command_calibrate)

    p = subparsers.add_parser("record", help="Record one assay run using the saved profile")
    p.add_argument("--profile", default="")
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--record-fps", type=float, default=None)
    p.add_argument("--process-after", action="store_true")
    p.set_defaults(func=command_record)

    p = subparsers.add_parser("process", help="Process one recorded assay run or raw assay video")
    p.add_argument("run")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_process)

    p = subparsers.add_parser("process-last", help="Process the profile's most recent assay run")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_process_last)

    p = subparsers.add_parser("batch-process", help="Process all assay run folders found under a directory")
    p.add_argument("folder")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_batch_process)

    p = subparsers.add_parser("upload", help="Upload a run folder to Box using the profile settings")
    p.add_argument("run", nargs="?", default="")
    p.add_argument("--profile", default="")
    p.add_argument("--artifact-mode", default="")
    p.set_defaults(func=command_upload)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - final CLI safety net
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
