"""Download engine: a thin, GUI-agnostic wrapper around yt-dlp + ffmpeg."""

from __future__ import annotations

import os
import re
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
from yt_dlp.postprocessor.common import PostProcessor  # noqa: E402
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor  # noqa: E402
from yt_dlp.utils import DownloadCancelled, prepend_extension  # noqa: E402

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
    cookies_browser: str = ""      # "" or firefox/chrome/edge/brave/opera/vivaldi: use that browser's login
    cookies_file: str = ""         # optional cookies.txt (wins over cookies_browser)
    clip_start: str = ""           # optional "90", "1:30", "1:02:30" (blank = from the beginning)
    clip_end: str = ""             # optional (blank = to the end)
    accurate_cut: bool = True      # re-encode at the cut points: slower, but keeps audio/video in sync


class ClipError(ValueError):
    """Raised when the clip start/end values are invalid."""


def parse_time(text: str) -> Optional[float]:
    """'90' / '1:30' / '1:02:30' / '1:30.5' -> seconds. Blank -> None."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ClipError(f"'{text}' isn't a valid time. Use seconds, mm:ss or hh:mm:ss.")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ClipError(f"'{text}' isn't a valid time. Use seconds, mm:ss or hh:mm:ss.") from None
    if any(n < 0 for n in nums):
        raise ClipError("Times can't be negative.")
    if len(nums) > 1 and any(n >= 60 for n in nums[1:]):
        raise ClipError(f"'{text}': minutes and seconds must be below 60 when using mm:ss or hh:mm:ss.")
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def fmt_time(seconds: float) -> str:
    """Seconds -> '1h02m30s' style text (used in messages and filenames)."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def validate_clip(start_text: str, end_text: str, duration: Optional[float] = None):
    """Check clip times. Returns (start, end) in seconds (None = unset).

    If `duration` (seconds) is known, also checks the times fit inside the video.
    Raises ClipError with a human-readable message.
    """
    start, end = parse_time(start_text), parse_time(end_text)
    if start is not None and end is not None and end <= start:
        raise ClipError(f"End ({fmt_time(end)}) must be after start ({fmt_time(start)}).")
    if duration:
        length = fmt_time(duration)
        if start is not None and start >= duration:
            raise ClipError(f"Start ({fmt_time(start)}) is past the end of the video (length {length}).")
        if end is not None and end > duration:
            raise ClipError(f"End ({fmt_time(end)}) is longer than the video (length {length}).")
    return start, end


def probe_duration(url: str, cookies_browser: str = "", cookies_file: str = "") -> Optional[float]:
    """Length of the video in seconds, or None if the site doesn't say."""
    with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                    "noplaylist": True, **common_ydl_opts(cookies_browser, cookies_file)}) as ydl:
        info = ydl.extract_info(url, download=False)
    d = (info or {}).get("duration")
    return float(d) if d else None


# ----------------------------------------------------------------------------
# Clip support: trimming subtitles and chapters to the clip
#
# yt-dlp downloads subtitles/chapters for the WHOLE video. If they were embedded
# as-is, a 1-minute clip would carry a subtitle track and chapter track as long as
# the full video (players then show the full length, seeking breaks, and the
# subtitles are out of sync). These helpers cut them down to the clip and shift
# them so they start at 0:00.
# ----------------------------------------------------------------------------

_TS = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_TIMING_RE = re.compile(r"^(\s*)(" + _TS + r")(\s*-->\s*)(" + _TS + r")(.*)$")
_INLINE_TS_RE = re.compile(r"<(" + _TS + r")>")     # YouTube auto-caption word timings


def _ts_to_seconds(text: str) -> float:
    parts = text.replace(",", ".").split(":")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def _seconds_to_ts(seconds: float, comma: bool = False) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms_total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"


def _split_blocks(text: str) -> list[str]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return [b for b in re.split(r"\n{2,}", text.strip("\n")) if b.strip()]


