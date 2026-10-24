"""Update yt-dlp and ffmpeg when running from source (no pip needed).

    python update.py

yt-dlp -> ./yt-dlp.pyz   ffmpeg -> ./bin   (the packaged app has an Update button for this)
"""
import updater


def show(pct, msg):
    print(f"\r{msg}      ", end="", flush=True)


if __name__ == "__main__":
    print(updater.update_ytdlp(show))
    print(updater.update_ffmpeg(show))
