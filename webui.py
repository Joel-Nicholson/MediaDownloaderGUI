"""Web interface: a tiny local HTTP server + a browser window. Standard library only.

The page (web/index.html) talks to this server over JSON and Server-Sent Events.
All downloading, clipping, updating and cookie logic is the existing Python code
(downloader.py / updater.py); this file only connects it to the page.

Security: the server listens on 127.0.0.1 only, every request must carry a random
per-run token, and the Host/Origin headers are checked, so other websites open in
your browser can't control it.
"""

from __future__ import annotations

import hmac
import importlib.util
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import updater
from downloader import (
    COOKIE_BROWSERS, ClipError, DownloadWorker, Options, fetch_info, find_deno,
    find_ffmpeg, friendly_error, validate_clip, ytdlp_version,
)

HEIGHTS = {None, 2160, 1440, 1080, 720, 480, 360}
CONTAINERS = ("mp4", "mkv", "webm")
AUDIO_CODECS = ("mp3", "m4a", "opus", "flac", "wav")
BITRATES = ("128", "192", "256", "320")
COOKIE_CHOICES = ["None"] + [b.capitalize() for b in COOKIE_BROWSERS]

GRACE_SECONDS = 15        # how long to wait after the window closes before quitting
STARTUP_SECONDS = 180     # quit if no window ever connects


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, kind: str = "error"):
        super().__init__(message)
        self.message, self.status, self.kind = message, status, kind


def web_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) if base else Path(__file__).resolve().parent) / "web"


def can_pick() -> bool:
    return importlib.util.find_spec("tkinter") is not None


def pick_path(kind: str, initial: str = "") -> str:
    """Show a native file/folder dialog (in a child process so it can't freeze the server)."""
    import tempfile
    fd, outfile = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parent / "app.py")]
    cmd += ["--pick", kind, initial or "", outfile]
    kwargs = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
    try:
        subprocess.run(cmd, timeout=900, capture_output=True, **kwargs)
        return Path(outfile).read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            os.remove(outfile)
        except OSError:
            pass


