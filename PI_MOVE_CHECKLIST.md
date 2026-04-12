# Raspberry Pi Move Checklist

Use this checklist before moving the project from the Windows home machine to the Raspberry Pi.

If the operator says "prep this for Pi" or asks for a one-click Pi conversion, use this file as the source of truth.

## Must be true on the Pi

1. Keep the repo layout the same:
   - `geneticsdrosophilaproject/CodeDirectory`
   - `geneticsdrosophilaproject/fin6`

2. Install the Pi GUI/tracking dependencies:
   - Run `fin6/install_pi.sh`

3. Install `gpiozero` on the Pi:
   - `sudo apt install -y python3-gpiozero`
   - Reason: without `gpiozero`, the main GUI stays in simulation mode.

4. Install the classifier package if real fly classification is needed:
   - `python3 -m pip install ultralytics`

5. Put the classifier model at:
   - `~/newone.pt`
   - Or change `CLASSIFIER_MODEL_PATH` in `CodeDirectory/fly_classifier.py`

6. Verify the Pi camera capture command exists:
   - `/usr/bin/rpicam-still`
   - Reason: `fly_classifier.py` calls that exact path.

7. Verify the hardware pin mapping matches the real wiring:
   - `CodeDirectory/config.py`

8. Verify the `fin6` camera settings match the actual Pi camera devices:
   - `fin6/.fly_tracking_gui_settings.json`
   - Especially:
     - `channel_device_var`
     - `assay_camera_backend_var`
     - `assay_camera_device_var`
     - `assay_camera_index_var`

## Known path assumptions

1. The main GUI channel output path is already Pi-friendly if the repo layout is preserved.

2. `fin6/.fly_tracking_gui_settings.json` already contains Pi-style paths under `/home/team8/...`, but the username or device nodes may need updating on the target Pi.

3. `CodeDirectory/fly_classifier.py` assumes:
   - model path: `~/newone.pt`
   - temp image dir: `~/tempClassImage`
   - camera command: `/usr/bin/rpicam-still`

4. `CodeDirectory/nozzle_implementation.py` and `CodeDirectory/gantryOperation.py` contain older JSON path assumptions and should be sanity-checked if those scripts are used directly outside the GUI.

## Success checks on the Pi

1. Launch `python3 gui.py` from `CodeDirectory`.

2. Confirm the window says `Hardware Mode`, not `Simulation Mode`.

3. Move to Tube 1 or Channel and confirm the log does not say `Simulated move`.

4. Run the `fin6` GUI and confirm the output folder updates under the Pi path.

5. If classification is needed, run one classification and confirm it does not report simulation mode.

## Current gaps to fix before calling it fully plug-and-play

1. `fin6/install_pi.sh` should also install `python3-gpiozero`.

2. The classifier path should ideally be configurable instead of hardcoded to `~/newone.pt`.

3. The camera command path in `fly_classifier.py` should ideally be configurable or validated at startup.

4. A startup self-check would help show which subsystems are live versus simulated.
