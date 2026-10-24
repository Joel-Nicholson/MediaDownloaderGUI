"""yt-dlp + ffmpeg download GUI (tkinter). Run:  python app.py"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# A windowed (no-console) build has no stdout/stderr; some libraries expect them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import updater  # noqa: E402
from downloader import (  # noqa: E402
    ClipError, DownloadWorker, Options, fetch_info, find_ffmpeg, validate_clip,
    ytdlp_version,
)

QUALITIES = {
    "Best available": None,
    "2160p (4K)": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}
CONTAINERS = ["mp4", "mkv", "webm"]
AUDIO_CODECS = ["mp3", "m4a", "opus", "flac", "wav"]
AUDIO_BITRATES = ["128", "192", "256", "320"]


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=12)
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.worker: DownloadWorker | None = None

        self.url = tk.StringVar()
        self.out_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.mode = tk.StringVar(value="video")
        self.quality = tk.StringVar(value="Best available")
        self.container = tk.StringVar(value="mp4")
        self.audio_codec = tk.StringVar(value="mp3")
        self.audio_bitrate = tk.StringVar(value="192")
        self.playlist = tk.BooleanVar(value=False)
        self.thumb = tk.BooleanVar(value=False)
        self.meta = tk.BooleanVar(value=True)
        self.subs = tk.BooleanVar(value=False)
        self.sub_langs = tk.StringVar(value="en")
        self.clip_start = tk.StringVar(value="")
        self.clip_end = tk.StringVar(value="")
        self.accurate = tk.BooleanVar(value=True)
        self.ffmpeg_path = tk.StringVar(value="")
        self.status = tk.StringVar(value="Ready.")
        self.info_text = tk.StringVar(value="")

        self._build()
        self._on_mode_change()
        self._check_ffmpeg()
        self._refresh_version()
        self.after(100, self._poll_events)
        if not find_ffmpeg():
            # First run: fetch ffmpeg automatically so the user never has to.
            self.after(400, lambda: self._run_update(ytdlp=False, ffmpeg=True))

    # ---- layout ----------------------------------------------------------

    def _build(self):
        self.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(self, text="URL").grid(row=r, column=0, sticky="w", pady=3)
        url_entry = ttk.Entry(self, textvariable=self.url)
        url_entry.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        url_entry.focus()
        ttk.Button(self, text="Paste", width=8, command=self._paste).grid(row=r, column=3)
        r += 1

        ttk.Label(self, textvariable=self.info_text, foreground="#666").grid(
            row=r, column=1, columnspan=2, sticky="w", padx=6
        )
        self.info_btn = ttk.Button(self, text="Fetch info", width=8, command=self._fetch_info)
        self.info_btn.grid(row=r, column=3)
        r += 1

        ttk.Label(self, text="Save to").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.out_dir).grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        ttk.Button(self, text="Browse…", width=8, command=self._browse).grid(row=r, column=3)
        r += 1

        # --- type
        type_box = ttk.LabelFrame(self, text="Media type", padding=8)
        type_box.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        ttk.Radiobutton(type_box, text="Video", value="video", variable=self.mode,
                        command=self._on_mode_change).grid(row=0, column=0, padx=(0, 16))
        ttk.Radiobutton(type_box, text="Audio only", value="audio", variable=self.mode,
                        command=self._on_mode_change).grid(row=0, column=1)

        self.video_opts = ttk.Frame(type_box)
        self.video_opts.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(self.video_opts, text="Quality").grid(row=0, column=0)
        ttk.Combobox(self.video_opts, textvariable=self.quality, values=list(QUALITIES),
                     state="readonly", width=16).grid(row=0, column=1, padx=(6, 16))
        ttk.Label(self.video_opts, text="Container").grid(row=0, column=2)
        ttk.Combobox(self.video_opts, textvariable=self.container, values=CONTAINERS,
                     state="readonly", width=6).grid(row=0, column=3, padx=6)

        self.audio_opts = ttk.Frame(type_box)
        self.audio_opts.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(self.audio_opts, text="Format").grid(row=0, column=0)
        codec_cb = ttk.Combobox(self.audio_opts, textvariable=self.audio_codec,
                                values=AUDIO_CODECS, state="readonly", width=6)
        codec_cb.grid(row=0, column=1, padx=(6, 16))
        codec_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())
        ttk.Label(self.audio_opts, text="Bitrate (kbps)").grid(row=0, column=2)
        self.bitrate_cb = ttk.Combobox(self.audio_opts, textvariable=self.audio_bitrate,
                                       values=AUDIO_BITRATES, state="readonly", width=6)
        self.bitrate_cb.grid(row=0, column=3, padx=6)
        r += 1

        # --- extras
        extras = ttk.LabelFrame(self, text="Options", padding=8)
        extras.grid(row=r, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Checkbutton(extras, text="Download whole playlist", variable=self.playlist,
                        command=self._on_playlist_toggle).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(extras, text="Embed metadata", variable=self.meta).grid(row=0, column=1, sticky="w", padx=16)
        ttk.Checkbutton(extras, text="Embed thumbnail", variable=self.thumb).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(extras, text="Subtitles", variable=self.subs).grid(row=1, column=0, sticky="w", pady=(6, 0))
        sub_row = ttk.Frame(extras)
        sub_row.grid(row=1, column=1, columnspan=2, sticky="w", padx=16, pady=(6, 0))
        ttk.Label(sub_row, text="Languages").grid(row=0, column=0)
        ttk.Entry(sub_row, textvariable=self.sub_langs, width=14).grid(row=0, column=1, padx=6)

        ttk.Label(extras, text="Clip (optional)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        clip_row = ttk.Frame(extras)
        clip_row.grid(row=2, column=1, columnspan=3, sticky="w", padx=16, pady=(8, 0))
        ttk.Label(clip_row, text="Start").grid(row=0, column=0)
        self.clip_start_entry = ttk.Entry(clip_row, textvariable=self.clip_start, width=9)
        self.clip_start_entry.grid(row=0, column=1, padx=(6, 12))
        ttk.Label(clip_row, text="End").grid(row=0, column=2)
        self.clip_end_entry = ttk.Entry(clip_row, textvariable=self.clip_end, width=9)
        self.clip_end_entry.grid(row=0, column=3, padx=(6, 12))
        self.accurate_cb = ttk.Checkbutton(clip_row, text="Frame-accurate (slower, keeps audio in sync)", variable=self.accurate)
        self.accurate_cb.grid(row=0, column=4)
        ttk.Label(extras, text="Times like 90, 1:30 or 1:02:30. Leave both blank for the whole video.",
                  foreground="#666").grid(row=3, column=1, columnspan=3, sticky="w", padx=16)
        r += 1

        # --- ffmpeg
        ff = ttk.Frame(self)
        ff.grid(row=r, column=0, columnspan=4, sticky="ew", pady=4)
        ff.columnconfigure(1, weight=1)
        self.ffmpeg_label = ttk.Label(ff, text="")
        self.ffmpeg_label.grid(row=0, column=0, sticky="w")
        ttk.Button(ff, text="Locate ffmpeg…", command=self._locate_ffmpeg).grid(row=0, column=2, sticky="e")
        self.version_label = ttk.Label(ff, text="", foreground="#666")
        self.version_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.update_btn = ttk.Button(ff, text="Update yt-dlp & ffmpeg",
                                     command=self._run_update)
        self.update_btn.grid(row=1, column=2, sticky="e", pady=(4, 0))
        r += 1

        # --- actions
        actions = ttk.Frame(self)
        actions.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        self.start_btn = ttk.Button(actions, text="Download", command=self._start)
        self.start_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(actions, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=6)
        ttk.Button(actions, text="Open folder", command=self._open_folder).grid(row=0, column=2)
        r += 1

        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        r += 1
        ttk.Label(self, textvariable=self.status).grid(row=r, column=0, columnspan=4, sticky="w")
        r += 1

        # --- log
        log_frame = ttk.Frame(self)
        log_frame.grid(row=r, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        self.rowconfigure(r, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=9, wrap="word", state="disabled", font=("TkFixedFont", 9))
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

    # ---- helpers ---------------------------------------------------------

    def _on_mode_change(self):
        self._update_clip_controls()
        if self.mode.get() == "video":
            self.audio_opts.grid_remove()
            self.video_opts.grid()
        else:
            self.video_opts.grid_remove()
            self.audio_opts.grid()
            lossless = self.audio_codec.get() in ("flac", "wav")
            self.bitrate_cb.configure(state="disabled" if lossless else "readonly")

    def _check_ffmpeg(self):
        found = find_ffmpeg(self.ffmpeg_path.get())
        if found:
            self.ffmpeg_label.configure(text=f"ffmpeg: {found}", foreground="#2e7d32")
        else:
            self.ffmpeg_label.configure(
                text="ffmpeg not found — merging/conversion will fail. Install it or locate it.",
                foreground="#c62828",
            )

    def _on_playlist_toggle(self):
        self._update_clip_controls()

    def _update_clip_controls(self):
        # One start/end for every video in a playlist makes no sense, so turn clipping off.
        playlist = self.playlist.get()
        state = "disabled" if playlist else "normal"
        for w in (self.clip_start_entry, self.clip_end_entry):
            w.configure(state=state)
        # Frame-accurate cutting only applies to video (audio clips are already precise).
        self.accurate_cb.configure(
            state="disabled" if playlist or self.mode.get() == "audio" else "normal")

    def _refresh_version(self, note: str = ""):
        self.version_label.configure(text=f"yt-dlp {ytdlp_version()}{note}")

    def _run_update(self, ytdlp: bool = True, ffmpeg: bool = True):
        if self.worker and self.worker.running:
            messagebox.showinfo("Busy", "Wait for the current download to finish first.")
            return
        self._set_updating(True)
        self.progress.configure(value=0)
        self.status.set("Updating…")

        def emit(pct, msg):
            self.events.put(("progress", (max(pct, 0), msg)))

        def work():
            msgs = []
            try:
                if ytdlp:
                    msgs.append(updater.update_ytdlp(emit))
                if ffmpeg:
                    msgs.append(updater.update_ffmpeg(emit))
                self.events.put(("update_done", (True, " ".join(msgs), ytdlp)))
            except Exception as e:  # noqa: BLE001
                self.events.put(("update_done", (False, f"Update failed: {e}", False)))

        threading.Thread(target=work, daemon=True).start()

    def _set_updating(self, updating: bool):
        state = "disabled" if updating else "normal"
        self.update_btn.configure(state=state)
        self.start_btn.configure(state=state)

    def _locate_ffmpeg(self):
        path = filedialog.askopenfilename(title="Select the ffmpeg executable")
        if path:
            self.ffmpeg_path.set(path)
            self._check_ffmpeg()

    def _paste(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get() or None)
        if d:
            self.out_dir.set(d)

    def _open_folder(self):
        path = self.out_dir.get()
        if not os.path.isdir(path):
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        self.update_btn.configure(state="disabled" if running else "normal")

    # ---- actions ---------------------------------------------------------

    def _fetch_info(self):
        url = self.url.get().strip()
        if not url:
            return
        self.info_text.set("Fetching…")
        self.info_btn.configure(state="disabled")

        def work():
            try:
                info = fetch_info(url, self.playlist.get())
                title = info.get("title", "?")
                if info.get("_type") == "playlist":
                    n = len(info.get("entries") or [])
                    msg = f"Playlist: {title} ({n} items)"
                else:
                    dur = info.get("duration")
                    d = f" — {int(dur // 60)}:{int(dur % 60):02d}" if dur else ""
                    msg = f"{title}{d}"
            except Exception as e:  # noqa: BLE001
                msg = f"Couldn't fetch info: {str(e)[:120]}"
            self.events.put(("info", msg))

        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a URL first.")
            return
        out = self.out_dir.get().strip()
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Bad folder", str(e))
            return

        clip_on = not self.playlist.get()
        if clip_on:
            try:
                validate_clip(self.clip_start.get(), self.clip_end.get())
            except ClipError as e:
                messagebox.showwarning("Clip times", str(e))
                return

        opts = Options(
            url=url,
            out_dir=out,
            mode=self.mode.get(),
            max_height=QUALITIES[self.quality.get()],
            container=self.container.get(),
            audio_codec=self.audio_codec.get(),
            audio_quality=self.audio_bitrate.get(),
            playlist=self.playlist.get(),
            embed_thumbnail=self.thumb.get(),
            embed_metadata=self.meta.get(),
            subtitles=self.subs.get(),
            sub_langs=self.sub_langs.get(),
            ffmpeg_path=self.ffmpeg_path.get(),
            clip_start=self.clip_start.get().strip() if clip_on else "",
            clip_end=self.clip_end.get().strip() if clip_on else "",
            accurate_cut=self.accurate.get(),
        )
        self.progress.configure(value=0)
        self.status.set("Starting…")
        self._set_running(True)
        self.worker = DownloadWorker(opts, lambda kind, payload: self.events.put((kind, payload)))
        self.worker.start()

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.status.set("Cancelling…")

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    pct, text = payload
                    self.progress.configure(value=pct)
                    self.status.set(text)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "info":
                    self.info_text.set(payload)
                    self.info_btn.configure(state="normal")
                elif kind == "update_done":
                    ok, msg, restart = payload
                    self.status.set(msg)
                    self._append_log(msg)
                    self._set_updating(False)
                    self._check_ffmpeg()
                    if ok:
                        self.progress.configure(value=100)
                        if restart:
                            self._refresh_version("  (new version active after restart)")
                elif kind == "done":
                    ok, msg = payload
                    self.status.set(msg)
                    if ok:
                        self.progress.configure(value=100)
                    self._append_log(msg)
                    self._set_running(False)
                    if msg.startswith("Clip times:"):
                        messagebox.showwarning("Clip times", msg[len("Clip times:"):].strip())
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


def main():
    if "--selftest" in sys.argv:   # used to verify a packaged build without opening a window
        import yt_dlp
        print("yt-dlp", ytdlp_version(), "from", yt_dlp.__file__)
        print("ffmpeg", find_ffmpeg())
        print("data dir", updater.data_dir())
        return
    root = tk.Tk()
    root.title("MediaDownloader")
    root.geometry("760x640")
    root.minsize(640, 560)
    try:
        ttk.Style().theme_use("clam" if sys.platform.startswith("linux") else ttk.Style().theme_use())
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
