"""Reader for MTGO's Match_GameLog_*.dat files.

Binary layout (little endian, .NET BinaryWriter conventions):
    int16   version
    string  match id            (7-bit length prefixed UTF-8)
    int16   unknown
    string  match id (again)
    then until EOF, one record per log line:
        int64   DateTime.ToBinary() (local time)
        string  sender (empty for game messages)
        string  message

One file holds a whole match; games are split on "X joined the game." runs.
"""
from __future__ import annotations

import datetime as dt
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

_TICKS_MASK = 0x3FFFFFFFFFFFFFFF
_EPOCH = dt.datetime(1, 1, 1)


@dataclass
class Record:
    time: dt.datetime
    text: str


@dataclass
class Game:
    number: int
    players: list[str]
    records: list[Record] = field(default_factory=list)
    winner: str | None = None


@dataclass
class Match:
    match_id: str
    path: Path
    records: list[Record]
    games: list[Game]
    players: list[str]

    @property
    def start(self) -> dt.datetime:
        return self.records[0].time


def _read_string(buf: bytes, i: int) -> tuple[str, int]:
    length = shift = 0
    while True:
        b = buf[i]
        i += 1
        length |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            break
    if i + length > len(buf):
        raise IndexError("string runs past the end of the file")      # record still being written
    return buf[i:i + length].decode("utf-8", "replace"), i + length


def read_records(path: Path) -> tuple[str, list[Record]]:
    buf = path.read_bytes()
    i = 2
    match_id, i = _read_string(buf, i)
    i += 2
    _, i = _read_string(buf, i)
    records = []
    while i + 8 <= len(buf):
        try:
            (ticks,) = struct.unpack_from("<q", buf, i)
            i += 8
            _sender, i = _read_string(buf, i)
            text, i = _read_string(buf, i)
            time = _EPOCH + dt.timedelta(microseconds=(ticks & _TICKS_MASK) // 10)
        except (IndexError, OverflowError, struct.error):
            break                    # MTGO is still writing this record (match in progress): keep the rest
        records.append(Record(time, text))
    return match_id, records


_JOINED = re.compile(r"^@P@P(.+) joined the game\.$")
_WINS = re.compile(r"^@P(.+) wins the game\.$")


def read_match(path: Path) -> Match:
    match_id, records = read_records(path)
    games: list[Game] = []
    players: list[str] = []
    current: Game | None = None
    joining = False
    for rec in records:
        m = _JOINED.match(rec.text)
        if m:
            if not joining:
                current = Game(len(games) + 1, [])
                games.append(current)
                joining = True
            if m.group(1) not in current.players:
                current.players.append(m.group(1))
            if m.group(1) not in players:
                players.append(m.group(1))
            continue
        joining = False
        if current is None:
            continue  # die rolls before the first game
        current.records.append(rec)
        w = _WINS.match(rec.text)
        if w:
            current.winner = w.group(1)
    games = [g for g in games if any("begins the game" in r.text for r in g.records)]
    return Match(match_id, path, records, games, players)
