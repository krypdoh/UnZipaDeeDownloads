import os
import time
import subprocess
import threading
import sys
import patoolib
from PIL import Image
from pystray import Icon, MenuItem, Menu
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Constants
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
WATCHED_FOLDERS = [DOWNLOADS_DIR]
ARCHIVE_EXTENSIONS = {".tar", ".7z", ".zip", ".gzip", ".rar"}

class ExtractHandler(FileSystemEventHandler):
    def __init__(self):
        self.enabled = True
        self.watched_paths = {}
    
    def on_created(self, event):
        if self.enabled and not event.is_directory and any(event.src_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            # Small delay to ensure file is fully written
            time.sleep(0.5)
            self.extract_archive(event.src_path)

    def on_moved(self, event):
        if self.enabled and not event.is_directory and any(event.dest_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            # Skip if the file was already inside a watched folder (internal reorganization)
            if any(event.src_path.startswith(folder) for folder in self.watched_paths):
                return
            time.sleep(0.5)
            self.extract_archive(event.dest_path)

    def extract_archive(self, archive_path):
        # Check if file still exists (browser may have moved it)
        if not os.path.exists(archive_path):
            print(f"File not found (may have been moved by browser): {archive_path}")
            return
        
        try:
            base_name = os.path.basename(archive_path)
            # Remove extension to get clean folder name
            name_without_ext = os.path.splitext(base_name)[0]
            # Create folder like "Extracted foldername"
            extract_dir = os.path.join(DOWNLOADS_DIR, f"Extracted {name_without_ext}")
            os.makedirs(extract_dir, exist_ok=True)
            patoolib.extract_archive(archive_path, outdir=extract_dir)
            print(f"Extracted {base_name} to {extract_dir}")
        except Exception as e:
            print(f"Error extracting {archive_path}: {e}")

def main():
    observer = Observer()
    event_handler = ExtractHandler()
    watched_paths = {}  # Track scheduled paths
    event_handler.watched_paths = watched_paths  # share reference so handler stays in sync
    stop_event = threading.Event()
    tray_icon = None

    # Load icon image
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
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
            capture_output=True, text=True
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
            capture_output=True, text=True
        )
        try:
            idx = int(result.stdout.strip()) - 1
            folder = list(watched_paths.keys())[idx]
            observer.unschedule(watched_paths[folder])
            del watched_paths[folder]
            print(f"Stopped watching: {folder}")
        except (ValueError, IndexError, KeyError):
            print("Invalid selection")
    
    def on_exit(icon, item):
        stop_event.set()
        observer.stop()
        icon.stop()

    def get_status_text(item):
        return "✓ Enabled" if event_handler.enabled else "  Disabled"

    tray_menu = Menu(
        MenuItem(get_status_text, toggle_enable),
        Menu.SEPARATOR,
        MenuItem("Open Downloads", open_downloads),
        MenuItem("Add Folder...", add_folder),
        MenuItem("Remove Folder...", remove_folder),
        Menu.SEPARATOR,
        MenuItem("Exit", on_exit)
    )
    tray_icon = Icon("UnZipaDeeDownloads", image, "UnZipaDeeDownloads", tray_menu)
    tray_icon.run_detached()

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