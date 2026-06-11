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
import time
import subprocess
import threading
import sys
import webbrowser
import tkinter as tk
import patoolib
from PIL import Image, ImageTk
from pystray import Icon, MenuItem, Menu
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Suppress console windows for ALL child processes (e.g. 7z/unrar spawned by patoolib).
# This is Windows-only but the whole app is Windows-only.
_orig_Popen = subprocess.Popen
class _Popen(_orig_Popen):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('creationflags', 0)
        kwargs['creationflags'] |= subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)
subprocess.Popen = _Popen

# Constants
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
WATCHED_FOLDERS = [DOWNLOADS_DIR]
ARCHIVE_EXTENSIONS = {".tar", ".7z", ".zip", ".gzip", ".rar"}

class ExtractHandler(FileSystemEventHandler):
    def __init__(self):
        self.enabled = True
        self.watched_paths = {}
        self.notifications_enabled = False
        self.tray_icon = None
    
    def on_created(self, event):
        if self.enabled and not event.is_directory and any(event.src_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            threading.Thread(target=self.extract_archive, args=(event.src_path,), daemon=True).start()

    def on_moved(self, event):
        if self.enabled and not event.is_directory and any(event.dest_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            # Only skip if the source itself was already an archive being moved within watched folders
            # (e.g. the user reorganising files).  Do NOT skip browser temp-file renames like
            # foo.zip.crdownload -> foo.zip, where src is not an archive.
            src_is_archive = any(event.src_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS)
            src_in_watched = any(event.src_path.startswith(folder) for folder in self.watched_paths)
            if src_is_archive and src_in_watched:
                return
            threading.Thread(target=self.extract_archive, args=(event.dest_path,), daemon=True).start()

    def _wait_for_file_ready(self, path, timeout=120, poll_interval=0.5):
        """Poll until the file exists, is non-empty, and its size has been
        stable for three consecutive checks (i.e. the browser has finished
        writing it).  Returns True when ready, False on timeout."""
        prev_size = -1
        stable_count = 0
        elapsed = 0
        while elapsed < timeout:
            if not os.path.exists(path):
                time.sleep(poll_interval)
                elapsed += poll_interval
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                time.sleep(poll_interval)
                elapsed += poll_interval
                continue
            if size > 0 and size == prev_size:
                stable_count += 1
                if stable_count >= 3:
                    return True
            else:
                stable_count = 0
            prev_size = size
            time.sleep(poll_interval)
            elapsed += poll_interval
        return False

    def extract_archive(self, archive_path):
        if not self._wait_for_file_ready(archive_path):
            print(f"File not ready or not found (timed out): {archive_path}")
            return

        extract_dir = None
        try:
            base_name = os.path.basename(archive_path)
            # Remove extension to get clean folder name
            name_without_ext = os.path.splitext(base_name)[0]
            # Create folder like "Extracted foldername"
            extract_dir = os.path.join(DOWNLOADS_DIR, f"Extracted {name_without_ext}")
            os.makedirs(extract_dir, exist_ok=True)
            patoolib.extract_archive(archive_path, outdir=extract_dir)
            print(f"Extracted {base_name} to {extract_dir}")
            if self.notifications_enabled and self.tray_icon is not None:
                try:
                    self.tray_icon.notify(f"Extracted: {base_name}", "UnZipaDeeDownloads")
                except Exception:
                    pass
        except Exception as e:
            print(f"Error extracting {archive_path}: {e}")
            # Remove the folder only if it ended up empty so the user
            # doesn't see a misleading empty "Extracted ..." directory.
            try:
                if extract_dir and os.path.isdir(extract_dir) and not os.listdir(extract_dir):
                    os.rmdir(extract_dir)
            except Exception:
                pass

def main():
    observer = Observer()
    event_handler = ExtractHandler()
    watched_paths = {}  # Track scheduled paths
    event_handler.watched_paths = watched_paths  # share reference so handler stays in sync
    stop_event = threading.Event()
    tray_icon = None

    # Load icon image — works both as a script and as a PyInstaller onefile exe
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "icon.png")
    try:
        image = Image.open(icon_path)
        print(f"Icon loaded successfully from {icon_path}")
    except Exception as e:
        print(f"Failed to load icon from {icon_path}: {e}")
        return 1

    def toggle_enable(icon, item):
        event_handler.enabled = not event_handler.enabled
        status = "enabled" if event_handler.enabled else "disabled"
        print(f"Auto-extraction {status}")
    
    def open_downloads(icon, item):
        subprocess.Popen(f'explorer "{DOWNLOADS_DIR}"')
    
    def add_folder(icon, item):
        # Open folder picker dialog
        result = subprocess.run(
            ['powershell', '-Command', 
             'Add-Type -AssemblyName System.Windows.Forms; '
             '$f = New-Object System.Windows.Forms.FolderBrowserDialog; '
             '$f.Description = "Select folder to watch for archives"; '
             'if ($f.ShowDialog() -eq "OK") { $f.SelectedPath }'],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        folder = result.stdout.strip()
        if folder and os.path.isdir(folder) and folder not in watched_paths:
            watch = observer.schedule(event_handler, path=folder, recursive=True)
            watched_paths[folder] = watch
            print(f"Now watching: {folder}")
    
    def remove_folder(icon, item):
        # Show list of watched folders to remove
        if not watched_paths:
            print("No additional folders being watched")
            return
        folders_list = '\n'.join([f'{i+1}. {f}' for i, f in enumerate(watched_paths.keys())])
        result = subprocess.run(
            ['powershell', '-Command',
             f'Add-Type -AssemblyName Microsoft.VisualBasic; '
             f'[Microsoft.VisualBasic.Interaction]::InputBox("Enter number to remove:\n{folders_list}", "Remove Folder")'],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            idx = int(result.stdout.strip()) - 1
            folder = list(watched_paths.keys())[idx]
            observer.unschedule(watched_paths[folder])
            del watched_paths[folder]
            print(f"Stopped watching: {folder}")
        except (ValueError, IndexError, KeyError):
            print("Invalid selection")
    
    def show_about(icon, item):
        def _open():
            win = tk.Tk()
            win.title("About UnZipaDeeDownloads")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            try:
                ico_path = os.path.join(base_dir, "icon.ico")
                win.iconbitmap(ico_path)
            except Exception:
                pass
            try:
                img = Image.open(os.path.join(base_dir, "icon.png")).resize((64, 64), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                lbl_img = tk.Label(win, image=photo)
                lbl_img.image = photo
                lbl_img.pack(pady=(16, 4))
            except Exception:
                pass
            tk.Label(win, text="UnZipaDeeDownloads", font=("Segoe UI", 14, "bold")).pack()
            tk.Label(win, text="Author:  Paul R. Charovkine", font=("Segoe UI", 10)).pack(pady=(10, 2))
            tk.Label(win, text="Copyright 2026 - All Rights Reserved", font=("Segoe UI", 10)).pack(pady=2)
            link = tk.Label(win, text="krypdoh.github.io/UnZipaDeeDownloads",
                            font=("Segoe UI", 10, "underline"), fg="blue", cursor="hand2")
            link.pack(pady=(2, 16))
            link.bind("<Button-1>", lambda e: webbrowser.open("https://krypdoh.github.io/UnZipaDeeDownloads"))
            tk.Button(win, text="OK", width=10, command=win.destroy).pack(pady=(0, 16))
            win.mainloop()
        threading.Thread(target=_open, daemon=True).start()

    def on_exit(icon, item):
        stop_event.set()
        observer.stop()
        icon.stop()

    def get_status_text(item):
        return "✓ Enabled" if event_handler.enabled else "  Disabled"

    def toggle_notifications(icon, item):
        event_handler.notifications_enabled = not event_handler.notifications_enabled
        status = "enabled" if event_handler.notifications_enabled else "disabled"
        print(f"Notifications {status}")

    def get_notification_text(item):
        return "✓ Notifications" if event_handler.notifications_enabled else "  Notifications"

    tray_menu = Menu(
        MenuItem(get_status_text, toggle_enable),
        MenuItem(get_notification_text, toggle_notifications),
        Menu.SEPARATOR,
        MenuItem("Open Downloads", open_downloads),
        MenuItem("Add Folder...", add_folder),
        MenuItem("Remove Folder...", remove_folder),
        Menu.SEPARATOR,
        MenuItem("About", show_about),
        MenuItem("Exit", on_exit)
    )
    tray_icon = Icon("UnZipaDeeDownloads", image, "UnZipaDeeDownloads", tray_menu)
    tray_icon.run_detached()
    event_handler.tray_icon = tray_icon

    # Watch initial folders
    for folder in WATCHED_FOLDERS:
        watch = observer.schedule(event_handler, path=folder, recursive=True)
        watched_paths[folder] = watch

    try:
        observer.start()
        print(f"Watching for archives in: {', '.join(watched_paths.keys())}")
        while not stop_event.is_set():
            time.sleep(0.25)
    except KeyboardInterrupt:
        stop_event.set()
        observer.stop()
    except Exception as e:
        print(f"Fatal error: {e}")
        stop_event.set()
        observer.stop()
        return 1
    finally:
        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass

    return 0

if __name__ == "__main__":
    sys.exit(main())