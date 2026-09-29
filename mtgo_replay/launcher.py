"""What double-clicking the installed app does.

If the background process (watcher + viewer server) is running, just open the
viewer in the browser; otherwise start it detached and open the viewer once it
answers.  The background process is also what runs at Windows startup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "MTGO Replay"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_home() -> Path:
    """Where the installed app keeps reviews, notes and caches."""
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME


def running_url(data: Path) -> str | None:
    """URL of our running viewer server, or None."""
    try:
        port = json.loads((data / "server.json").read_text(encoding="utf-8"))["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            if json.load(r).get("app") == "mtgo-replay":
                return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, KeyError):
        pass
    return None


def background_command(data: Path, out: Path) -> list[str]:
    if frozen():
        return [sys.executable, "--background"]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else sys.executable)
    return [exe, "-m", "mtgo_replay", "--background", "--data", str(data), "--out", str(out)]


def start_background(data: Path, out: Path):
    flags = 0x00000008 | 0x00000200 | 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(background_command(data, out), creationflags=flags, close_fds=True,
                     cwd=str(Path(__file__).resolve().parent.parent) if not frozen() else None,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def message(text: str):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)
    except Exception:
        print(text)


def launch(data: Path, out: Path):
    url = running_url(data)
    if url is None:
        start_background(data, out)
        for _ in range(120):                      # the first start builds the card database
            time.sleep(0.5)
            url = running_url(data)
            if url:
                break
    if url:
        webbrowser.open(url)
    else:
        message(f"{APP_NAME} could not start its background process.\n\nDetails: {data / 'watch.log'}")
