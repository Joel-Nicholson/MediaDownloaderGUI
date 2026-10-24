"""Download the latest ffmpeg + ffprobe into ./bin (thin wrapper around updater.py)."""
import updater


def show(pct, msg):
    print(f"\r{msg}      ", end="", flush=True)


if __name__ == "__main__":
    print(updater.update_ffmpeg(show))
