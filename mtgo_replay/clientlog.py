"""MTGO client log (Logs/mtgo.log): exact game-state snapshots and decklists.

For Twitch integration MTGO writes a JSON snapshot of the visible game state on
every change ("Game Play Status Update") and the decklist used in each game.
The file is overwritten every time MTGO starts, so the relevant lines are
archived under data/clientlogs/ each time the tool runs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

_KEEP = ("Twitch Info|", "Game State Changed for ", "(Initialization|SessionStarted)")
_SESSION = re.compile(r"SessionStarted\) ID: ([0-9a-f-]+)")
_SNAPSHOT = re.compile(r"^(\d\d:\d\d:\d\d) .*Game Play Status Update for Game ID: (\d+),.*?\) (\{.*\})\s*$")
_DECK = re.compile(r"^(\d\d:\d\d:\d\d) .*Username: (.+?) Deck Used in Game ID: (\d+)\) (\[.*\])\s*$")
_GAME = re.compile(r"Game State Changed for ([0-9a-f-]{36}):(\d+)")


@dataclass
class Snapshot:
    time: dt.time
    players: list[dict]
    cards: list[dict]


@dataclass
class ClientData:
    game_to_match: dict[int, str] = field(default_factory=dict)
    snapshots: dict[int, list[Snapshot]] = field(default_factory=dict)
    decks: dict[int, tuple[str, list[dict]]] = field(default_factory=dict)

    def games_of_match(self, match_id: str) -> list[int]:
        return sorted(g for g, m in self.game_to_match.items() if m == match_id)


_last_seen: dict[Path, tuple[int, float]] = {}


def archive(archive_dir: Path) -> list[Path]:
    """Copy the useful lines of every live mtgo.log into the archive. Returns new/updated files."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for log in paths.client_log_files():
        try:
            st = log.stat()
            if _last_seen.get(log) == (st.st_size, st.st_mtime):
                continue                        # unchanged since the last call (watch mode)
            _last_seen[log] = (st.st_size, st.st_mtime)
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        kept = [l for l in lines if any(k in l for k in _KEEP)]
        if not any("Game Play Status Update" in l for l in kept):
            continue
        session = next((m.group(1) for l in kept if (m := _SESSION.search(l))), None)
        if session is None:
            session = hashlib.sha1((lines[0] if lines else str(log)).encode()).hexdigest()[:16]
        target = archive_dir / f"{session}.log"
        if target.exists() and len(target.read_text(encoding="utf-8").splitlines()) >= len(kept):
            continue
        target.write_text("\n".join(kept) + "\n", encoding="utf-8")
        written.append(target)
    return written


def load(archive_dir: Path) -> ClientData:
    data = ClientData()
    for f in sorted(archive_dir.glob("*.log")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if m := _GAME.search(line):
                data.game_to_match[int(m.group(2))] = m.group(1)
            elif m := _SNAPSHOT.match(line):
                try:
                    js = json.loads(m.group(3))
                except json.JSONDecodeError:
                    continue
                snap = Snapshot(dt.time.fromisoformat(m.group(1)), js.get("Players", []), js.get("Cards", []))
                data.snapshots.setdefault(int(m.group(2)), []).append(snap)
            elif m := _DECK.match(line):
                try:
                    data.decks[int(m.group(3))] = (m.group(2), json.loads(m.group(4)))
                except json.JSONDecodeError:
                    pass
    return data
