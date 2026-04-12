# Raspberry Pi Move Checklist

Use this checklist before moving the project from the Windows home machine to the Raspberry Pi.

If the operator says "prep this for Pi" or asks for a one-click Pi conversion, use this file as the source of truth.

## Must be true on the Pi

1. Keep the repo layout the same:
   - `geneticsdrosophilaproject/CodeDirectory`
   - `geneticsdrosophilaproject/vision/fin6`
   - `geneticsdrosophilaproject/start_backend.sh`

2. Install the Pi GUI/tracking dependencies:
   - Run `vision/fin6/install_pi.sh`

3. Install `gpiozero` on the Pi:
   - `sudo apt install -y python3-gpiozero`
   - Reason: without `gpiozero`, the main GUI stays in simulation mode.

4. Install the classifier package if real fly classification is needed:
   - `python3 -m pip install ultralytics`

5. Put the classifier model at:
   - `best.pt` at the repository root by default
   - Or override `DROSOPHILA_MODEL_PATH`

6. Verify the Pi camera capture command exists:
   - `/usr/bin/rpicam-still`
   - Reason: `fly_classifier.py` calls that exact path.

7. Verify the hardware pin mapping matches the real wiring:
   - `CodeDirectory/config.py`

8. Verify the vision camera settings match the actual Pi camera devices:
   - `vision/fin6/.fly_tracking_gui_settings.json`
   - Especially:
     - `channel_device_var`
     - `assay_camera_backend_var`
     - `assay_camera_device_var`
     - `assay_camera_index_var`

## Known path assumptions

1. The main GUI channel output path is already Pi-friendly if the repo layout is preserved.

2. `vision/fin6/.fly_tracking_gui_settings.json` is a local runtime settings file when present and is not tracked in git.

3. `CodeDirectory/fly_classifier.py` assumes:
   - model path defaults to the repo-root `best.pt`
   - temp image dir defaults to `CodeDirectory/tempClassImage`
   - camera command: `/usr/bin/rpicam-still`

4. `CodeDirectory/nozzle_implementation.py` and `CodeDirectory/gantryOperation.py` now resolve the shared channel-detection output path from the repo layout.

## Success checks on the Pi

1. Launch `python3 gui.py` from `CodeDirectory`.

2. Confirm the window says `Hardware Mode`, not `Simulation Mode`.

3. Move to Tube 1 or Channel and confirm the log does not say `Simulated move`.

4. Run the vision GUI and confirm the output folder updates under the Pi path.

5. If classification is needed, run one classification and confirm it does not report simulation mode.

## Current gaps to fix before calling it fully plug-and-play

1. `vision/fin6/install_pi.sh` should also install `python3-gpiozero`.

2. Backend and GUI config should be copied from the tracked example files into local override files where machine-specific values are needed.

3. The camera command path in `fly_classifier.py` should ideally be configurable or validated at startup.

4. A startup self-check would help show which subsystems are live versus simulated.
