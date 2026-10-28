# MediaDownloader (yt-dlp + ffmpeg)

A desktop app/script for downloading video or audio with **yt-dlp**, using **ffmpeg** for merging and
converting. Supports MP4/MKV/WebM, MP3/M4A/Opus/FLAC/WAV, playlists, subtitles, embedded
metadata and thumbnails, optional start/end clipping, live progress and cancel.

## The window
MediaDownloader opens a modern web-style window (light or dark, following your system). It is served by a tiny
local server inside the app and shown in an app-style window using Edge or Chrome if one is installed
(otherwise a tab in your default browser). The server only accepts requests from that window (localhost only,
random per-run key). Closing the window quits the app. Prefer the old plain window? Run `python app.py --classic`.

## For end users (you already have the packaged app(.exe))
Double-click `MediaDownloader` (`.exe` on Windows). That's it — no Python, yt-dlp, or ffmpeg to install.

- **First launch:** the app downloads ffmpeg and Deno (a small JavaScript runtime YouTube needs) automatically (one time, needs internet).
- **Updating:** if downloads stop working (sites change often), click **Update yt-dlp & ffmpeg** (it also refreshes Deno),
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

## Clipping (optional)
Fill in **Start** and/or **End** (`90`, `1:30` or `1:02:30`) to download just part of a video or audio file;
leave them blank for the whole thing. The app checks the times against the video's length when the site
reports it (YouTube and most big sites do) and tells you if a time is past the end.

- **Frame-accurate** (on by default, video only) re-encodes around the cut points so picture and sound start
  together and stay in sync. It is slower; turn it off for a fast clip, but the video may then start a little
  before the audio. Audio-only clips ignore this option: the audio is downloaded in full and cut precisely
  while converting, so the clip starts exactly where you asked.
- Subtitles and chapters are trimmed to the clip and start at 0:00, so the clip's length and seeking are correct.
- Clipping is turned off for playlists, and clips get a `[clip 1m30s-2m45s]` suffix in the filename.
- Cover art ("Embed thumbnail") works for MP4, MKV, MP3 and M4A. For WebM, WAV, and FLAC/Opus it is skipped
  with a note in the log (those formats need extra software).

## YouTube says "Sign in to confirm you're not a bot"
YouTube sometimes blocks anonymous downloads. Under **Cookies** pick the browser you're signed in to YouTube with
(Firefox is the most reliable; Chrome-based browsers on Windows often can't be read), or choose **Use cookies.txt…**
to point at an exported cookies file. Your choice is remembered. Turning off a VPN can also help.
Note: heavy automated use of a logged-in account can get that account flagged by YouTube, so a spare account is safer.

## Running from source
Install Python 3.10+ if you don't have it already from python.org (Windows: tick "Add Python to PATH"; Debian/Ubuntu also `sudo apt install python3-tk`), then:
```
python app.py
```
That's all. On first run the app downloads yt-dlp and ffmpeg into this folder automatically, and
the **Update yt-dlp & ffmpeg** button keeps them current. (`python update.py` does the same from a terminal.)

## Files
- `app.py` – starts the app (web window by default, `--classic` for the tkinter window)
- `webui.py` – local server that connects the web page to the Python code
- `web/index.html` – the interface (one file: HTML, CSS and JavaScript)
- `app_tk.py` – the older tkinter window
- `downloader.py` – download engine (yt-dlp options, progress, cancel, ffmpeg lookup)
- `updater.py` – downloads/updates yt-dlp, ffmpeg and Deno, no pip required
- `build.py` – packages the app with PyInstaller
- `update.py`, `fetch_binaries.py` – command-line versions of the update step

Only download content you have the right to download.
