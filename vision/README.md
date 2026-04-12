# Vision Layout

The current vision pipeline still lives in the top-level `fin6/` directory.

It has not been moved in this pass because:
- detection/result paths are already runtime-critical
- preview/status wiring depends on the current layout
- moving it safely requires a dedicated compatibility migration

This `vision/` directory exists to make the future packaging target explicit.

