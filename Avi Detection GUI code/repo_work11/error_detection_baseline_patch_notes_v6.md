# Error-detection baseline patch (v6)

This pass kept the existing UI/workflow layout and only touched the sexing error-detection path, the protected debug page, and a small channel-preview trim adjustment.

## What changed

- Replaced the simple chamber occupied/not-occupied gate with a count-based blob-detection pass derived from the uploaded `fly_classifier1.py` baseline.
- The sexing service now reports:
  - `count`
  - `errors`
  - `occupancy_score`
  - optional `debug_image_path`
- The classifier now behaves more predictably for chamber-edge cases:
  - `count == 0` -> `UNCERTAIN` with `CHAMBER_EMPTY` or `CHAMBER_OCCUPIED_UNCOUNTED`
  - `count > 1` -> `UNCERTAIN` with `MULTIPLE_FLIES:<n>`
  - `count == 1` -> proceed to YOLO sex classification
- `inspect_chamber()` now uses the same count-based error detector, so the chamber-preflight / chamber-clear logic has access to better information.
- Added a protected **Sexing error detection** section in `Debug / Advanced` with tuning fields for:
  - corner sample size
  - background tolerance
  - open / close / erode kernels
  - erode iterations
  - minimum fly area fraction
  - maximum single-fly area
- Added **Inspect Chamber Now** in `Debug / Advanced` to run the tuned error detector against the current chamber image without changing the workflow UI.
- Preserved the rest of the app flow and routing logic.
- Reduced channel preview over-trimming slightly by increasing the preview trim padding.

## Files changed

- `stitch_operator/services/sexing.py`
- `stitch_operator/controller.py`
- `stitch_operator/state.py`
- `stitch_operator/settings.py`
- `stitch_operator/operator_settings.json`
- `stitch_operator/app.py`
- `stitch_operator/tests/test_sexing_error_detection.py`

## Validation

- `PYTHONPATH=. pytest -q` -> `23 passed`
- compile checks passed on edited files