def open_folder(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------- app window

def _find_app_browser():
    """A Chromium-based browser (Edge/Chrome/...) that can show the page as an app window."""
    if sys.platform.startswith("win"):
        bases = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")]
        rels = [r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
                r"Vivaldi\Application\vivaldi.exe",
                r"Chromium\Application\chrome.exe"]
        for rel in rels:
            for base in bases:
                if base and (Path(base) / rel).is_file():
                    return str(Path(base) / rel)
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome", "Brave Browser", "Chromium"):
            p = f"/Applications/{app}.app/Contents/MacOS/{app}"
            if os.path.isfile(p):
                return p
    else:
        for name in ("microsoft-edge", "microsoft-edge-stable", "google-chrome",
                     "google-chrome-stable", "chromium", "chromium-browser", "brave-browser"):
            found = shutil.which(name)
            if found:
                return found
    return None


def open_window(url: str) -> str:
    """Open the UI. Returns how it was opened (for the console message)."""
    if os.environ.get("MEDIADL_NO_WINDOW"):
        return "skipped"
    exe = _find_app_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1020,900"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "app window"
        except OSError:
            pass
    webbrowser.open(url)
    return "browser tab"


# ------------------------------------------------------------------- state

class Core:
    def __init__(self):
        self.token = secrets.token_urlsafe(18)
        self.port = 0
        self.lock = threading.RLock()
        self.subs: list[queue.Queue] = []
        self.log: deque = deque(maxlen=500)
        self.worker: DownloadWorker | None = None
        self.updating = False
        self.status = "Ready."
        self.progress = 0.0
        self.stopping = threading.Event()
        self.started = time.time()
        self.had_subscriber = False
        self.last_leave = time.time()
        self.settings = self._load_settings()

    # ---- settings (shared with the classic window) ----
    @staticmethod
    def _settings_path() -> Path:
        return updater.data_dir() / "settings.json"

    def _load_settings(self) -> dict:
        try:
            data = json.loads(self._settings_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:  # noqa: BLE001
            data = {}
        data.setdefault("out_dir", str(Path.home() / "Downloads"))
        if data.get("cookie_browser") not in COOKIE_CHOICES:
            data["cookie_browser"] = "None"
        data.setdefault("cookie_file", "")
        data.setdefault("ffmpeg_path", "")
        if not isinstance(data.get("ui"), dict):
            data["ui"] = {}
        return data

    def save_settings(self, patch: dict) -> None:
        with self.lock:
            for key in ("out_dir", "cookie_file", "ffmpeg_path"):
                if isinstance(patch.get(key), str):
                    self.settings[key] = patch[key][:1000]
            if patch.get("cookie_browser") in COOKIE_CHOICES:
                self.settings["cookie_browser"] = patch["cookie_browser"]
            ui = patch.get("ui")
            if isinstance(ui, dict) and len(ui) <= 40:
                self.settings["ui"] = {str(k)[:40]: v for k, v in ui.items()
                                       if isinstance(v, (str, int, float, bool)) or v is None}
            try:
                self._settings_path().write_text(json.dumps(self.settings), encoding="utf-8")
            except OSError:
                pass

    # ---- events ----
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subs.append(q)
            self.had_subscriber = True
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)
            self.last_leave = time.time()

    def publish(self, kind: str, data) -> None:
        msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            for q in self.subs:
                q.put(msg)

    def add_log(self, text: str) -> None:
        with self.lock:
            self.log.append(text)
        self.publish("log", {"text": text})

    def busy(self) -> bool:
        with self.lock:
            return self.updating or bool(self.worker and self.worker.running)

    def publish_busy(self) -> None:
        with self.lock:
            running = bool(self.worker and self.worker.running)
            self.publish("busy", {"running": running, "updating": self.updating})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "version": ytdlp_version(),
                "ffmpeg": bool(find_ffmpeg(self.settings.get("ffmpeg_path", ""))),
                "deno": bool(find_deno()),
                "settings": self.settings,
                "cookie_choices": COOKIE_CHOICES,
                "running": bool(self.worker and self.worker.running),
                "updating": self.updating,
                "status": self.status,
                "progress": self.progress,
                "log": list(self.log),
                "can_pick": can_pick(),
            }

    # ---- request validation ----
    def cookie_args(self, d: dict):
        choice = str(d.get("cookie_browser", "None"))
        if choice not in COOKIE_CHOICES:
            raise ApiError("Unknown browser for cookies.")
        browser = "" if choice == "None" else choice.lower()
        cfile = str(d.get("cookie_file", "") or "").strip()
        if cfile and not os.path.isfile(cfile):
            raise ApiError(f"Can't find the cookies file:\n{cfile}", kind="cookies")
        return browser, cfile

    def options_from(self, d: dict) -> Options:
        url = str(d.get("url", "")).strip()
        if not url:
            raise ApiError("Paste a URL first.")
        out_dir = os.path.expanduser(str(d.get("out_dir", "")).strip())
        if not out_dir:
            raise ApiError("Choose a folder to save to.")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            raise ApiError(f"Can't use that folder: {e}")

        mode = d.get("mode", "video")
        if mode not in ("video", "audio"):
            raise ApiError("Unknown media type.")
        height = d.get("max_height")
        if height not in HEIGHTS:
            raise ApiError("Unknown quality.")
        container = d.get("container", "mp4")
        acodec = d.get("audio_codec", "mp3")
        abitrate = str(d.get("audio_quality", "192"))
        if container not in CONTAINERS or acodec not in AUDIO_CODECS or abitrate not in BITRATES:
            raise ApiError("Unknown format option.")

        playlist = bool(d.get("playlist"))
        clip_start = "" if playlist else str(d.get("clip_start", "")).strip()[:20]
        clip_end = "" if playlist else str(d.get("clip_end", "")).strip()[:20]
        try:
            validate_clip(clip_start, clip_end)
        except ClipError as e:
            raise ApiError(str(e), kind="clip")
        browser, cfile = self.cookie_args(d)

        return Options(
            url=url, out_dir=out_dir, mode=mode, max_height=height, container=container,
            audio_codec=acodec, audio_quality=abitrate, playlist=playlist,
            embed_thumbnail=bool(d.get("embed_thumbnail")), embed_metadata=bool(d.get("embed_metadata")),
            subtitles=bool(d.get("subtitles")), sub_langs=str(d.get("sub_langs", "en"))[:100] or "en",
            ffmpeg_path=str(d.get("ffmpeg_path", "") or "").strip(),
            cookies_browser=browser, cookies_file=cfile,
            clip_start=clip_start, clip_end=clip_end, accurate_cut=bool(d.get("accurate_cut", True)),
        )

    # ---- actions ----
    def start_download(self, d: dict) -> None:
        if self.busy():
            raise ApiError("Please wait for the current task to finish.", 409)
        options = self.options_from(d)
        self.save_settings({"out_dir": options.out_dir, "cookie_browser": d.get("cookie_browser"),
                            "cookie_file": options.cookies_file, "ffmpeg_path": options.ffmpeg_path})
        with self.lock:
            self.progress, self.status = 0.0, "Starting…"
            self.worker = DownloadWorker(options, self._on_worker_event)
            worker = self.worker
        self.publish("progress", {"pct": 0, "text": "Starting…"})
        worker.start()
        self.publish_busy()

    def _on_worker_event(self, kind: str, payload) -> None:
        if kind == "progress":
            pct, text = payload
            with self.lock:
                self.progress, self.status = float(pct), str(text)
            self.publish("progress", {"pct": pct, "text": text})
        elif kind == "log":
            self.add_log(str(payload))
        elif kind == "done":
            ok, msg = payload
            head, _, tip = str(msg).partition("\n\nTip: ")
            with self.lock:
                self.status = head.replace("\n", " ")[:300]
                if ok:
                    self.progress = 100.0
            self.add_log(str(msg))
            self.publish("done", {"ok": bool(ok), "message": head, "tip": tip,
                                  "clip": head.startswith("Clip times:")})
            # the worker thread is about to finish; tell the page once it has
            threading.Timer(0.3, self.publish_busy).start()

    def cancel(self) -> None:
        with self.lock:
            if self.worker and self.worker.running:
                self.worker.cancel()
                self.status = "Cancelling…"
        self.publish("progress", {"pct": self.progress, "text": "Cancelling…"})

    def fetch(self, d: dict) -> None:
        url = str(d.get("url", "")).strip()
        if not url:
            raise ApiError("Paste a URL first.")
        browser, cfile = self.cookie_args(d)
        playlist = bool(d.get("playlist"))

        def work():
            try:
                info = fetch_info(url, playlist, browser, cfile)
                title = info.get("title") or "Untitled"
                if info.get("_type") == "playlist":
                    meta = f"Playlist \u2022 {len(info.get('entries') or [])} items"
                else:
                    dur = info.get("duration")
                    parts = []
                    if info.get("uploader") or info.get("channel"):
                        parts.append(info.get("uploader") or info.get("channel"))
                    if dur:
                        h, rem = divmod(int(dur), 3600)
                        m, s = divmod(rem, 60)
                        parts.append(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
                    meta = " \u2022 ".join(parts)
                thumb = info.get("thumbnail") or ""
                if not thumb and info.get("thumbnails"):
                    thumb = (info["thumbnails"][-1] or {}).get("url", "")
                self.publish("info", {"ok": True, "title": title, "meta": meta,
                                      "thumb": thumb if str(thumb).startswith("http") else "",
                                      "duration": info.get("duration") or 0})
            except Exception as e:  # noqa: BLE001
                self.publish("info", {"ok": False,
                                      "message": friendly_error(str(e)).replace("\n\n", " ")[:400]})

        threading.Thread(target=work, daemon=True).start()

    def start_update(self, d: dict) -> None:
        if self.busy():
            raise ApiError("Please wait for the current task to finish.", 409)
        ytdlp, ffmpeg, deno = (bool(d.get(k)) for k in ("ytdlp", "ffmpeg", "deno"))
        with self.lock:
            self.updating = True
            self.progress, self.status = 0.0, "Updating…"
        self.publish_busy()

        def emit(pct, msg):
            with self.lock:
                self.progress, self.status = max(float(pct), 0.0), str(msg)
            self.publish("progress", {"pct": max(pct, 0), "text": msg, "indeterminate": pct < 0})

        def work():
            msgs, ok = [], True
            try:
                if ytdlp:
                    msgs.append(updater.update_ytdlp(emit))
                if ffmpeg:
                    msgs.append(updater.update_ffmpeg(emit))
                if deno:
                    try:
                        msgs.append(updater.update_deno(emit))
                    except Exception as e:  # noqa: BLE001 - optional, don't fail the rest
                        msgs.append(f"Couldn't install Deno ({e}). Some YouTube videos may need a login or miss formats.")
            except Exception as e:  # noqa: BLE001
                ok, msgs = False, [f"Update failed: {e}"]
            text = " ".join(msgs)
            with self.lock:
                self.updating = False
                self.status = text
                if ok:
                    self.progress = 100.0
            self.add_log(text)
            self.publish("update_done", {"ok": ok, "message": text, "restart": ok and ytdlp})
            self.publish_busy()

        threading.Thread(target=work, daemon=True).start()

    def shutdown_soon(self, server) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        with self.lock:
            if self.worker and self.worker.running:
                self.worker.cancel()
        threading.Thread(target=server.shutdown, daemon=True).start()


# ------------------------------------------------------------------ server

def make_handler(core: Core):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MediaDownloader"
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):   # keep the console (and any missing stderr) quiet
            pass

        # -- helpers --
        def _hosts(self):
            return {f"127.0.0.1:{core.port}", f"localhost:{core.port}"}

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, query: dict) -> bool:
            """Host/Origin/token checks. Sends the error response itself when it fails."""
            if self.headers.get("Host", "") not in self._hosts():
                self._json(403, {"ok": False, "error": "Bad host."})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in self._hosts()}:
                self._json(403, {"ok": False, "error": "Bad origin."})
                return False
            token = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            if not hmac.compare_digest(token.encode(), core.token.encode()):
                self._json(403, {"ok": False, "error": "Bad token."})
                return False
            return True

        # -- routes --
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                if not self._guard(query):
                    return
                page = (web_dir() / "index.html").read_text(encoding="utf-8")
                self._send(200, page.replace("__TOKEN__", core.token).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif url.path == "/api/state":
                if self._guard(query):
                    self._json(200, {"ok": True, **core.snapshot()})
            elif url.path == "/api/events":
                if self._guard(query):
                    self._events()
            elif url.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(404, {"ok": False, "error": "Not found."})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = core.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not core.stopping.is_set():
                    try:
                        msg = q.get(timeout=5)
                    except queue.Empty:
                        msg = ": ping\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                core.unsubscribe(q)

        def do_POST(self):
            url = urlparse(self.path)
            if not self._guard(parse_qs(url.query)):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 65536:
                    raise ApiError("Request too large.", 413)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ApiError("Bad request.")
                result = self._dispatch(url.path, body)
                self._json(200, {"ok": True, **(result or {})})
            except ApiError as e:
                self._json(e.status, {"ok": False, "error": e.message, "kind": e.kind})
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "Bad JSON."})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"Unexpected error: {e}"})

        def _dispatch(self, path: str, body: dict):
            if path == "/api/download":
                core.start_download(body)
            elif path == "/api/cancel":
                core.cancel()
            elif path == "/api/fetch":
                core.fetch(body)
            elif path == "/api/update":
                core.start_update(body)
            elif path == "/api/settings":
                core.save_settings(body)
            elif path == "/api/pick":
                kind = body.get("kind")
                if kind not in ("folder", "cookies", "ffmpeg"):
                    raise ApiError("Unknown picker.")
                return {"path": pick_path(kind, str(body.get("initial", "")))}
            elif path == "/api/open_folder":
                folder = os.path.expanduser(str(body.get("path", "")))
                if not os.path.isdir(folder):
                    raise ApiError("That folder doesn't exist yet.")
                open_folder(folder)
            else:
                raise ApiError("Not found.", 404)

    return Handler


def run() -> None:
    core = Core()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(core))
    server.daemon_threads = True
    core.port = server.server_address[1]
    url = f"http://127.0.0.1:{core.port}/?t={core.token}"

    def watchdog():
        while not core.stopping.is_set():
            time.sleep(1)
            with core.lock:
                nobody = not core.subs
                gone_for = time.time() - core.last_leave
                waited = time.time() - core.started
                seen = core.had_subscriber
            if (seen and nobody and gone_for > GRACE_SECONDS) or (not seen and waited > STARTUP_SECONDS):
                core.shutdown_soon(server)

    threading.Thread(target=watchdog, daemon=True).start()

    # First run: fetch whatever is missing (ffmpeg, Deno) in the background.
    need_ffmpeg = not find_ffmpeg(core.settings.get("ffmpeg_path", ""))
    need_deno = not find_deno()
    if need_ffmpeg or need_deno:
        threading.Timer(0.5, lambda: core.start_update(
            {"ytdlp": False, "ffmpeg": need_ffmpeg, "deno": need_deno})).start()

    how = open_window(url)
    print(f"MediaDownloader is running ({how}). If no window appeared, open:\n  {url}\n"
          f"Close the window to quit.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        core.stopping.set()
        server.server_close()
