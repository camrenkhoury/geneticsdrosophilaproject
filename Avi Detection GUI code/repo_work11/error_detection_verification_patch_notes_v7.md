# v7 patch notes — error detection + two-pass sex verification

This pass was intentionally narrow.

## What changed

- Restored clearer **count/reject visibility** in the sexing pane without changing the page layout:
  - the Loading / Sexing panel now shows:
    - `Count: ...`
    - `Rejects: ...`
    - the detailed pass log / reject reason

- Tightened **auto-flow count-based rejection**:
  - `count == 0` rejects the sexing attempt
  - `count > 1` rejects the sexing attempt
  - uncertain first-pass classifications stay rejects

- Added **two-pass sex verification** to the auto-loading path:
  - first chamber sex
  - re-acquire from chamber
  - re-drop into chamber
  - second chamber sex
  - only matching `male/male` or `female/female` advances to a sex-specific vial
  - mismatches / invalid second passes reject as a sexing failure

- Preserved the earlier **don’t waste a junk trip on a likely pickup miss** behavior:
  - if the first pass is uncertain, auto-flow refreshes the channel before sending to junk
  - if the source is still present, it treats that cycle as a pickup miss and moves on / skip-logic still applies

- Expanded saved auto-flow stats/history:
  - `pickup_rejects`
  - `sexing_rejects`
  - `total_rejects`
  - per-attempt count / errors / reject reason

## What I did not change

- I did **not** rework the current UI layout.
- I did **not** change the manual Route Next Fly flow in this pass.
  - The new two-pass verification is applied to the **auto-flow path** so the unattended workflow gets the reliability gain without changing manual operator behavior unnecessarily.

## Validation

- `PYTHONPATH=. pytest -q` → `24 passed`
- compile checks passed for:
  - `stitch_operator/controller.py`
  - `stitch_operator/app.py`
  - `stitch_operator/services/sexing.py`
