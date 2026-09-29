"""Locate MTGO's data folders on disk.

MTGO is a ClickOnce app: every client update installs into a new folder with
a hashed name under %LOCALAPPDATA%\\Apps\\2.0, so nothing here hard-codes a path.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

APPS_ROOT = Path(os.path.expandvars(r"%LOCALAPPDATA%\Apps\2.0"))


def _newest_first(paths):
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def game_log_files() -> list[Path]:
    """All Match_GameLog_*.dat files, deduplicated by name (updates copy them over)."""
    found: dict[str, Path] = {}
    pattern = str(APPS_ROOT / "Data" / "**" / "Match_GameLog_*.dat")
    for f in map(Path, glob.glob(pattern, recursive=True)):
        prev = found.get(f.name)
        if prev is None or f.stat().st_mtime > prev.stat().st_mtime:
            found[f.name] = f
    return _newest_first(found.values())


def client_log_files() -> list[Path]:
    """The per-installation Logs/mtgo.log files (each is overwritten every session)."""
    return _newest_first(map(Path, glob.glob(str(APPS_ROOT / "**" / "Logs" / "mtgo.log"), recursive=True)))


def card_data_dir() -> Path | None:
    """The CardDataSource folder of the most recently updated installation."""
    dirs = [Path(p) for p in glob.glob(str(APPS_ROOT / "Data" / "*" / "*" / "*" / "Data" / "CardDataSource"))]
    dirs = [d for d in dirs if (d / "CARDNAME_STRING.xml").exists()]
    return _newest_first(dirs)[0] if dirs else None


def deck_files() -> list[Path]:
    """Saved decks ("grouping <guid>.xml"), deduplicated by name, newest copy wins."""
    found: dict[str, Path] = {}
    for f in map(Path, glob.glob(str(APPS_ROOT / "Data" / "**" / "grouping *.xml"), recursive=True)):
        prev = found.get(f.name)
        if prev is None or f.stat().st_mtime > prev.stat().st_mtime:
            found[f.name] = f
    return list(found.values())
