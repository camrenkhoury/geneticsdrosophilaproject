# Avi Assay Mapping

This note documents the Pi-side assay workflow found in Avi's read-only directory:

- `/home/team8/geneticsdrosophilaproject/Avi Detection GUI code/Integrated1`

No files in Avi's directory were modified while producing this note.

## Scope

Goal: understand Avi's actual assay system well enough to map it into the current project architecture:

- host GUI = operator-facing UI only
- Pi backend = hardware, camera, saved setup, runtime artifacts, processing authority
- FastAPI = transport between host GUI and Pi backend

The intended implementation target is the current repo, not Avi's tree.

## High-level conclusion

Avi's assay flow is not a small helper. It is a complete subsystem with:

- profile-backed assay configuration
- assay background capture / restore
- transform setup
- multi-vial calibration editor
- recording
- offline processing
- report / CSV / annotated video outputs
- optional Box upload
- operator state integration for run/results pages

The assay GUI in Avi's operator shell is mostly an embedded wrapper around the standalone `fin6/fly_tracking_gui.py` app. The real logic lives in the `fin6` service and tracking modules, then gets mirrored into operator state.

## Files that matter

### Pi-side operator shell

- `stitch_operator/app.py`
- `stitch_operator/controller.py`
- `stitch_operator/state.py`
- `stitch_operator/assay_embed.py`
- `stitch_operator/services/assay.py`

### Pi-side standalone assay implementation

- `fin6/fly_tracking_gui.py`
- `fin6/assay_recording.py`
- `fin6/assay_processing.py`
- `fin6/assay_tracking.py`
- `fin6/assay_profile.py`

### Not the real implementation

- `CodeDirectory/assay.py`

That file is just a trivial legacy vibration script. It is not the real assay pipeline.

## Actual assay call graph

### Operator shell startup

1. `run_operator_app.py`
2. `stitch_operator.app.main()`
3. `OperatorApp`
4. `WorkflowController`
5. `WorkflowController.assay = AssayService(...)`

### Assay UI attachment

1. `stitch_operator/app.py`
2. `_build_assay_page()` and `_build_results_page()` create blank pages
3. `_maybe_attach_embedded_assay_pages()`
4. `EmbeddedAssayUI.attach_pages(...)`
5. `assay_embed.py` reuses most methods from `fin6/fly_tracking_gui.py`

So the assay pages are not handwritten operator pages. They are embedded fin6 pages.

### Recording path

1. UI button in `fin6/fly_tracking_gui.py`
   - `run_assay_recording()`
2. saves profile / calibration if needed
3. calls `record_assay_run(...)`
4. implementation in `fin6/assay_recording.py`
5. result handled by `_on_assay_record_done(...)`
6. embedded adapter mirrors run state back into operator controller

### Processing path

1. UI button in `fin6/fly_tracking_gui.py`
   - `process_last_assay_run()`
2. calls `process_last_assay(...)`
3. implementation in `fin6/assay_processing.py`
4. result handled by `_on_assay_process_done(...)`
5. embedded adapter mirrors processed artifact state back into operator controller

### Upload path

1. UI button in `fin6/fly_tracking_gui.py`
   - `upload_last_run()`
2. service method in `stitch_operator/services/assay.py`
   - `upload_last(...)`
3. Box helpers upload the selected artifact bundle

## Standalone assay GUI structure

The real assay page in `fin6/fly_tracking_gui.py` is split like this:

- left sidebar:
  - Profile
  - Background
  - Transform
  - Calibration
  - Recording
  - Processing
  - Export / Upload
- right viewer:
  - preview mode selector
    - calibration
    - background
    - transform
    - annotated
    - mask
    - raw
  - main assay canvas
  - playback controls
  - calibrated vial editor
  - assay log

This is a full setup + run + results workbench, not just a single "Run Assay" button.

## What the assay workflow actually does

### 1. Profile

`fin6/assay_profile.py` defines `AssayProfile`.

It stores:

- assay camera selection
- transform settings
- detector thresholds and limits
- analysis FPS and smoothing
- vibration settings
- output root
- Box upload settings
- calibration path
- current / previous background paths
- last run dir

This is the persistent source of assay configuration in Avi's system.

### 2. Background

The operator can:

- capture assay background
- import assay background
- restore previous background
- rebuild transformed background

This is profile-scoped. Backgrounds are tracked per profile.

### 3. Transform

Before calibration or processing, Avi applies transform settings:

- rotation
- horizontal / vertical flips
- crop rectangle

The assay GUI supports:

- test frame capture
- transform preview
- interactive crop selection
- crop-to-vials

