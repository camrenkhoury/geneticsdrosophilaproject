# Tester feedback fixes

This patch focused only on issues that were confirmed as real bugs or clear workflow problems.

## Fixed

- **Assay vibration was not running**
  - The shipped `stitch_operator` assay profile had the motor disabled.
  - The default profile now enables the vibration motor again.

- **Assay vial indexing was wrong for the junk vial layout**
  - The operator layout is `V1=Junk`, `V2=M1`, `V3=F1`, `V4=M2`, `V5=F2`.
  - The assay service now treats junk vials as ignored physical vials during assay calibration.
  - Existing calibration files are normalized on load so the assay side keeps the right physical-vial mapping.
  - Per-vial results in the UI now map by **physical vial index**, which fixes the `V5`/results mismatch.

- **Results page was not surfacing the generated PDF correctly**
  - Processing outputs use `report_pdf`, but the controller UI bridge was not mapping that into the operator state.
  - The results page now shows the PDF path correctly.
  - It also now shows the processed folder and summary CSV, with buttons to open them.
  - After processing, the results preview can show a generated graph image from the processed output.

- **Auto flow wasted time routing “no-fly / pickup miss” cases to junk**
  - In the uncertain path, auto flow now re-checks the channel **before** doing the junk route.
  - If the source is still sitting on the channel, it is treated as a pickup miss and junk routing is skipped.

- **Uncertain/manual routing dialog could block unattended use**
  - The manual routing choice dialog now supports an automatic timeout.
  - If no operator chooses male/female within 5 seconds, the specimen is sent to the junk vial instead of stalling indefinitely.
  - The dialog now auto-closes on timeout.

- **Channel capture had avoidable overhead**
  - Raw channel images are now saved as JPEG instead of PNG to reduce repeated write cost.
  - Channel image saving uses lower-overhead encoding settings.
  - Channel camera description lookups are cached.
  - Channel capture timings are logged so slow captures are easier to diagnose.

- **Channel preview formatting**
  - The channel preview display now trims large black borders in the UI so the annotated channel image fills the preview area better.

## Left as-is

- **Vial count reset behavior**
  - The front page reset/new-run control was already present, so that was not reworked again.

- **Vial-card placement above pictures**
  - I did not change that layout in this pass because it is mostly presentation, not a confirmed functional bug.

- **Male/female vial rotation**
  - The controller is already written to fill the first matching vial before moving to the next one.
  - No change was made there.
