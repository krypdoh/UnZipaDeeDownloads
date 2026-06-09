# UnZipaDeeDownloads

Automatically watches your Downloads folder (and optional extra folders) for archive files and extracts each archive into its own folder.

## What it does

- Watches `~/Downloads` by default
- Detects new archives with these extensions:
  - `.zip`
  - `.7z`
  - `.rar`
  - `.tar`
  - `.gzip`
- Extracts each archive to a folder named:
  - `Extracted <archive-name-without-extension>`
- Runs as a tray app with controls to:
  - Enable/disable auto-extraction
  - Open Downloads
  - Add/remove watched folders
  - Exit cleanly

## Requirements

- Windows
- Python 3.10+
- Python packages:
  - `watchdog`
  - `pystray`
  - `pillow`
  - `patool`
- Archive backend tools available on `PATH` for the formats you use
  - Example: `7z` for `.7z` and many `.zip` files
  - `unrar` for some `.rar` files

## Install

```powershell
cd C:\Users\prc\Dropbox\github\UnZipaDeeDownloads
python -m pip install watchdog pystray pillow patool
```

If extraction fails for some formats, install 7-Zip and make sure `7z.exe` is available in `PATH`.

## Run

```powershell
cd C:\Users\prc\Dropbox\github\UnZipaDeeDownloads
python unzipadeedownloads.py
```

Expected startup output:

- Icon loaded successfully...
- Watching for archives in: ...

## Output folder behavior

If `openscreen-main.zip` is downloaded, extraction target is:

- `C:\Users\<you>\Downloads\Extracted openscreen-main`

If a folder already exists, extraction reuses that folder.

## Tray menu behavior

- `✓ Enabled` / `Disabled`: toggles auto extraction
- `Open Downloads`: opens the Downloads folder in Explorer
- `Add Folder...`: opens a folder picker and starts watching selected folder
- `Remove Folder...`: prompts for a numbered watched folder to remove
- `Exit`: stops watcher and closes tray icon

## Troubleshooting

### File not found during extraction

Sometimes browsers create a temp file and rename/move it quickly. The script:

- waits briefly before extracting
- checks file existence before extraction

If this still happens often in your browser:

- try downloading to a non-synced local folder
- keep `Downloads` local (not redirected through cloud-sync tools)

### Extraction command errors

`patool` needs external tools. Install 7-Zip and/or unrar, then retry.

### Tray icon not showing

Make sure `icon.png` exists in the project directory and you are running in a desktop session (not headless/remote without tray support).
