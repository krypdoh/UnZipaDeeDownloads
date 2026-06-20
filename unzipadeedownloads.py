#!/usr/bin/env python3
# =============================================================================
# UnZipaDeeDownloads
# =============================================================================
# Description : Watches ~/Downloads (and optional extra folders) for new
#               archive files and automatically extracts each one into its
#               own named subfolder ("Extracted <name>").  Runs as a Windows
#               system-tray application with controls to enable/disable
#               auto-extraction, open Downloads, add/remove watched folders,
#               and exit cleanly.
#
# Author      : Paul R. Charovkine - 2026 
# Version     : 1.0.4
# Date        : 2024-06-20
# 
# Supported formats : .zip  .7z  .rar  .tar  .gzip
#
# Dependencies: watchdog, pystray, Pillow, patool
#               External tools on PATH for some formats (e.g. 7z, unrar)
#
# Platform    : Windows
# Requires    : Python 3.10+
#
# License     : MIT
# =============================================================================

import os
import sys
import time
import json
import threading
import subprocess
import traceback
import logging
import shutil
from pathlib import Path
from typing import Set

import zipfile
import tarfile
import queue

# GUI/tray libs (import errors handled so script can run headless for diagnostics)
try:
    from PIL import Image
    from pystray import Icon, MenuItem, Menu
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    from win11toast import toast
except Exception:
    Image = None
    Icon = None
    MenuItem = None
    Menu = None
    Observer = None
    FileSystemEventHandler = object
    def toast(*a, **k): pass

# -------------------------------------------------------------------------
# PyInstaller-safe picker entry
# If the executable is launched with --picker, run only the folder picker
# and exit immediately. This prevents spawning a full second tray icon.
# -------------------------------------------------------------------------
def run_picker_and_exit(timeout_seconds=60):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title='Select folder to watch for archives')
        root.destroy()
        if folder:
            print(folder, flush=True)
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

if "--picker" in sys.argv:
    run_picker_and_exit()

# -------------------------------------------------------------------------
# Constants / Paths
# -------------------------------------------------------------------------
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
ARCHIVE_EXTENSIONS = {".tar", ".7z", ".zip", ".gzip", ".gz", ".rar", ".tgz", ".tar.gz"}
TEMP_EXTENSIONS = {".crdownload", ".download", ".part"}

APP_NAME = "UnZipaDeeDownloads"
APPDATA_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), APP_NAME)
WATCHED_JSON = os.path.join(APPDATA_DIR, "watched.json")
LOG_PATH = os.path.join(APPDATA_DIR, "unzipadee.log")

