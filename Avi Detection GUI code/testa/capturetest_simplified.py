#!/usr/bin/env python3
import os
import sys
import time
import signal
import termios
import tty
import datetime
import subprocess

# Simple Raspberry Pi live-preview + capture script.
# Intended use:
#   1) Run this ON the Pi with a monitor attached.
#   2) Stop the desktop first if you want console/KMS preview:
#        sudo systemctl stop display-manager
#   3) Run the script:
#        sudo python3 capturetest_simplified.py
#   4) Press SPACE to save an image, q to quit.
#   5) Restore desktop later if needed:
#        sudo systemctl start display-manager

SAVE_DIR = "/home/team8/captures"
CAPTURE_KEY = " "
QUIT_KEY = "q"
PREVIEW_CMD = ["/usr/bin/rpicam-hello", "-t", "0"]
STILL_CMD_BASE = ["/usr/bin/rpicam-still"]

preview_proc = None
captured_count = 0


def log(msg: str) -> None:
    print(f"\r{msg}", flush=True)


def getch() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ensure_environment() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("Run this directly in a terminal on the Pi, not through redirected stdin.")

    if not os.path.exists(PREVIEW_CMD[0]):
        raise RuntimeError(f"Not found: {PREVIEW_CMD[0]}")

    if not os.path.exists(STILL_CMD_BASE[0]):
        raise RuntimeError(f"Not found: {STILL_CMD_BASE[0]}")

    os.makedirs(SAVE_DIR, exist_ok=True)



def start_preview() -> None:
    global preview_proc

    if preview_proc is not None and preview_proc.poll() is None:
        return

    preview_proc = subprocess.Popen(
        PREVIEW_CMD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    time.sleep(0.6)

    if preview_proc.poll() is not None:
        raise RuntimeError(
            "Preview failed to start. Make sure a monitor is attached and run this locally on the Pi. "
            "If you stopped the desktop, stay on the Pi console/TTY when launching the script."
        )



def stop_preview() -> None:
    global preview_proc

    if preview_proc is None:
        return

    if preview_proc.poll() is None:
        try:
            os.killpg(os.getpgid(preview_proc.pid), signal.SIGTERM)
            preview_proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(preview_proc.pid), signal.SIGKILL)
            except Exception:
                pass

    preview_proc = None
    time.sleep(0.25)



def capture_image() -> str | None:
    global captured_count

    stop_preview()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filepath = os.path.join(SAVE_DIR, f"capture_{timestamp}.jpg")

    cmd = STILL_CMD_BASE + [
        "-o", filepath,
        "-n",              # no preview during still capture
        "--immediate",     # capture right away
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    try:
        start_preview()
    except Exception as e:
        log(f"[WARN] Capture saved, but preview did not restart: {e}")

    if result.returncode != 0:
        err = result.stderr.strip().splitlines()
        msg = err[-1] if err else "unknown capture error"
        log(f"[ERROR] Capture failed: {msg}")
        return None

    captured_count += 1
    log(f"[{captured_count}] Saved: {filepath}")
    return filepath



def main() -> None:
    ensure_environment()

    log("Starting live preview...")
    start_preview()

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(" SPACE  -> capture image")
    print(" q      -> quit")
    print(f" save dir: {SAVE_DIR}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        while True:
            key = getch()
            if key == QUIT_KEY:
                log("[Done] Quitting...")
                break
            if key == CAPTURE_KEY:
                capture_image()
    except KeyboardInterrupt:
        log("[Done] Interrupted.")
    finally:
        stop_preview()
        log(f"[Done] Total captures: {captured_count}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
