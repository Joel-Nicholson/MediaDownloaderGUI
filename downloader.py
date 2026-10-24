"""Download engine: a thin, GUI-agnostic wrapper around yt-dlp + ffmpeg."""

from __future__ import annotations

import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import updater

# If a newer yt-dlp was downloaded by the updater, use it instead of the bundled copy.
updater.activate_ytdlp_override()

try:
    import yt_dlp  # noqa: F401
except ImportError:
    # First run from source with nothing installed: fetch yt-dlp automatically.
    import importlib

    print("yt-dlp not found - downloading it i guess (one-time)...")
    try:
        updater.update_ytdlp(None)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Couldn't download yt-dlp ({exc}). Check your internet connection or something, "
            "or run: pip install yt-dlp"
        )
    updater.activate_ytdlp_override()
    importlib.invalidate_caches()
    import yt_dlp  # noqa: F401

from yt_dlp import YoutubeDL  # noqa: E402
from yt_dlp.utils import DownloadCancelled  # noqa: E402

# Event callback signature: on_event(kind, payload)
#   ("progress", (percent: float, text: str))
#   ("log", str)
#   ("done", (ok: bool, message: str))
EventCallback = Callable[[str, object], None]


@dataclass
class Options:
    url: str
    out_dir: str
    mode: str = "video"            # "video" or "audio"
    max_height: Optional[int] = None   # None = best available
    container: str = "mp4"         # mp4 / mkv / webm
    audio_codec: str = "mp3"       # mp3 / m4a / opus / flac / wav
    audio_quality: str = "192"     # kbps (ignored for lossless codecs)
    playlist: bool = False
    embed_thumbnail: bool = False
    embed_metadata: bool = True
    subtitles: bool = False
    sub_langs: str = "en"
    ffmpeg_path: str = ""          # optional: folder or binary path


def ytdlp_version() -> str:
    import yt_dlp.version as v
    return v.__version__


def find_ffmpeg(custom: str = "") -> Optional[str]:
    """Locate ffmpeg. Order: user choice > downloaded (data dir) > bundled > PATH."""
    if custom:
        return custom

    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidates = [updater.bin_dir()]
    meipass = getattr(sys, "_MEIPASS", None)   # ffmpeg optionally bundled by build.py
    if meipass:
        candidates.append(Path(meipass) / "bin")
    for d in candidates:
        if (d / exe).is_file():
            return str(d)

    return shutil.which("ffmpeg")


def build_ydl_opts(o: Options, hook, logger) -> dict:
    opts: dict = {
        "outtmpl": o.out_dir.rstrip("/\\")
        + "/%(playlist_title&{}/|)s%(title).150B [%(id)s].%(ext)s",
        "noplaylist": not o.playlist,
        "progress_hooks": [hook],
        "logger": logger,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": o.playlist,   # keep going if one playlist item fails
        "retries": 5,
        "restrictfilenames": False,
        "windowsfilenames": True,
    }

    ffmpeg = find_ffmpeg(o.ffmpeg_path)
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    pps: list[dict] = []

    if o.mode == "audio":
        opts["format"] = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": o.audio_codec}
        if o.audio_codec in ("mp3", "m4a", "opus"):
            pp["preferredquality"] = o.audio_quality
        pps.append(pp)
    else:
        h = o.max_height
        if h:
            opts["format"] = (
                f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
            )
        else:
            opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = o.container

    if o.embed_metadata:
        pps.append({"key": "FFmpegMetadata", "add_metadata": True})

    if o.embed_thumbnail:
        opts["writethumbnail"] = True
        # Convert to jpg so every container/codec can embed it.
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        pps.append({"key": "EmbedThumbnail"})

    if o.subtitles:
        langs = [s.strip() for s in o.sub_langs.split(",") if s.strip()] or ["en"]
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = langs
        if o.mode == "video" and o.container in ("mp4", "mkv"):
            pps.append({"key": "FFmpegEmbedSubtitle"})

    if pps and not ffmpeg:
        # Let the caller surface this; yt-dlp will raise a clear error anyway.
        pass

    opts["postprocessors"] = pps
    return opts


class _Logger:
    def __init__(self, emit: EventCallback):
        self._emit = emit

    def debug(self, msg: str):
        # yt-dlp routes some info through debug(); keep only meaningful lines.
        if msg.startswith("[debug]"):
            return
        self._emit("log", msg)

    def info(self, msg: str):
        self._emit("log", msg)

    def warning(self, msg: str):
        self._emit("log", f"WARNING: {msg}")

    def error(self, msg: str):
        self._emit("log", f"ERROR: {msg}")


def _fmt_bytes(n: Optional[float]) -> str:
    if not n:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


class DownloadWorker:
    """Runs a download on a background thread; can be cancelled."""

    def __init__(self, options: Options, on_event: EventCallback):
        self.options = options
        self._emit = on_event
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    # -- internals ---------------------------------------------------------

    def _hook(self, d: dict):
        if self._cancel.is_set():
            raise DownloadCancelled("Cancelled by user")

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            speed = d.get("speed")
            eta = d.get("eta")
            info = d.get("info_dict") or {}
            idx = info.get("playlist_index")
            prefix = f"[{idx}] " if idx else ""
            text = (
                f"{prefix}{_fmt_bytes(done)} / {_fmt_bytes(total)}"
                f"  •  {_fmt_bytes(speed)}/s"
                f"  •  ETA {int(eta)}s" if eta is not None else
                f"{prefix}{_fmt_bytes(done)} / {_fmt_bytes(total)}"
                f"  •  {_fmt_bytes(speed)}/s"
            )
            self._emit("progress", (pct, text))
        elif status == "finished":
            self._emit("progress", (100.0, "Download finished — processing with ffmpeg…"))

    def _run(self):
        try:
            opts = build_ydl_opts(self.options, self._hook, _Logger(self._emit))
            with YoutubeDL(opts) as ydl:
                ydl.download([self.options.url])
            self._emit("done", (True, "Done."))
        except DownloadCancelled:
            self._emit("done", (False, "Cancelled."))
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            if self._cancel.is_set():
                self._emit("done", (False, "Cancelled."))
            else:
                self._emit("done", (False, f"Failed: {e}"))


def fetch_info(url: str, playlist: bool = False) -> dict:
    """Quick metadata lookup (no download)."""
    with YoutubeDL({
        "quiet": True,
        "skip_download": True,
        "noplaylist": not playlist,
        "extract_flat": "in_playlist",
    }) as ydl:
        return ydl.extract_info(url, download=False)