### 4. Calibration

Calibration is multi-vial ROI based.

`AssayCalibration` contains:

- image shape
- vial calibrations
- ignored physical indices

Each vial calibration stores:

- ROI rectangle
- top reference
- baseline reference
- threshold reference
- physical vial index / label metadata

The GUI supports:

- drawing vial ROIs
- split / guided calibration tools
- editing threshold / baseline lines
- saving/loading calibration JSON
- testing calibration on a fresh frame

### 5. Recording

`fin6/assay_recording.py` handles recording.

Recording behavior:

- validates background + calibration exist
- validates calibration shape against transformed background
- creates a run dir like `assay_<timestamp>`
- copies setup snapshots into the run
- records raw video
- optionally captures periodic snapshots
- optionally pulses the vibration motor
- streams live preview frames via callback during recording

Important run artifacts:

- `run_manifest.json`
- `raw_video.mp4`
- `profile_snapshot.json`
- `transform_snapshot.json`
- `calibration_snapshot.json`
- `background_meta_snapshot.json`
- `background_raw_snapshot.png`
- `background_transformed_snapshot.png`
- optional snapshots directory

### 6. Processing

`fin6/assay_processing.py` handles offline processing.

Processing behavior:

- loads run manifest and snapshots
- reconstructs the correct context for that run
- validates calibration dimensions again
- produces processed output in `processed/proc_<timestamp>`
- writes annotated video, optional mask video, summary CSVs, processing JSON, and report outputs

Important processed artifacts:

- `processing_session.json`
- `annotated_video_path`
- `mask_video_path`
- `per_vial_summary_csv`
- `report_pdf` / summary PDF
- threshold crossing totals
- per-vial metrics

### 7. Upload

Box upload is optional and profile-driven.

Upload can happen:

- after recording
- after processing
- manually

Artifact mode is configurable.

## Operator state contract used by Avi

`stitch_operator/state.py` defines `AssayRunState`.

Fields mirrored into operator state:

- `run_dir`
- `preview_image_path`
- `processed_dir`
- `processed_at`
- `pdf_path`
- `processing_json`
- `summary_csv_path`
- `upload_status`
- `unique_crossings_total`
- `duration_s`
- `per_vial_summary`

This is important because Avi's operator app does not poll the fin6 app directly. It mirrors assay state into a stable controller/state model.

## How previews are handled in Avi's system

There are multiple preview concepts:

- setup preview on assay canvas
- live preview during recording
- raw video path after recording
- annotated video path after processing
- results page preview path mirrored from processed output

In `stitch_operator/services/assay.py` the runtime preview files include:

- `stitch_operator/runtime/assay_preview.png`
- `stitch_operator/runtime/assay_live_preview.png`

In the embedded adapter:

- after recording, preview state is updated from raw video / live preview context
- after processing, preview state is updated from annotated video path

So Avi's UI is not showing a single static assay image. It is switching preview source as the run progresses.

## Controller stage transitions

Relevant controller stages:

- `READY`
- `ASSAY`
- `PROCESSING`
- `RESULTS`

Assay transitions in `stitch_operator/controller.py`:

- `_run_assay_impl(...)`
  - validates assay background + calibration readiness
  - logs recording
  - streams preview callback updates
  - updates assay state with run dir / duration / preview path
  - sets stage to `ASSAY`

- `_process_last_assay_impl(...)`
  - processes last run
  - updates assay state with processed outputs
  - sets stage to `RESULTS`

The auto flow can also transition into assay and then processing when channel/loading stages are complete and readiness checks pass.

## What the current repo already has

### Present today

Current repo already has some assay plumbing:

- `host_app/operator_bridge.py`
  - `get_setup_status()`
  - `run_assay_from_saved_settings()`
  - `launch_fin6_gui(start_tab=\"assay\")`
- `pi_backend/control/assay_service.py`
  - wraps `run_assay_from_saved_settings()`
- `pi_backend/api/routes.py`
  - exposes `POST /run_assay`
- `host_app/controllers/remote_controller.py`
  - exposes `run_assay()`
- `host_app/gui/gui.py`
  - can open assay setup
  - can run assay
  - has an assay tab shell

### Missing compared to Avi's system

The current repo does **not** yet mirror Avi's full assay contract.

Missing pieces:

- no FastAPI endpoint for assay background capture
- no FastAPI endpoint for assay calibration save/load/test
- no FastAPI endpoint for assay preview image / live preview / latest processed preview
- no FastAPI endpoint for `process_last_assay`
- no FastAPI endpoint for `upload_last_run`
- no backend state model carrying assay run/process/results fields comparable to Avi's `AssayRunState`
- no host-side assay results panel wired to:
  - processed dir
  - summary CSV
  - report PDF
  - upload status
  - per-vial summary rows
