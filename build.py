"""Build a standalone app for the CURRENT operating system.

    pip install -r requirements.txt pyinstaller
    python build.py                    # small build; downloads ffmpeg on first launch
    python build.py --bundle-ffmpeg    # also embeds ffmpeg (needs `python fetch_binaries.py` first; ~200 MB+)

Output: dist/MediaDownloader(.exe). Build on each OS you want to ship for.
The packaged app stores downloaded yt-dlp/ffmpeg updates in a per-user folder and has an
"Update yt-dlp & ffmpeg" button, so users never need to reinstall.
"""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sep = ";" if sys.platform.startswith("win") else ":"

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "MediaDownloader",
    "--collect-submodules", "yt_dlp",      # baseline yt-dlp inside the app (updates override it)
]
try:
    import yt_dlp_ejs  # noqa: F401  (YouTube's JavaScript solver scripts)
    cmd += ["--collect-all", "yt_dlp_ejs"]
except ImportError:
    print("Note: yt-dlp-ejs isn't installed, so the app will rely on the Update button for it.\n"
          "      Run `pip install -r requirements.txt` first to bundle it.")
if "--bundle-ffmpeg" in sys.argv:
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    if not (root / "bin" / exe).exists():
        sys.exit("Run `python fetch_binaries.py` first so ffmpeg can be bundled.")
    cmd += ["--add-data", f"{root / 'bin'}{sep}bin"]
cmd.append(str(root / "app.py"))

print(" ".join(cmd))
subprocess.check_call(cmd, cwd=root)
print("\nBuilt: dist/MediaDownloader" + (".exe" if sys.platform.startswith("win") else ""))
