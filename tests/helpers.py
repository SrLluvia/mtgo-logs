"""Synthetic test data: MTGO log files, log lines and a tiny card database.

Nothing here comes from real games, so tests can run anywhere (no MTGO needed).
"""
from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

from mtgo_replay.carddb import CardDB
from mtgo_replay.events import parse_events
from mtgo_replay.gamelog import Record

T0 = dt.datetime(2026, 1, 1, 12, 0, 0)

CARDS = {
    "Island": {"types": ["Land", "Basic"]},
    "Swamp": {"types": ["Land", "Basic"]},
    "Watery Grave": {"types": ["Land"], "text": "({T}: Add {U} or {B}.)\nAs this land enters, you may pay 2 life. "
                                                "If you don't, it enters tapped."},
    "Meticulous Archive": {"types": ["Land"], "text": "({T}: Add {W} or {U}.)\nThis land enters tapped.\n"
                                                      "When this land enters, surveil 1."},
    "Polluted Delta": {"types": ["Land"], "text": "{T}, Pay 1 life, Sacrifice this land: Search your library for an "
                                                  "Island or Swamp card, put it onto the battlefield, then shuffle."},
    "Psychic Frog": {"types": ["Creature"], "power": "1", "toughness": "2", "mv": 2, "cost": "UB"},
    "Monk Token": {"types": ["Creature", "Token"], "power": "1", "toughness": "1"},
    "Grizzly Bears": {"types": ["Creature"], "power": "2", "toughness": "2", "mv": 2, "cost": "1G",
                      "keywords": []},
    "Fatal Push": {"types": ["Instant"], "mv": 1, "cost": "B",
                   "text": "Destroy target creature if it has mana value 2 or less."},
    "Thoughtseize": {"types": ["Sorcery"], "mv": 1, "cost": "B",
                     "text": "Target player reveals their hand. You choose a nonland card from it. That player "
                             "discards that card. You lose 2 life."},
    "Teferi, Time Raveler": {"types": ["Planeswalker", "Legendary"], "loyalty": "4", "mv": 3, "cost": "1WU",
                             "text": "+1: Until your next turn, you may cast sorcery spells as though they had flash.\n"
                                     "-3: Return up to one target artifact, creature, or enchantment to its owner's "
                                     "hand. Draw a card."},
    "Cori-Steel Cutter": {"types": ["Artifact"], "mv": 2, "cost": "1R",
                          "text": "Equipped creature gets +1/+1 and has trample and haste.\nFlurry — Whenever you "
                                  "cast your second spell each turn, create a 1/1 white Monk creature token with "
                                  "prowess. You may attach this Equipment to it.\nEquip {1R}"},
}


def card_db() -> CardDB:
    cards = {}
    for name, d in CARDS.items():
        cards[name] = {"types": d["types"], "power": d.get("power"), "toughness": d.get("toughness"),
                       "loyalty": d.get("loyalty"), "mv": d.get("mv", 0), "text": d.get("text", ""),
                       "keywords": d.get("keywords", []), "cost": d.get("cost", "")}
    return CardDB({"cat": {}, "tex": {}, "cards": cards})


def ref(name: str, iid: int, texture: int = 1000) -> str:
    """A card reference as MTGO writes it in the game log."""
    return f"@[{name}@:{texture},{iid}:@]"


def records(lines: list[str]) -> list[Record]:
    return [Record(T0 + dt.timedelta(seconds=i), f"@P{line}" if not line.startswith("@P") else line)
            for i, line in enumerate(lines)]


def events(lines: list[str], players=("Alice", "Bob")):
    return parse_events(records(lines), list(players))


# ------------------------------------------------------------ .dat writer
def _string(s: str) -> bytes:
    data = s.encode("utf-8")
    n, out = len(data), bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + data


def write_dat(path: Path, match_id: str, lines: list[str]) -> Path:
    """Write a Match_GameLog-style binary file (see gamelog.py for the layout)."""
    buf = bytearray(struct.pack("<h", 1) + _string(match_id) + struct.pack("<h", 4) + _string(match_id))
    for i, line in enumerate(lines):
        ticks = int((T0 + dt.timedelta(seconds=i) - dt.datetime(1, 1, 1)).total_seconds() * 10_000_000)
        buf += struct.pack("<q", ticks) + _string("") + _string(line)
    path.write_bytes(bytes(buf))
    return path