def _clip_cue(lines: list[str], idx: int, start: float, end: float, comma: bool):
    """Return the cue's lines trimmed to [start, end] and shifted by -start, or None."""
    m = _TIMING_RE.match(lines[idx])
    if not m:
        return None
    cs, ce = _ts_to_seconds(m.group(2)), _ts_to_seconds(m.group(4))
    s, e = max(cs, start), min(ce, end)
    if e <= s:
        return None
    timing = (f"{m.group(1)}{_seconds_to_ts(s - start, comma)}{m.group(3)}"
              f"{_seconds_to_ts(e - start, comma)}{m.group(5)}")
    text_lines = [
        _INLINE_TS_RE.sub(
            lambda t: "<" + _seconds_to_ts(_ts_to_seconds(t.group(1)) - start, comma) + ">", ln)
        for ln in lines[idx + 1:]
    ]
    return lines[:idx], timing, text_lines


def trim_vtt(text: str, start: float, end: float) -> Optional[str]:
    """Trim WebVTT subtitles to [start, end] seconds, shifted to start at 0.
    Returns None if no subtitle falls inside the range."""
    out: list[str] = []
    cues = 0
    for block in _split_blocks(text):
        lines = block.split("\n")
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:                       # WEBVTT header, NOTE, STYLE, REGION: keep
            out.append(block)
            continue
        clipped = _clip_cue(lines, idx, start, end, comma=False)
        if clipped:
            before, timing, text_lines = clipped
            out.append("\n".join(before + [timing] + text_lines))
            cues += 1
    if not cues:
        return None
    return "\n\n".join(out) + "\n"


def trim_srt(text: str, start: float, end: float) -> Optional[str]:
    """Trim SubRip subtitles to [start, end] seconds, shifted to start at 0 and renumbered."""
    out: list[str] = []
    for block in _split_blocks(text):
        lines = block.split("\n")
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        clipped = _clip_cue(lines, idx, start, end, comma=True)
        if clipped:
            _before, timing, text_lines = clipped
            out.append("\n".join([str(len(out) + 1), timing] + text_lines))
    if not out:
        return None
    return "\n\n".join(out) + "\n"


class ClipTrimPP(PostProcessor):
    """Runs just before a clip is downloaded; trims subtitles and chapters to the clip.

    Video clips: yt-dlp knows the section, so the times come from the download itself.
    Audio clips: the whole audio is downloaded and cut afterwards, so the times are passed in.
    """

    def __init__(self, downloader=None, start=None, end=None, explicit=False):
        super().__init__(downloader)
        self._explicit = (float(start or 0), end) if explicit else None

    def run(self, info):
        if self._explicit is not None:
            start, end_raw = self._explicit
        else:
            start = float(info.get("section_start") or 0)
            end_raw = info.get("section_end")
        if not start and not end_raw:          # not a clip: nothing to do
            return [], info
        end = float(end_raw) if end_raw else float("inf")
        files_to_delete = self._trim_subtitles(info, start, end)
        self._trim_chapters(info, start, end)
        return files_to_delete, info

    def _trim_subtitles(self, info, start: float, end: float) -> list[str]:
        subs = info.get("requested_subtitles") or {}
        delete: list[str] = []
        for lang in list(subs):
            sub = subs[lang]
            path = sub.get("filepath")
            if not path or not os.path.isfile(path):
                continue
            ext = (sub.get("ext") or os.path.splitext(path)[1].lstrip(".")).lower()
            new_text = None
            try:
                with open(path, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
                if ext == "vtt":
                    new_text = trim_vtt(text, start, end)
                elif ext == "srt":
                    new_text = trim_srt(text, start, end)
                else:
                    raise ValueError(f"'{ext}' subtitles can't be trimmed to a clip")
                if new_text is None:
                    self.to_screen(f"No '{lang}' subtitles fall inside the clip; skipping them")
            except Exception as e:  # noqa: BLE001 - never embed untrimmed subtitles
                self.report_warning(f"Skipping '{lang}' subtitles: {e}")
            if new_text is None:
                delete.append(path)
                del subs[lang]
            else:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_text)
        return delete

    def _trim_chapters(self, info, start: float, end: float) -> None:
        chapters = info.get("chapters") or []
        if not chapters:
            return
        out = []
        for i, c in enumerate(chapters):
            cs = float(c.get("start_time") or 0)
            ce = c.get("end_time")
            if ce is None:
                nxt = chapters[i + 1].get("start_time") if i + 1 < len(chapters) else None
                ce = nxt if nxt is not None else end
            s, e = max(cs, start), min(float(ce), end)
            if e - s >= 0.5:
                out.append({**c, "start_time": s - start,
                            "end_time": None if e == float("inf") else e - start})
        info["chapters"] = out or None


