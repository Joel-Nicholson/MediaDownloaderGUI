"""MediaDownloader. Run:  python app.py

  python app.py            modern web-style window (default)
  python app.py --classic  the older plain tkinter window
"""

from __future__ import annotations

import os
import sys

# A windowed (no-console) build has no stdout/stderr; some libraries expect them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def _pick(args: list[str]) -> None:
    """Native file/folder dialog, run as a separate process by the web interface.

    usage: --pick <folder|cookies|ffmpeg> <initial folder> <result file>
    """
    kind = args[0] if args else "folder"
    initial = args[1] if len(args) > 1 else ""
    outfile = args[2] if len(args) > 2 else ""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    start = initial if initial and os.path.isdir(initial) else None
    if kind == "folder":
        path = filedialog.askdirectory(initialdir=start, title="Choose a folder")
    elif kind == "cookies":
        path = filedialog.askopenfilename(
            title="Select a cookies.txt file",
            filetypes=[("Cookies", "*.txt"), ("All files", "*.*")])
    else:
        path = filedialog.askopenfilename(title="Select the ffmpeg executable")
    root.destroy()
    if outfile:
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(path or "")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--pick"]:
        _pick(args[1:])
        return
    if "--selftest" in args:   # verify a packaged build without opening a window
        import updater
        from downloader import find_deno, find_ffmpeg, ytdlp_version
        import yt_dlp
        import yt_dlp.dependencies as dep
        import webui
        print("yt-dlp", ytdlp_version(), "from", yt_dlp.__file__)
        print("ffmpeg", find_ffmpeg())
        print("deno", find_deno())
        print("ejs solver", dep.yt_dlp_ejs is not None)
        print("data dir", updater.data_dir())
        page = webui.web_dir() / "index.html"
        print("web page", page.is_file(), page)
        print("folder dialogs available", webui.can_pick())
        return
    if "--classic" in args:
        import app_tk
        app_tk.main()
        return
    import webui
    webui.run()


if __name__ == "__main__":
    main()
