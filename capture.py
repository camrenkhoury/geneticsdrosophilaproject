#!/usr/bin/env python3
import sys
import os
import subprocess
import datetime
import termios
import tty
import threading
import queue
import time
import json

sys.path.insert(0, '/home/team8/.local/lib/python3.13/site-packages')
from box_sdk_gen import (
    BoxOAuth, OAuthConfig, BoxClient,
    UploadFileAttributes, UploadFileAttributesParentField
)

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID       = 'k1hxdppdrmp3rm8vqcbb66wpf0ut3iyv'
CLIENT_SECRET   = 'IJ4pOmk3t3wWyxL0wYSdoZoyAXGqfp15'
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'box_tokens.json')
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'captures')
BOX_FOLDER_NAME = 'pi_captures'
BOX_PARENT_FOLDER_ID = '366684356655'
CAPTURE_KEY     = ' '   # Spacebar to capture
QUIT_KEY        = 'q'
# ─────────────────────────────────────────────────────────────────────────────

upload_queue   = queue.Queue()
captured_count = 0
uploaded_count = 0
preview_proc   = None

def log(msg):
    print(f"\r{msg}", flush=True)

# ── Token persistence ─────────────────────────────────────────────────────────
def load_tokens():
    with open(TOKENS_FILE, 'r') as f:
        return json.load(f)

def save_tokens(access_token, refresh_token):
    with open(TOKENS_FILE, 'w') as f:
        json.dump({'access_token': access_token, 'refresh_token': refresh_token}, f)

def get_box_client():
    """Create a Box client using saved tokens with auto-refresh."""
    tokens = load_tokens()
    config = OAuthConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    oauth  = BoxOAuth(config)

    # Inject saved tokens
    from box_sdk_gen import AccessToken
    token_obj = AccessToken(access_token=tokens['access_token'], refresh_token=tokens['refresh_token'])
    oauth.token_storage.store(token_obj)
    client = BoxClient(oauth)

    # Hook into token refresh to save new tokens automatically
    original_refresh = oauth.refresh_token
    def refresh_and_save(*args, **kwargs):
        new_tokens = original_refresh(*args, **kwargs)
        save_tokens(new_tokens.access_token, new_tokens.refresh_token)
        log("[Box] Token refreshed and saved.")
        return new_tokens
    oauth.refresh_token = refresh_and_save

    return client

# ── Preview ───────────────────────────────────────────────────────────────────
def start_preview():
    global preview_proc
    preview_proc = subprocess.Popen([
        '/usr/bin/rpicam-vid',
        '--timeout', '0',
        '--width', '1280',
        '--height', '1280',
        '--codec', 'yuv420',
        '-o', '/dev/null',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)

def stop_preview():
    global preview_proc
    if preview_proc:
        preview_proc.terminate()
        try:
            preview_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            preview_proc.kill()
        preview_proc = None
    subprocess.run(['sudo', 'fuser', '-k', '/dev/media0'], capture_output=True)
    subprocess.run(['sudo', 'fuser', '-k', '/dev/media2'], capture_output=True)
    time.sleep(0.1)

# ── Capture ───────────────────────────────────────────────────────────────────
def capture_image():
    global captured_count
    stop_preview()

    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    filepath  = os.path.join(SAVE_DIR, f'capture_{timestamp}.jpg')

    result = subprocess.run([
        '/usr/bin/rpicam-still',
        '--output', filepath,
        '--zsl',
        # '--awbgains', '3.0,0.9',
        '--nopreview',
        '-n',
        '--width', '2028',
        '--height', '1520',
    ], capture_output=True, text=True)

    start_preview()

    if result.returncode != 0:
        log(f"[ERROR] Capture failed: {result.stderr.splitlines()[-1] if result.stderr.strip() else 'unknown'}")
        return None

    captured_count += 1
    now = datetime.datetime.now().strftime('%H:%M:%S')
    log(f"[{captured_count}] Captured @ {now} — queued for upload | Uploaded: {uploaded_count}")
    return filepath

# ── Box uploader (background thread) ─────────────────────────────────────────
def box_uploader(client, folder_id):
    global uploaded_count
    while True:
        filepath = upload_queue.get()
        if filepath is None:
            break
        try:
            filename = os.path.basename(filepath)
            with open(filepath, 'rb') as f:
                attrs = UploadFileAttributes(
                    name=filename,
                    parent=UploadFileAttributesParentField(id=folder_id)
                )
                client.uploads.upload_file(attrs, f)
            uploaded_count += 1
            log(f"  ↑ Uploaded: {filename} ({uploaded_count}/{captured_count})")
        except Exception as e:
            log(f"  ✗ Upload failed: {os.path.basename(filepath)} — {e}")
        upload_queue.task_done()

def get_or_create_box_folder(client, folder_name):
    items = client.folders.get_folder_items(BOX_PARENT_FOLDER_ID)
    for item in items.entries:
        if item.name == folder_name:
            log(f"[Box] Using folder '{folder_name}' (id={item.id})")
            return item.id
    folder = client.folders.create_folder(
        folder_name,
        UploadFileAttributesParentField(id=BOX_PARENT_FOLDER_ID)
    )
    log(f"[Box] Created folder '{folder_name}' (id={folder.id})")
    return folder.id

# ── Input ─────────────────────────────────────────────────────────────────────
def getch():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ── Display manager ───────────────────────────────────────────────────────────
def stop_lightdm():
    log("[System] Stopping display manager...")
    subprocess.run(['sudo', 'systemctl', 'stop', 'display-manager'], capture_output=True)
    time.sleep(0.5)

def start_lightdm():
    log("[System] Restarting display manager...")
    subprocess.run(['sudo', 'systemctl', 'start', 'display-manager'], capture_output=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs(SAVE_DIR, exist_ok=True)

    log("[Box] Connecting...")
    client    = get_box_client()
    me        = client.users.get_user_me()
    log(f"[Box] Connected as: {me.name}")
    folder_id = get_or_create_box_folder(client, BOX_FOLDER_NAME)

    uploader = threading.Thread(target=box_uploader, args=(client, folder_id), daemon=True)
    uploader.start()

    stop_lightdm()
    log("[Camera] Starting preview...")
    start_preview()

    log("")
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log("  SPACE  →  capture + upload to Box")
    log("  q      →  quit")
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        while True:
            key = getch()
            if key == QUIT_KEY:
                log("\n[Done] Quitting...")
                break
            if key == CAPTURE_KEY:
                filepath = capture_image()
                if filepath:
                    upload_queue.put(filepath)
    except KeyboardInterrupt:
        log("\n[Done] Interrupted.")
    finally:
        stop_preview()
        pending = upload_queue.qsize()
        if pending:
            log(f"[Box] Waiting for {pending} pending uploads...")
        upload_queue.put(None)
        uploader.join()
        log(f"[Done] Captured: {captured_count} | Uploaded: {uploaded_count}")
        start_lightdm()

if __name__ == '__main__':
    main()