class AudioClipCutPP(FFmpegPostProcessor):
    """Safety net for audio clips, run right after yt-dlp's audio conversion.

    The cut normally happens during that conversion. But when the audio is already in the
    target format (for example AAC audio saved as .m4a) yt-dlp skips the conversion, so
    nothing would be cut. This checks the file's real length and, if it is still too long,
    cuts it precisely (stream copy, reading from the start so the start point is exact).
    """

    def __init__(self, downloader=None, start=0.0, end=None):
        super().__init__(downloader)
        self._start = float(start or 0.0)
        self._end = end

    def run(self, info):
        path = info.get("filepath")
        if not path or not os.path.isfile(path):
            return [], info
        end = self._end if self._end is not None else info.get("duration")
        if end is None:
            return [], info
        want = float(end) - self._start
        if want <= 0:
            return [], info
        try:
            have = self._get_real_video_duration(path, False)
        except Exception:  # noqa: BLE001
            have = None
        if not have or float(have) <= want + 0.75:      # already cut: nothing to do
            return [], info
        out = prepend_extension(path, "cut")
        self.to_screen(f"Cutting the audio to the clip ({fmt_time(self._start)}, {fmt_time(want)} long)")
        self.run_ffmpeg(path, out, ["-vn", "-map", "0:a:0", "-c:a", "copy",
                                    "-ss", f"{self._start:.3f}", "-t", f"{want:.3f}"])
        os.replace(out, path)
        return [], info


try:   # make the step available to yt-dlp's post-processor lookup (its plugin registry)
    from yt_dlp.globals import postprocessors as _pp_registry
    _pp_registry.value["AudioClipCutPP"] = AudioClipCutPP
except ImportError:   # older yt-dlp
    import yt_dlp.postprocessor as _ytpp
    _ytpp.AudioClipCutPP = AudioClipCutPP


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


COOKIE_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi")


def find_deno() -> Optional[str]:
    """Path to the Deno JavaScript runtime (needed by YouTube), or None."""
    exe = "deno.exe" if sys.platform.startswith("win") else "deno"
    p = updater.bin_dir() / exe
    if p.is_file():
        return str(p)
    return shutil.which("deno")


def common_ydl_opts(cookies_browser: str = "", cookies_file: str = "") -> dict:
    """Settings every yt-dlp call needs: JavaScript runtime and (optional) login cookies."""
    opts: dict = {"color": {"stdout": "never", "stderr": "never"}}   # no ANSI codes in our log box
    deno = find_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    cookies_file = (cookies_file or "").strip()
    cookies_browser = (cookies_browser or "").strip().lower()
    if cookies_file:
        if not os.path.isfile(cookies_file):
            raise ValueError(f"Cookies file not found: {cookies_file}")
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        if cookies_browser not in COOKIE_BROWSERS:
            raise ValueError(f"Unsupported browser for cookies: {cookies_browser}")
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def friendly_error(message: str) -> str:
    """Add a plain-English hint to the errors people actually run into."""
    low = message.lower()
    hint = ""
    if "sign in to confirm" in low or "not a bot" in low:
        hint = ("YouTube is asking for a login. Pick your browser under \"Cookies\" "
                "(Firefox is the most reliable), make sure you're signed in to YouTube there, and try again. "
                "Turning off a VPN can also help.")
    elif "dpapi" in low or "could not copy" in low and "cookie" in low or "failed to decrypt" in low:
        hint = ("Couldn't read that browser's cookies (Chrome-based browsers often block this on Windows). "
                "Try Firefox, close the browser first, or export a cookies.txt file and choose it instead.")
    elif "could not find" in low and "cookies" in low:
        hint = "That browser's cookies weren't found. Check the browser is installed and you've used it to sign in."
    return f"{message}\n\nTip: {hint}" if hint else message


