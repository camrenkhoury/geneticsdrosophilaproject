# v9 patch notes

This pass keeps the existing UI layout and flow intact, and only updates the sex-verification path plus the debug tuning controls.

## What changed

- Added a persisted operator setting: `sexing_verification_passes`
  - default is now `3`
  - debug tuning accepts `1` or `3`
- Auto-flow sex verification is now configurable:
  - `1` = single-pass classification
  - `3` = three chamber classifications with majority vote (`2/3` or `3/3`)
- The three-pass sequence reuses the same chamber cycle:
  - classify
  - re-pick from chamber
  - re-drop into chamber
  - classify again
  - repeat for the requested number of passes
- Count/reject logic stays aligned with the uploaded baseline classifier rules:
  - `count == 0` remains a reject path
  - `count > 1` remains a reject path
- Majority confirmation only routes to a sex-specific vial when the final pass still isolates exactly one fly.
  - If majority is reached but the final pass no longer has a clean single specimen, the fly is rejected instead of being sex-routed.
- The old “don’t waste a junk trip on a likely pickup miss” behavior is preserved and extended to the new verification flow:
  - when all completed passes are uncertain single-fly passes, the controller refreshes the channel before junk-routing
- Added the verification-pass control to the existing **Channel + loading tuning** section in `Debug / Advanced`
- Updated the shipped `operator_settings.json` so the repo starts in 3-pass mode
- Auto-flow master output now records the configured verification-pass count for the run

## Validation

- `python -m py_compile ...` passed on edited files
- `PYTHONPATH=. pytest -q` -> `27 passed`

## Files changed

- `stitch_operator/controller.py`
- `stitch_operator/app.py`
- `stitch_operator/settings.py`
- `stitch_operator/operator_settings.json`
- `stitch_operator/tests/test_controller_auto.py`
