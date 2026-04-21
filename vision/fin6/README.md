
# Fruit Fly Tracking System for Brio + Selectable Assay Camera

This project integrates the uploaded `fly_x_detector.py` channel detector into a larger Raspberry Pi workflow:

- **Channel mode / Brio (`/dev/video8`)**
  - Capture a background
  - Calibrate the horizontal lane
  - Detect flies against the white background
  - Run live detection

- **Assay mode / selectable camera**
  - Capture a background
  - Default to a USB webcam assay source in this variant
  - Switch between a USB webcam (`OpenCV`) or IMX477 (`Pi HQ / Picamera2`) in the GUI
  - Override the webcam device path as needed (default `/dev/video10`)
  - Calibrate any number of vertical assay lanes with an in-GUI editor
  - Draw independent rectangles or split one larger assay area into draggable lanes
  - Edit saved regions, reorder them, toggle active/ignored, and re-save without starting over
  - Track multiple flies per active assay lane
  - Run a 30-second assay
  - Save snapshots every second
  - Save raw + annotated video
  - Generate CSV, SQLite, JSON, PNG graphs, and a PDF report

- **GUI**
  - One window with a Channel tab and an Assay tab
  - Dashboard-style layout with larger previews, clearer controls, and status indicators
  - In-GUI assay calibration editor with draggable rectangles, resize handles, top/baseline lines, lane dividers, undo/redo, and save/load/reset
  - Preview modes for raw, annotated, mask, and calibration-overlay views
  - Real-time table of active fly IDs and distance from baseline
  - Persistent last-used settings in `.fly_tracking_gui_settings.json`

## Files

- `fly_x_detector.py`
  - Your uploaded detector, used as the basis for the Brio channel mode.
- `brio_channel_cli.py`
  - CLI for Brio background / calibration / detect / live.
- `assay_tracking.py`
  - Assay calibration, detection, tracking, and report generation for either a USB webcam or IMX477.
- `fly_tracking_gui.py`
  - GUI integrating both modes.
- `camera_sources.py`
  - Brio/UVC + Picamera2 camera helpers.
- `shared_utils.py`
  - JSON, video-writer, and small utility helpers.
- `install_pi.sh`
  - Raspberry Pi installation helper.
- `requirements.txt`
  - Extra Python dependencies.

## Recommended Raspberry Pi install

Use Raspberry Pi OS Bookworm or newer.

### Option A: Raspberry Pi system Python (recommended on Pi)

```bash
cd ~/fly_tracking_system
chmod +x install_pi.sh
./install_pi.sh
```

### Option B: manual install

```bash
sudo apt update
sudo apt install -y \
    python3-picamera2 \
    python3-opencv \
    python3-pil \
    python3-pil.imagetk \
    python3-tk \
    python3-pandas \
    python3-scipy \
    python3-skimage \
    python3-matplotlib \
    ffmpeg
```

If you prefer a virtual environment, create it with `--system-site-packages` so the
`picamera2` apt package remains visible:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Channel mode (Brio)

### 1) Capture a background

```bash
python3 brio_channel_cli.py background \
  -o backgrounds/channel_bg.png \
  --device /dev/video8 \
  --width 1920 --height 1080 --fps 30
```

### 2) Calibrate the channel

```bash
python3 brio_channel_cli.py calibrate \
  -b backgrounds/channel_bg.png \
  -c calibrations/channel_calibration.json \
  --channel-mm 111.0
```

Click the left and right ends of the channel axis.

### 3) Run one detection

```bash
python3 brio_channel_cli.py detect \
  -b backgrounds/channel_bg.png \
  -c calibrations/channel_calibration.json \
  -o outputs/channel
```

### 4) Run live detection

```bash
python3 brio_channel_cli.py live \
  -b backgrounds/channel_bg.png \
  -c calibrations/channel_calibration.json
```

Press `q` or `Esc` to quit the OpenCV live window.

## Assay mode

### 1) Capture a background

```bash
python3 assay_tracking.py background \
  -o backgrounds/assay_bg.png \
  --camera-backend opencv \
  --camera-device /dev/video10 \
  --width 1536 --height 864 --fps 10
```

### 2) Calibrate the assay

```bash
python3 assay_tracking.py calibrate \
  -b backgrounds/assay_bg.png \
  -o calibrations/assay_calibration.json \
  --tube-height-mm 100
```

CLI calibration is now a legacy fallback. It still uses OpenCV windows, but it no longer hard-requires exactly 5 ROIs unless you pass `--total-vials`.