# -------------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------------
def setup_logging():
    os.makedirs(APPDATA_DIR, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    if not logger.handlers:
        logger.addHandler(ch)
        logger.addHandler(fh)
    return logger

logger = setup_logging()

# -------------------------------------------------------------------------
# Toast helper (suppress return value noise)
# -------------------------------------------------------------------------
def safe_toast(title, msg, duration=None):
    try:
        if duration is None:
            _ = toast(title, msg)
        else:
            _ = toast(title, msg, duration=duration)
    except Exception:
        logger.exception("toast failed")

# -------------------------------------------------------------------------
# Helpers: persistence, file checks
# -------------------------------------------------------------------------
def ensure_appdata():
    os.makedirs(APPDATA_DIR, exist_ok=True)

def load_persisted_folders() -> Set[str]:
    ensure_appdata()
    if not os.path.exists(WATCHED_JSON):
        return set()
    try:
        with open(WATCHED_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            valid = {os.path.normpath(p) for p in data if os.path.isdir(p)}
            logger.debug("Loaded persisted folders: %s", valid)
            return valid
    except Exception:
        logger.exception("Failed to load persisted folders")
    return set()

def save_persisted_folders(folders: Set[str]):
    ensure_appdata()
    try:
        with open(WATCHED_JSON, "w", encoding="utf-8") as f:
            json.dump(sorted(list(folders)), f, indent=2)
        logger.debug("Saved persisted folders: %s", sorted(list(folders)))
    except Exception:
        logger.exception("Failed to save persisted folders")

def wait_until_fully_written(path, timeout=15, interval=0.25):
    """
    Shorter timeout and a quick read test. Returns True if file appears stable/readable.
    """
    start = time.time()
    last_size = -1
    while time.time() - start < timeout:
        if not os.path.exists(path):
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        try:
            with open(path, "rb") as f:
                f.read(1)
        except Exception:
            pass
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(interval)
    return False

# -------------------------------------------------------------------------
# Extraction helpers (no frozen-exe spawn)
# -------------------------------------------------------------------------
def extract_zip(archive_path, out_dir):
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(out_dir)

def extract_tar(archive_path, out_dir):
    try:
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(out_dir)
    except tarfile.ReadError:
        # handle single-file .gz (not tar.gz)
        if archive_path.lower().endswith(".gz") and not archive_path.lower().endswith((".tar.gz", ".tgz")):
            import gzip, shutil as _shutil
            base = os.path.basename(archive_path)
            name_no_gz = base[:-3] if base.endswith(".gz") else base + ".out"
            out_path = os.path.join(out_dir, name_no_gz)
            with gzip.open(archive_path, "rb") as f_in, open(out_path, "wb") as f_out:
                _shutil.copyfileobj(f_in, f_out)
        else:
            raise

def extract_with_7z(archive_path, out_dir):
    seven = shutil.which("7z") or shutil.which("7z.exe") or shutil.which(r"C:\ProgramData\chocolatey\bin\7z.exe")
    if not seven:
        raise FileNotFoundError("7z executable not found on PATH")
    cmd = [seven, "x", "-y", f"-o{out_dir}", archive_path]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=creationflags, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"7z failed: rc={proc.returncode} stderr={proc.stderr}")
    return proc

# -------------------------------------------------------------------------
# Watchdog Handler with robust extraction
# -------------------------------------------------------------------------
class ExtractHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.enabled = True
        self.notifications_enabled = True
        self.app_ref = None

    def notify(self, title, msg):
        if self.notifications_enabled:
            safe_toast(title, msg)

    def on_created(self, event):
        logger.debug("on_created event: is_dir=%s src=%s", event.is_directory, getattr(event, "src_path", None))
        if event.is_directory or not self.enabled:
            return
        ext = os.path.splitext(event.src_path)[1].lower()
        if ext in TEMP_EXTENSIONS:
            logger.debug("Ignoring temp extension for %s", event.src_path)
            return
        if ext in ARCHIVE_EXTENSIONS:
            logger.info("Archive detected (created): %s", event.src_path)
            threading.Thread(target=self.extract_archive, args=(event.src_path,), daemon=True).start()

    def on_moved(self, event):
        logger.debug("on_moved event: is_dir=%s dest=%s", event.is_directory, getattr(event, "dest_path", None))
        if event.is_directory or not self.enabled:
            return
        ext = os.path.splitext(event.dest_path)[1].lower()
        if ext in TEMP_EXTENSIONS:
            logger.debug("Ignoring temp extension for %s", event.dest_path)
            return
        if ext in ARCHIVE_EXTENSIONS:
            logger.info("Archive detected (moved): %s", event.dest_path)
            threading.Thread(target=self.extract_archive, args=(event.dest_path,), daemon=True).start()

    def extract_archive(self, archive_path):
        try:
            parent = os.path.dirname(archive_path) or ""
            if os.path.basename(parent).startswith("Extracted "):
                logger.debug("Skipping archive inside Extracted folder: %s", archive_path)
                return

            app = getattr(self, "app_ref", None)
            if app is not None:
                with app._extract_lock:
                    if archive_path in app._extracting:
                        logger.debug("Already extracting (debounced): %s", archive_path)
                        return
                    app._extracting.add(archive_path)

            def _do_extract():
                try:
                    if not os.path.exists(archive_path):
                        logger.warning("File disappeared before extraction: %s", archive_path)
                        return

                    base = os.path.basename(archive_path)
                    logger.info("Preparing to extract: %s", archive_path)

                    if not wait_until_fully_written(archive_path, timeout=15):
                        logger.warning("File did not stabilize quickly: %s", archive_path)
                        try:
                            with open(archive_path, "rb") as f:
                                f.read(1)
                        except Exception:
                            logger.warning("File not readable yet, skipping: %s", archive_path)
                            return

                    archive_parent = os.path.dirname(archive_path) or DOWNLOADS_DIR
                    name_no_ext = os.path.splitext(base)[0]
                    out_dir = os.path.join(archive_parent, f"Extracted {name_no_ext}")
                    os.makedirs(out_dir, exist_ok=True)

                    ext = os.path.splitext(archive_path)[1].lower()

                    # ZIP
                    if ext == ".zip":
                        try:
                            logger.info("Using zipfile for %s", archive_path)
                            extract_zip(archive_path, out_dir)
                            logger.info("zipfile extraction succeeded: %s -> %s", archive_path, out_dir)
                            self.notify(APP_NAME, f"Extracted: {base}")
                            return
                        except Exception:
                            logger.exception("zipfile extraction failed for %s", archive_path)

                    # TAR family
                    if ext in (".tar", ".tgz", ".tar.gz", ".gz"):
                        try:
                            logger.info("Using tarfile for %s", archive_path)
                            extract_tar(archive_path, out_dir)
                            logger.info("tarfile extraction succeeded: %s -> %s", archive_path, out_dir)
                            self.notify(APP_NAME, f"Extracted: {base}")
                            return
                        except Exception:
                            logger.exception("tarfile extraction failed for %s", archive_path)

                    # 7z / rar
                    if ext in (".7z", ".rar"):
                        try:
                            logger.info("Trying 7z for %s", archive_path)
                            extract_with_7z(archive_path, out_dir)
                            logger.info("7z extraction succeeded: %s -> %s", archive_path, out_dir)
                            self.notify(APP_NAME, f"Extracted: {base}")
                            return
                        except FileNotFoundError:
                            logger.warning("7z not found; cannot extract %s", archive_path)
                        except Exception:
                            logger.exception("7z extraction failed for %s", archive_path)

                    logger.error("No extractor succeeded for %s; ensure 7z is installed for .7z/.rar", archive_path)
                    self.notify(APP_NAME, f"Error extracting: {base}")

                finally:
                    if app is not None:
                        with app._extract_lock:
                            app._extracting.discard(archive_path)

            def _worker():
                sem = app._max_concurrent_extractions if app is not None else threading.Semaphore(2)
                with sem:
                    _do_extract()

            threading.Thread(target=_worker, daemon=True).start()

        except Exception:
            logger.exception("Unexpected error in extract_archive for %s", archive_path)

# -------------------------------------------------------------------------
# Main App
# -------------------------------------------------------------------------
class UnZipaDeeApp:
    def __init__(self):
        self.observer = Observer() if Observer is not None else None
        self.handler = ExtractHandler()
        self.handler.app_ref = self
        self.stop_event = threading.Event()
        self.tray_icon = None

        self.lock = threading.Lock()
        persisted = load_persisted_folders()
        self.watched_folders = {os.path.normpath(DOWNLOADS_DIR)} | persisted

        self._menu_update_queue = queue.Queue()
        self._menu_update_lock = threading.Lock()
        self._menu_updater_thread = None

        self._extracting = set()
        self._extract_lock = threading.Lock()
        self._max_concurrent_extractions = threading.Semaphore(2)

    # -------------------------
    # Menu updater (runs in background)
    # -------------------------
    def _start_menu_updater(self):
        if self._menu_updater_thread and self._menu_updater_thread.is_alive():
            return
        def updater():
            while not self.stop_event.is_set():
                try:
                    try:
                        self._menu_update_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    with self._menu_update_lock:
                        if self.tray_icon:
                            try:
                                self.tray_icon.menu = self.build_menu()
                                try:
                                    self.tray_icon.update_menu()
                                except Exception:
                                    pass
                            except Exception:
                                logger.exception("Menu updater failed to update tray menu")
                except Exception:
                    logger.exception("Menu updater unexpected error")
            logger.debug("Menu updater exiting")
        t = threading.Thread(target=updater, daemon=True)
        self._menu_updater_thread = t
        t.start()

    def _request_menu_update(self):
        try:
            self._menu_update_queue.put_nowait(1)
        except Exception:
            logger.exception("Failed to enqueue menu update")

    # -------------------------
    # Watch Management (non-blocking swap)
    # -------------------------
    def reschedule_all(self):
        if self.observer is None:
            logger.debug("Observer not available in this environment")
            return
        with self.lock:
            try:
                try:
                    self.observer.unschedule_all()
                    for folder in sorted(self.watched_folders):
                        self.observer.schedule(self.handler, folder, recursive=True)
                        logger.info("Watching: %s", folder)
                    return
                except Exception:
                    logger.debug("unschedule_all unavailable; performing non-blocking observer swap")

                new_observer = Observer()
                for folder in sorted(self.watched_folders):
                    try:
                        new_observer.schedule(self.handler, folder, recursive=True)
                        logger.info("Watching (new observer): %s", folder)
                    except Exception:
                        logger.exception("Failed to schedule watch for %s on new observer", folder)

                try:
                    new_observer.start()
                except Exception:
                    logger.exception("Failed to start new observer")
                    try:
                        new_observer.stop()
                    except Exception:
                        pass
                    return

                old_observer = self.observer
                self.observer = new_observer

                def stop_old(obs):
                    try:
                        obs.stop()
                        obs.join(timeout=2)
                    except Exception:
                        logger.exception("Error stopping old observer")

                threading.Thread(target=stop_old, args=(old_observer,), daemon=True).start()

            except Exception:
                logger.exception("reschedule_all failed")

    def add_folder(self, folder):
        folder = os.path.normpath(folder)
        if not os.path.isdir(folder):
            logger.warning("add_folder: not a directory: %s", folder)
            return False
        with self.lock:
            if folder in self.watched_folders:
                logger.debug("add_folder: already watched: %s", folder)
                return False
            self.watched_folders.add(folder)
            save_persisted_folders(self.watched_folders - {os.path.normpath(DOWNLOADS_DIR)})
            logger.info("Added folder to watched set: %s", folder)
            if self.observer is not None:
                try:
                    self.observer.schedule(self.handler, folder, recursive=True)
                    logger.info("Scheduled observer for new folder: %s", folder)
                except Exception:
                    logger.exception("Failed to schedule new folder on observer: %s", folder)
                try:
                    if not self.observer.is_alive():
                        logger.debug("Observer not alive; starting observer")
                        self.observer.start()
                except Exception:
                    logger.exception("Failed to start observer after adding folder")
        self._request_menu_update()
        return True

    def remove_folder(self, folder):
        folder = os.path.normpath(folder)
        if folder == os.path.normpath(DOWNLOADS_DIR):
            logger.debug("Attempt to remove Downloads ignored")
            return False
        with self.lock:
            if folder not in self.watched_folders:
                logger.debug("remove_folder: not watched: %s", folder)
                return False
            self.watched_folders.remove(folder)
            save_persisted_folders(self.watched_folders - {os.path.normpath(DOWNLOADS_DIR)})
            logger.info("Removed folder from watched set: %s", folder)
            if self.observer is not None:
                threading.Thread(target=self.reschedule_all, daemon=True).start()
        self._request_menu_update()
        return True

    # -------------------------
    # Folder Picker (spawn exe with --picker)
    # -------------------------
    def spawn_folder_picker(self, timeout_seconds=60):
        cmd = [sys.executable, "--picker"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            logger.debug("Spawning picker child: %s", cmd)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=creationflags,
                timeout=timeout_seconds
            )
            logger.debug("Picker returncode=%s stdout=%r stderr=%r", proc.returncode, proc.stdout, proc.stderr)
            if proc.returncode != 0:
                logger.warning("Picker exited non-zero: %s; stderr: %s", proc.returncode, proc.stderr.strip())
            out = proc.stdout.strip()
            return out if out else None
        except subprocess.TimeoutExpired:
            logger.warning("Picker timed out after %s seconds", timeout_seconds)
            return None
        except Exception:
            logger.exception("spawn_folder_picker failed")
            return None

    # -------------------------
    # Tray Menu Actions
    # -------------------------
    def _menu_add_folder(self):
        def worker():
            logger.debug("Add folder worker started")
            folder = self.spawn_folder_picker()
            logger.debug("spawn_folder_picker returned: %r", folder)
            if folder:
                if self.add_folder(folder):
                    msg = f"Now watching: {folder}"
                    print(msg)
                    logger.info(msg)
                    self.handler.notify(APP_NAME, f"Now watching: {os.path.basename(folder)}")
                else:
                    msg = f"Folder already watched or invalid: {folder}"
                    print(msg)
                    logger.info(msg)
            else:
                logger.info("No folder selected or picker failed")
        threading.Thread(target=worker, daemon=True).start()

    def _menu_remove_folder(self, folder):
        if self.remove_folder(folder):
            msg = f"Stopped watching: {folder}"
            print(msg)
            logger.info(msg)
            self.handler.notify(APP_NAME, f"Stopped watching: {os.path.basename(folder)}")

    def _menu_toggle_enabled(self):
        self.handler.enabled = not self.handler.enabled
        logger.info("Auto-extraction %s", "enabled" if self.handler.enabled else "disabled")
        self._request_menu_update()

    def _menu_toggle_notifications(self):
        self.handler.notifications_enabled = not self.handler.notifications_enabled
        logger.info("Notifications %s", "enabled" if self.handler.notifications_enabled else "disabled")
        self._request_menu_update()

    def _menu_open_downloads(self):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            subprocess.Popen(["explorer", DOWNLOADS_DIR], creationflags=creationflags)
        except Exception:
            logger.exception("Failed to open Downloads")

    def _menu_exit(self):
        logger.info("Exit requested from tray")
        self.stop_event.set()
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            logger.exception("Failed to stop tray icon")
        def stop_obs(obs):
            try:
                if obs is not None:
                    obs.stop()
                    obs.join(timeout=2)
            except Exception:
                logger.exception("Error stopping observer on exit")
        threading.Thread(target=stop_obs, args=(self.observer,), daemon=True).start()

    # -------------------------
    # Menu Builder
    # -------------------------
    def build_menu(self):
        def status_text(item):
            return "✓ Enabled" if self.handler.enabled else "  Disabled"
        def notify_text(item):
            return "✓ Show Notifications" if self.handler.notifications_enabled else "  Show Notifications"

        folder_items = []
        for folder in sorted(self.watched_folders):
            if os.path.normcase(folder) == os.path.normcase(os.path.normpath(DOWNLOADS_DIR)):
                folder_items.append(MenuItem(f"{folder} (Downloads)", None, enabled=False))
            else:
                def make_action(f):
                    def action(icon, item):
                        threading.Thread(target=lambda: self.remove_folder(f), daemon=True).start()
                    return action
                folder_items.append(MenuItem(folder, make_action(folder)))

        return Menu(
            MenuItem(status_text, lambda i, it: self._menu_toggle_enabled()),
            MenuItem(notify_text, lambda i, it: self._menu_toggle_notifications()),
            Menu.SEPARATOR,
            MenuItem("Open Downloads", lambda i, it: self._menu_open_downloads()),
            MenuItem("Add Folder...", lambda i, it: self._menu_add_folder()),
            MenuItem("Watched Folders", Menu(*folder_items)),
            Menu.SEPARATOR,
            MenuItem("Exit", lambda i, it: self._menu_exit())
        )

    # -------------------------
    # Run
    # -------------------------
    def run(self):
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base_dir, "icon.png")
        try:
            image = Image.open(icon_path) if Image is not None else None
        except Exception as e:
            logger.exception("Failed to load icon: %s", e)
            image = None

        self._start_menu_updater()
        self.reschedule_all()
        try:
            if self.observer is not None and not self.observer.is_alive():
                self.observer.start()
        except Exception:
            logger.exception("Failed to start observer")

        if Icon is not None and image is not None:
            self.tray_icon = Icon(APP_NAME, image, APP_NAME, self.build_menu())
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        else:
            logger.info("pystray or PIL not available; running headless")

        logger.info("Watching for archives in: %s", ", ".join(sorted(self.watched_folders)))
        print("Watching for archives in:")
        for f in sorted(self.watched_folders):
            print("  ", f)

        try:
            while not self.stop_event.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            self.stop_event.set()
        finally:
            try:
                if self.observer is not None and self.observer.is_alive():
                    self.observer.stop()
                    self.observer.join(timeout=5)
            except Exception:
                logger.exception("Error stopping observer on shutdown")
            try:
                if self.tray_icon is not None:
                    self.tray_icon.stop()
            except Exception:
                logger.exception("Error stopping tray icon on shutdown")
        return 0

# -------------------------------------------------------------------------
def main():
    return UnZipaDeeApp().run()

if __name__ == "__main__":
    sys.exit(main())
