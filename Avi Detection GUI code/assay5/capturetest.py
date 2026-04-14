#!/usr/bin/env python3
import datetime
import os
import pwd
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Optional

RPI_CAM_HELLO = "/usr/bin/rpicam-hello"
RPI_CAM_STILL = "/usr/bin/rpicam-still"

CAPTURE_KEY = " "
QUIT_KEY = "q"
PREVIEW_SETTLE_SECONDS = 1.0
MANAGE_DISPLAY_MANAGER = os.environ.get("MANAGE_DISPLAY_MANAGER", "1") != "0"

preview_proc = None
captured_count = 0


def owner_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


SAVE_DIR = Path(os.environ.get("CAPTURE_SAVE_DIR", owner_home() / "captures"))


def log(message: str = "") -> None:
    print(f"\r{message}", flush=True)


def require_binary(path: str) -> None:
    if not Path(path).exists():
        raise SystemExit(f"Missing required binary: {path}")


def run_display_manager(action: str) -> None:
    if os.geteuid() == 0:
        command = ["systemctl", action, "display-manager"]
    else:
        command = ["sudo", "systemctl", action, "display-manager"]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unknown systemctl error"
        raise RuntimeError(f"display-manager {action} failed: {error}")


def start_preview() -> None:
    global preview_proc

    if preview_proc and preview_proc.poll() is None:
        return

    preview_proc = subprocess.Popen(
        [RPI_CAM_HELLO, "-t", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(PREVIEW_SETTLE_SECONDS)

    if preview_proc.poll() is not None:
        error = preview_proc.stderr.read().strip() if preview_proc.stderr else ""
        preview_proc = None
        raise RuntimeError(error or "rpicam-hello exited immediately")


def stop_preview() -> None:
    global preview_proc

    if not preview_proc:
        return

    if preview_proc.poll() is None:
        preview_proc.send_signal(signal.SIGINT)
        try:
            preview_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            preview_proc.terminate()
            try:
                preview_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                preview_proc.kill()
                preview_proc.wait(timeout=2)

    preview_proc = None


def capture_image() -> Optional[Path]:
    global captured_count

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filepath = SAVE_DIR / f"capture_{timestamp}.jpg"

    stop_preview()

    result = subprocess.run(
        [
            RPI_CAM_STILL,
            "--immediate",
            "--nopreview",
            "-n",
            "--output",
            str(filepath),
        ],
        capture_output=True,
        text=True,
    )

    try:
        start_preview()
    except Exception as exc:
        log(f"[WARN] Capture saved, but preview did not restart: {exc}")

    if result.returncode != 0:
        error = result.stderr.strip().splitlines()
        tail = error[-1] if error else "unknown error"
        log(f"[ERROR] Capture failed: {tail}")
        return None

    captured_count += 1
    log(f"[{captured_count}] Saved {filepath}")
    return filepath


def getch() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> None:
    require_binary(RPI_CAM_HELLO)
    require_binary(RPI_CAM_STILL)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)

    managed_display = False

    try:
        if MANAGE_DISPLAY_MANAGER:
            log("[System] Stopping display manager...")
            run_display_manager("stop")
            managed_display = True
            time.sleep(1)

        log("[Camera] Starting preview on the connected Pi display...")
        start_preview()

        log("")
        log("-------------------------------------")
        log("  SPACE  -> save JPEG locally")
        log(f"  q      -> quit ({SAVE_DIR})")
        log("-------------------------------------")

        while True:
            key = getch()
            if key == QUIT_KEY:
                log("\n[Done] Quitting...")
                break
            if key == CAPTURE_KEY:
                capture_image()
    except KeyboardInterrupt:
        log("\n[Done] Interrupted.")
    finally:
        stop_preview()
        if managed_display:
            try:
                log("[System] Restarting display manager...")
                run_display_manager("start")
            except Exception as exc:
                log(f"[WARN] Could not restart display manager: {exc}")
        log(f"[Done] Saved {captured_count} capture(s) to {SAVE_DIR}")


if __name__ == "__main__":
    main()
