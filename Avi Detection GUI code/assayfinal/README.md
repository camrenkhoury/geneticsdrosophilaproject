# fin6 assay-only workflow

This bundle contains only the assay pipeline.

## What changed

- The main GUI now has two tabs: **Assay** and **Debug**.
- The **Assay** tab is the simple biologist view with:
  - video preview
  - playback for processed, raw, and mask videos
  - **Run Assay Recording**
  - **Process Assay**
  - **Export to Box**
- The **Debug** tab keeps the existing calibration, background, transform, recording, processing, motor, and Box settings.
- The assay camera is locked to the **HD Webcam eMeet C960** on **usb-xhci-hcd.1-2**. The Brio is no longer used for the assay path.
- The stitch tracker now reuses IDs more reliably when flies merge into one blob and split again.
- The report PDF is simplified to a small set of tables:
  - 0 s to 10 s x-displacement summary
  - threshold crossing summary
  - threshold crossing events
  - velocity summary
- Processing avoids some unnecessary transforms and report-generation overhead.

## Main files

- `fly_tracking_gui.py` — GUI entry point
- `assay_recording.py` — raw assay recording
- `assay_processing.py` — offline assay processing
- `assay_tracking.py` — calibration, detection, tracking, and report generation
- `camera_sources.py` — assay camera selection and camera open/reopen behavior

## Run the GUI

```bash
python fly_tracking_gui.py
```

## Run tests

```bash
pytest -q
```