def _put_ffmpeg_on_path(location: str) -> None:
    """Make ffmpeg visible on PATH for this process.

    yt-dlp's check for partial (clip) downloads ignores `ffmpeg_location` and only looks on
    PATH, so without this a clip fails with "ffmpeg is not installed" even when it is.
    """
    p = Path(location)
    folder = str(p if p.is_dir() else p.parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if folder not in parts:
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def _can_embed_thumbnail(mode: str, target: str) -> bool:
    """Can yt-dlp embed a cover picture in this output type? (Otherwise it aborts the job.)"""
    if mode == "video":
        return target in ("mp4", "mkv")
    if target in ("mp3", "m4a"):
        return True
    if target in ("opus", "flac"):          # these need the optional 'mutagen' library
        try:
            import mutagen  # noqa: F401
            return True
        except ImportError:
            return False
    return False                              # wav and anything else


def _reset_ytdlp_ffmpeg_cache() -> None:
    """yt-dlp remembers "ffmpeg not found" for the whole session; forget it.

    Matters when ffmpeg was downloaded after yt-dlp first looked for it (first run).
    Uses internal attributes, so it is wrapped to never break if yt-dlp changes them.
    """
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
        FFmpegPostProcessor._version_cache.clear()
        FFmpegPostProcessor._version_cache[None] = None
        FFmpegPostProcessor._features_cache.clear()
    except Exception:  # noqa: BLE001
        pass


def prepare_ffmpeg(custom: str = "") -> Optional[str]:
    """Find ffmpeg and make sure yt-dlp can see it. Call before every download."""
    location = find_ffmpeg(custom)
    if location:
        _put_ffmpeg_on_path(location)
    _reset_ytdlp_ffmpeg_cache()
    return location


def build_ydl_opts(o: Options, hook, logger) -> dict:
    clip_start, clip_end = validate_clip(o.clip_start, o.clip_end)
    clipping = (clip_start is not None or clip_end is not None) and not o.playlist
    suffix = ""
    if clipping:
        suffix = " [clip {}-{}]".format(
            fmt_time(clip_start) if clip_start is not None else "start",
            fmt_time(clip_end) if clip_end is not None else "end",
        )

    opts: dict = {
        "outtmpl": o.out_dir.rstrip("/\\")
        + "/%(playlist_title&{}/|)s%(title).150B [%(id)s]" + suffix + ".%(ext)s",
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
    opts.update(common_ydl_opts(o.cookies_browser, o.cookies_file))

    ffmpeg = prepare_ffmpeg(o.ffmpeg_path)
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    if clipping and o.mode == "audio":
        # Cutting audio during the download (stream copy) can only start at the container's
        # nearest earlier seek point, so the clip came out seconds early. Audio is small, so
        # download it all and make ONE precise cut while converting to the final format.
        cut = ["-ss", f"{clip_start or 0.0:.3f}"]
        if clip_end is not None:
            cut += ["-t", f"{max(clip_end - (clip_start or 0.0), 0.001):.3f}"]
        opts["postprocessor_args"] = {"extractaudio": cut}
    elif clipping:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(
            None,
            [(clip_start or 0.0, clip_end if clip_end is not None else float("inf"))],
        )
        # Frame-accurate cutting only matters for video (it is what keeps picture and sound
        # starting together). Audio-only clips are handled above (cut while converting).
        accurate = bool(o.accurate_cut) and o.mode == "video"
        opts["force_keyframes_at_cuts"] = accurate
        if accurate and o.container in ("mp4", "mkv"):
            # The re-encode uses ffmpeg's default x264/AAC; "veryfast" makes it roughly twice
            # as fast as the default preset (files come out a bit larger).
            opts["external_downloader_args"] = {"ffmpeg_o": ["-preset", "veryfast", "-crf", "20"]}

    pps: list[dict] = []

    if o.mode == "audio":
        opts["format"] = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": o.audio_codec}
        if o.audio_codec in ("mp3", "m4a", "opus"):
            pp["preferredquality"] = o.audio_quality
        pps.append(pp)
        if clipping:
            pps.append({"key": "AudioClipCut", "start": clip_start or 0.0, "end": clip_end})
    else:
        h = o.max_height
        if h:
            opts["format"] = (
                f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
            )
        else:
            opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = o.container

    sub_pps: list[dict] = []
    meta_pps: list[dict] = []
    thumb_pps: list[dict] = []

    if o.subtitles:
        langs = [s.strip() for s in o.sub_langs.split(",") if s.strip()] or ["en"]
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = langs
        opts["subtitlesformat"] = "vtt/srt/best"   # formats we can trim to a clip
        if o.mode == "video" and o.container in ("mp4", "mkv"):
            sub_pps.append({"key": "FFmpegEmbedSubtitle"})

    if o.embed_thumbnail:
        target = o.container if o.mode == "video" else o.audio_codec
        if _can_embed_thumbnail(o.mode, target):
            opts["writethumbnail"] = True
            # Convert to jpg so every container/codec can embed it.
            thumb_pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            thumb_pps.append({"key": "EmbedThumbnail"})
        else:
            # yt-dlp would abort the whole job at the very end, so skip it instead.
            logger.warning(f"Cover art can't be embedded in {target} files; skipping the thumbnail.")

    if o.embed_metadata:
        meta = {"key": "FFmpegMetadata", "add_metadata": True}
        if thumb_pps and o.mode == "audio" and o.audio_codec == "m4a":
            # yt-dlp's cover-art step crashes on m4a files that contain a chapters track.
            meta["add_chapters"] = False
        meta_pps.append(meta)

    # Order matters (it decides whether the cover art ends up as a real cover):
    #  - MP4 video: yt-dlp's ffmpeg fallback numbers streams wrongly once a chapters
    #    track exists, so the cover must be added BEFORE the metadata/chapters step.
    #  - MKV video: the cover attachment loses its filename if metadata is written after it.
    #  - Audio: yt-dlp's usual order (metadata, then cover).
    if o.mode == "video":
        if o.container == "mp4":
            pps += sub_pps + thumb_pps + meta_pps
        else:
            pps += sub_pps + meta_pps + thumb_pps
    else:
        pps += meta_pps + thumb_pps

    if pps and not ffmpeg:
        # Let the caller surface this; yt-dlp will raise a clear error anyway.
        pass

    opts["postprocessors"] = pps
    return opts


def create_ydl(o: Options, hook, logger) -> YoutubeDL:
    """Build the YoutubeDL object for a download (used by the worker and the tests)."""
    opts = build_ydl_opts(o, hook, logger)
    ydl = YoutubeDL(opts)
    if opts.get("download_ranges"):
        # Subtitles/chapters are fetched for the whole video; trim them to the clip.
        ydl.add_post_processor(ClipTrimPP(), when="before_dl")
    elif opts.get("postprocessor_args", {}).get("extractaudio"):
        start, end = validate_clip(o.clip_start, o.clip_end)
        ydl.add_post_processor(ClipTrimPP(start=start, end=end, explicit=True), when="before_dl")
    return ydl


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

    def _check_clip(self):
        """Validate clip times against the real video length (when the site reports it)."""
        o = self.options
        if o.playlist or not (o.clip_start.strip() or o.clip_end.strip()):
            return
        self._emit("log", "Checking video length for the clip…")
        duration = probe_duration(o.url, o.cookies_browser, o.cookies_file)
        if duration is None:
            self._emit("log", "Note: this site doesn't report a length, so clip times can't be checked.")
        validate_clip(o.clip_start, o.clip_end, duration)

    def _run(self):
        try:
            prepare_ffmpeg(self.options.ffmpeg_path)
            self._check_clip()
            ydl = create_ydl(self.options, self._hook, _Logger(self._emit))
            if ydl.params.get("download_ranges"):
                self._emit("progress", (0.0, "Downloading clip (progress may not show %)…"))
            with ydl:
                ydl.download([self.options.url])
            self._emit("done", (True, "Done."))
        except DownloadCancelled:
            self._emit("done", (False, "Cancelled."))
        except ClipError as e:
            self._emit("done", (False, f"Clip times: {e}"))
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            if self._cancel.is_set():
                self._emit("done", (False, "Cancelled."))
            else:
                self._emit("done", (False, friendly_error(f"Failed: {e}")))


def fetch_info(url: str, playlist: bool = False, cookies_browser: str = "",
               cookies_file: str = "") -> dict:
    """Quick metadata lookup (no download)."""
    with YoutubeDL({
        "quiet": True,
        "skip_download": True,
        "noplaylist": not playlist,
        "extract_flat": "in_playlist",
        **common_ydl_opts(cookies_browser, cookies_file),
    }) as ydl:
        return ydl.extract_info(url, download=False)