- no host relay for assay preview/media updates

In other words: the current project can trigger assay, but it cannot yet reproduce Avi's full run/process/results workflow through FastAPI.

## Recommended mapping into the current architecture

### Rule

Do not embed Avi's Tk pages directly into the current host GUI.

Instead:

- keep Pi-side assay authority on the Pi
- expose explicit FastAPI actions and artifact routes
- render the current assay tab using the current control-panel theme

That matches the project's remote-mode architecture better than porting another embedded Tk app wholesale.

### Proposed Pi backend additions

Add Pi-side service actions for:

- assay setup status
- capture assay background
- restore previous assay background
- capture assay preview
- run assay
- process last assay
- upload last assay
- fetch latest assay preview artifact
- fetch latest report PDF
- fetch latest summary CSV metadata

Likely target files:

- `pi_backend/control/assay_service.py`
- `pi_backend/control/machine_service.py`
- `pi_backend/api/routes.py`
- `pi_backend/api/models.py`
- `pi_backend/core/runtime_state.py`

### Proposed runtime state additions

Add assay state analogous to Avi's `AssayRunState`:

- `run_dir`
- `preview_image_path`
- `processed_dir`
- `processed_at`
- `pdf_path`
- `processing_json`
- `summary_csv_path`
- `upload_status`
- `unique_crossings_total`
- `duration_s`
- `per_vial_summary`

This needs to be visible in `/status`, not just hidden inside local Python state.

### Proposed host GUI assay tab

The current assay tab should be extended to show:

- readiness:
  - assay background ready
  - assay calibration ready
  - assay camera
  - active assay profile
- actions:
  - Open Assay Setup
  - Capture / Refresh Assay Preview
  - Run Assay
  - Process Last Assay
  - Upload Last Run
- preview pane:
  - live or latest assay preview artifact from the Pi
- results summary:
  - run dir
  - processed dir
  - processed at
  - summary CSV path
  - PDF path
  - upload status
  - unique crossings
  - per-vial metrics

### Preview mapping recommendation

Use the same staged preview semantics Avi used:

- before run:
  - assay setup preview
- during recording:
  - live assay preview
- after recording:
  - last run preview / raw video-derived preview
- after processing:
  - annotated processed preview or processed media artifact

The host should not invent its own assay artifact state. It should display the Pi's current assay artifact truth.

## Recommended implementation sequence

### Phase 1

Expose Pi-side assay actions and status only:

- `process_last_assay`
- `upload_last_run`
- assay state in `/status`

This closes the biggest architecture gap quickly.

### Phase 2

Add artifact routes:

- latest assay preview image
- latest processed preview
- latest report PDF / summary CSV references

### Phase 3

Upgrade the host assay tab:

- readiness summary
- action buttons
- live/last preview
- results metadata block

### Phase 4

Wire final report/document visibility:

- open PDF
- open processed directory
- show per-vial summary table in host GUI

## File-to-file port map

### Avi source -> current target

- `stitch_operator/services/assay.py`
  -> `pi_backend/control/assay_service.py` and `host_app/operator_bridge.py`

- `stitch_operator/state.py::AssayRunState`
  -> `pi_backend/core/runtime_state.py` and `pi_backend/api/models.py`

- `stitch_operator/controller.py` assay stage transitions
  -> current host automation / host assay state updates

- `fin6/fly_tracking_gui.py` assay page behavior
  -> `host_app/gui/gui.py` assay tab layout and button flow

- `fin6/assay_recording.py`
  -> remains Pi-side execution authority

- `fin6/assay_processing.py`
  -> remains Pi-side execution authority

- `stitch_operator/assay_embed.py`
  -> do not port directly; copy only the useful state-mirroring ideas

## Non-goals

Do not:

- edit Avi's directory
- make the host GUI the source of truth for assay artifacts
- run assay processing on the host in remote mode
- directly embed Avi's Tk UI into the current GUI unless there is a very strong reason later

## Practical takeaway

The cleanest port is:

1. keep assay execution and artifacts on the Pi
2. expose the missing assay run/process/upload/status/artifact contracts through FastAPI
3. rebuild the assay tab in `host_app/gui/gui.py` with the current theme
4. relay Pi-generated preview and result artifacts into the host assay panel

That reproduces the useful behavior from Avi's system without inheriting his full UI architecture.