The recommended workflow is the GUI editor on the Assay tab.

### 3) Run the assay

```bash
python3 assay_tracking.py run \
  -b backgrounds/assay_bg.png \
  -c calibrations/assay_calibration.json \
  -o outputs/assay \
  --camera-backend opencv \
  --camera-device /dev/video10 \
  --seconds 30 \
  --fps 10 \
  --width 1536 \
  --height 864
```

## GUI

```bash
python3 fly_tracking_gui.py
```

### Channel tab
- Capture background
- Calibrate
- Detect once
- Start live
- Stop live

### Assay tab
- Capture or load a background
- Select the assay camera source (`USB webcam` or `Pi HQ`) and device fallback in `Run settings`
- Load an existing calibration into the editor
- Use `Select`, `Draw region`, or `Split lanes`
- Drag region edges, drag shared dividers, and drag the top / baseline guides
- Duplicate, reorder, delete, undo, redo, reset, or toggle regions active / ignored
- Save calibration
- Test the current calibration on one live frame
- Preview the mask view
- Start or stop the assay

The assay tab now shows:
- a large main preview with `Calibration`, `Annotated`, `Raw`, and `Mask` views
- assay region overlays with lane IDs, top markers, baseline markers, and ignored vs active state
- live fly labels and track overlays
- a live table of currently active flies
- a timestamped assay log

### Recommended GUI calibration workflow
1. Capture or load the assay background.
2. Open the Assay tab and stay in `Calibration` view.
3. Choose `Draw region` for free rectangles or `Split lanes` to draw one larger assay area and subdivide it.
4. Select a lane to move or resize it.
5. Drag the yellow top line and green baseline line until the references look correct.
6. Use the region list and tools to reorder lanes, duplicate a lane, or mark a lane ignored.
7. Click `Save calibration`.
8. Use `Test on one frame` or `Preview mask` before starting the assay.

## Calibration JSON schema

The detector still runs from the same core vial fields, so older logic stays compatible. New GUI-edited calibration files add a few metadata fields:

- `schema_version`
  - `2` for GUI-edited files.
- `editor_mode`
  - Typically `gui_editor` for the new Tkinter editor or `opencv_legacy` for the old CLI fallback.
- `editor_meta`
  - Optional metadata used by the editor, such as split-lane settings.
- `vials[*].label`
  - Optional operator-facing label.
- `vials[*].group_id`
  - Optional grouping key for lanes created from a shared split area so their vertical dividers remain editable.

The detector-critical fields are still present:

- `background_path`
- `image_shape_hw`
- `ignored_physical_indices`
- `vials[*].physical_index`
- `vials[*].assay_index`
- `vials[*].enabled`
- `vials[*].roi_xywh`
- `vials[*].top_point_px`
- `vials[*].baseline_point_px`
- `vials[*].tube_height_mm`

## Migration notes

- Existing assay calibration JSON files still load in the new GUI.
- Old files without `schema_version`, `editor_mode`, `editor_meta`, `label`, or `group_id` are normalized automatically on load.
- Re-saving an old calibration through the GUI upgrades it to the new schema while preserving the detector fields.
- The old CLI calibration flow remains available as a fallback, but the GUI editor is the supported operator workflow now.

## Output files from the assay

Each assay session creates a timestamped folder like:

```text
outputs/assay/assay_YYYYMMDD_HHMMSS/
```

Typical contents:

```text
annotated_video.mp4
raw_video.mp4
snapshots/
  snapshot_00s.png
  snapshot_01s.png
  ...
detections.csv
tracks.csv
per_fly_summary.csv
per_vial_summary.csv
results.sqlite
graphs/
  tube_1_overlay.png
  tube_1_fly_1.png
  ...
report.pdf
session.json
```

## Notes on IDs and labels

- The GUI labels look like `fly (1,1)`.
- The first number is the **assay tube index** (1 to 4).
- The second number is the **fly ID within that tube**.
- The furthest-left physical vial is ignored, so assay tube 1 corresponds to the
  **second** physical vial in the image.

## Tuning

If the assay detector is too permissive or too strict, adjust:

- `--min-area`
- `--max-area`
- `--min-threshold`
- `--inner-margin-px`

If the Brio channel detector needs adjustment, tune:

- `--score-thresh`
- `--band-half-width`

## Practical note

Identity tracking can be disrupted if multiple flies fully overlap in the same vial.
The reason the assay uses continuous video internally, while still saving one snapshot
per second, is to improve ID stability compared with only sampling once per second.
