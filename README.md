# MediaDownloader (yt-dlp + ffmpeg GUI)

A desktop app/script for downloading video or audio with **yt-dlp**, using **ffmpeg** for merging and
converting. Supports MP4/MKV/WebM, MP3/M4A/Opus/FLAC/WAV, playlists, subtitles, embedded
metadata and thumbnails, live progress and cancel.

## For end users (you already have the packaged app(.exe))
Double-click `MediaDownloader` (`.exe` on Windows). That's it — no Python, yt-dlp, or ffmpeg to install.

- **First launch:** the app downloads ffmpeg automatically (one time, needs internet).
- **Updating:** if downloads stop working (sites change often), click **Update yt-dlp & ffmpeg**,
  then restart the app. Updates are stored in your user folder, so the app file itself never changes:
  - Windows `%APPDATA%\MediaDownloader`
  - macOS `~/Library/Application Support/MediaDownloader`
  - Linux `~/.local/share/MediaDownloader`

## This is for you looking at this repo that wants to build the packaged app (OPTIONAL), run the lines below in same folder (The next heading are instructions to run straight from a script if you dont want to do this)
```
pip install -r requirements.txt pyinstaller
python build.py                  # ~30 MB app; fetches ffmpeg on first launch
python build.py --bundle-ffmpeg  # optional: embed ffmpeg too (run `python fetch_binaries.py` FIRST; much larger, slower to start)
```
The result is `dist/MediaDownloader`. Build once per OS (Windows, macOS, Linux) — PyInstaller can't cross-compile.
The build contains a baseline yt-dlp; the Update button downloads the newest official release and it takes over on restart.
Rebuild only when *your own* code changes.

macOS note: unsigned apps trigger Gatekeeper warnings (right-click → Open); signing/notarizing needs an Apple developer account.
Windows note: unsigned executables may show SmartScreen warnings, and some antivirus tools flag PyInstaller apps.

## Running from source
Install Python 3.10+ if you dont have ut already from python.org (Windows: tick "Add Python to PATH"; Debian/Ubuntu also `sudo apt install python3-tk`), then:
```
python app.py
```
That's all. On first run the app downloads yt-dlp and ffmpeg into this folder automatically, and
the **Update yt-dlp & ffmpeg** button keeps them current. (`python update.py` does the same from a terminal.)

## Files
- `app.py` – tkinter GUI
- `downloader.py` – download engine (yt-dlp options, progress, cancel, ffmpeg lookup)
- `updater.py` – downloads/updates yt-dlp and ffmpeg, no pip required
- `build.py` – packages the app with PyInstaller
- `update.py`, `fetch_binaries.py` – command-line versions of the update step

Only download content you have the right to download.
