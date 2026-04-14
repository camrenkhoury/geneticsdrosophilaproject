# Drosophila Genetics GUI

This GUI provides a graphical interface to control the Drosophila genetics experimental setup.

## Features

- **Motion Control**: Home the gantry, move to predefined positions (Channel, Chamber, Tubes 1-5), and manual relative moves.
- **Device Control**: Control vacuum and vibration motors.
- **Operations**: Run automated fly sorting operation, assays, and fly classification.
- **Status Display**: Real-time position and status updates.
- **Log**: Scrollable log of operations and errors.
- **Simulation Mode**: Automatically detects if running on a computer without Pi hardware and simulates all operations.

## Requirements

- Python 3
- Tkinter (usually included with Python)
- On Raspberry Pi: gpiozero, ultralytics, opencv, etc.
- On computer: No hardware dependencies required - runs in simulation mode.

## Running the GUI

On the Raspberry Pi, navigate to the CodeDirectory and run:

```bash
python3 gui.py
```

On a computer (without Pi hardware), the GUI will automatically enter simulation mode. The mode is indicated in the status panel and title.

## Usage

1. **Home the motor** before starting operations (simulated homing in simulation mode).
2. Use **Motion Control** to move to specific locations for testing (simulated moves with delays).
3. Use **Device Control** to turn vacuum/vibration on/off (logged in simulation mode).
4. **Run Automated Operation** to start the full fly sorting cycle (simulates the entire process).
5. **Run Assay** to perform vibration assay (simulated vibration).
6. **Classify Fly** to capture and classify a fly image (returns random mock results in simulation).

## Simulation Mode

When gpiozero or ultralytics are not available, the GUI enters simulation mode:
- Motion commands update position with realistic delays
- Device controls log actions without hardware interaction
- Classification returns random mock results
- All operations complete successfully for testing the interface

## Notes

- The automated operation assumes channel detection has been performed and the JSON file is updated (or simulates it).
- All operations run in background threads to keep the GUI responsive.
- Errors are logged in the log area.