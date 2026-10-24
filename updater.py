"""Finding, downloading and updating yt-dlp and ffmpeg. No GUI, no pip needed.

Where things live (the "data dir"):
  * packaged .exe/.app : a per-user folder (APPDATA / Application Support / ~/.local/share)
  * running from source: the project folder itself

  <data dir>/bin/ffmpeg(.exe), ffprobe(.exe)   latest ffmpeg
  <data dir>/bin/deno(.exe)                    JavaScript runtime YouTube needs
  <data dir>/yt-dlp.pyz                        latest yt-dlp (official single-file release)

IMPORTANT: this module must not import yt_dlp at module level.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
import zipimport
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "MediaDownloader"

# progress callback: (percent 0-100 or -1 if unknown, message)
Progress = Optional[Callable[[float, str], None]]

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"


# ---------------------------------------------------------------- locations

def data_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def bin_dir() -> Path:
    return data_dir() / "bin"


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform.startswith("win") else "")


# ------------------------------------------------------------ yt-dlp override

def activate_ytdlp_override() -> Optional[Path]:
    """If a downloaded yt-dlp.pyz exists, make `import yt_dlp` use it.

    Must be called before the first `import yt_dlp`. It takes priority over any
    copy bundled inside a PyInstaller build or installed with pip.
    """
    d = data_dir()
    current, pending = d / "yt-dlp.pyz", d / "yt-dlp.pyz.new"

    # A fresh download is staged as .new (the old file may be locked on Windows
    # while the app runs); swap it in at startup.
    try:
        if pending.exists():
            os.replace(pending, current)
    except OSError:
        pass

    if not current.exists() or not zipfile.is_zipfile(current):
        return None
    try:
        importer = zipimport.zipimporter(str(current))
    except zipimport.ZipImportError:
        return None

    class _Finder:
        @staticmethod
        def find_spec(name, path=None, target=None):
            # yt_dlp_ejs holds YouTube's JavaScript solver scripts and ships inside the zip too
            if name in ("yt_dlp", "yt_dlp_ejs") or name.startswith(("yt_dlp.", "yt_dlp_ejs.")):
                return importer.find_spec(name)
            return None

    sys.meta_path.insert(0, _Finder)
    return current


# ----------------------------------------------------------------- downloads

def _download(url: str, dest: Path, progress: Progress, label: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                if total:
                    progress(done / total * 100,
                             f"{label}: {done / 1048576:.0f} / {total / 1048576:.0f} MB")
                else:
                    progress(-1, f"{label}: {done / 1048576:.0f} MB")


def update_ytdlp(progress: Progress = None) -> str:
    d = data_dir()
    tmp = d / "yt-dlp.pyz.dl"
    _download(YTDLP_URL, tmp, progress, "Downloading yt-dlp")
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Downloaded yt-dlp file was not valid.")
    os.replace(tmp, d / "yt-dlp.pyz.new")
    return "yt-dlp downloaded (restart the app to use it)."


def update_deno(progress: Progress = None) -> str:
    """Download Deno (the JavaScript runtime yt-dlp needs for YouTube) into bin/."""
    machine = platform.machine().lower()
    arm = machine in ("aarch64", "arm64")
    if sys.platform.startswith("win"):
        target = "aarch64-pc-windows-msvc" if arm else "x86_64-pc-windows-msvc"
    elif sys.platform == "darwin":
        target = "aarch64-apple-darwin" if arm else "x86_64-apple-darwin"
    elif sys.platform.startswith("linux"):
        target = "aarch64-unknown-linux-gnu" if arm else "x86_64-unknown-linux-gnu"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")

    bin_dir().mkdir(parents=True, exist_ok=True)
    name = _exe("deno")
    with tempfile.TemporaryDirectory() as tmpd:
        archive = Path(tmpd) / "deno.zip"
        _download(f"https://github.com/denoland/deno/releases/latest/download/deno-{target}.zip",
                  archive, progress, "Downloading Deno")
        if progress:
            progress(-1, "Installing Deno…")
        with zipfile.ZipFile(archive) as z:
            member = next((n for n in z.namelist() if Path(n).name == name), None)
            if member is None:
                raise RuntimeError("Couldn't find Deno inside the downloaded archive.")
            with z.open(member) as src:
                _install_binary(src, name)
    return "Deno is up to date."


def _install_binary(stream, name: str):
    dest = bin_dir() / name
    tmp = dest.with_name(name + ".tmp")
    with open(tmp, "wb") as out:
        shutil.copyfileobj(stream, out)
    tmp.chmod(0o755)
    os.replace(tmp, dest)


def update_ffmpeg(progress: Progress = None) -> str:
    bin_dir().mkdir(parents=True, exist_ok=True)
    win = sys.platform.startswith("win")
    wanted = {_exe("ffmpeg"), _exe("ffprobe")}
    found: set[str] = set()
    machine = platform.machine().lower()

    with tempfile.TemporaryDirectory() as tmpd:
        tmpd = Path(tmpd)

        if win or sys.platform.startswith("linux"):
            if win:
                archive_name = "ffmpeg-master-latest-win64-gpl.zip"
            else:
                arch = "linuxarm64" if machine in ("aarch64", "arm64") else "linux64"
                archive_name = f"ffmpeg-master-latest-{arch}-gpl.tar.xz"
            archive = tmpd / archive_name
            _download(BTBN + archive_name, archive, progress, "Downloading ffmpeg")
            if progress:
                progress(-1, "Installing ffmpeg…")

            if win:
                with zipfile.ZipFile(archive) as z:
                    for n in z.namelist():
                        base = Path(n).name
                        if base in wanted and "/bin/" in n:
                            with z.open(n) as src:
                                _install_binary(src, base)
                            found.add(base)
            else:
                with tarfile.open(archive, "r:xz") as t:
                    for m in t:
                        base = Path(m.name).name
                        if m.isfile() and base in wanted and "/bin/" in m.name:
                            src = t.extractfile(m)
                            if src:
                                _install_binary(src, base)
                                found.add(base)

        elif sys.platform == "darwin":
            for tool in ("ffmpeg", "ffprobe"):
                url = "https://evermeet.cx/ffmpeg/getrelease/" + (
                    "zip" if tool == "ffmpeg" else "ffprobe/zip")
                archive = tmpd / f"{tool}.zip"
                _download(url, archive, progress, f"Downloading {tool}")
                with zipfile.ZipFile(archive) as z:
                    with z.open(tool) as src:
                        _install_binary(src, tool)
                found.add(tool)
        else:
            raise RuntimeError(f"Unsupported platform: {sys.platform}")

    if not found:
        raise RuntimeError("Couldn't find ffmpeg inside the downloaded archive.")
    return "ffmpeg is up to date."
