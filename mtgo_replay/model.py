"""Data model of a game in progress: objects, stack items, players, recorded steps."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .events import Event

WARN = "⚠"
HIDDEN = ("library", "hand")


@dataclass
class Obj:
    uid: int
    name: str | None
    owner: str
    controller: str
    zone: str
    iid: int | None = None
    counters: Counter = field(default_factory=Counter)
    token: bool = False
    attacking: bool = False
    attached_to: int | None = None
    face_down: bool = False
    note: str = ""
    uncertain: bool = False
    exile_castable: bool = False
    placeholder: str | None = None
    damage: int = 0
    blinked_turn: int | None = None
    born: int = 0
    front: str | None = None
    tapped: bool = False
    tap_guess: bool = False   # tapped by our mana estimate, not by something the log shows
    mods: list = field(default_factory=list)   # [(dp, dt, label, until_end_of_turn)]
    inc: int = 0          # bumps on every zone change / blink: a "new object" for the rules

    def label(self) -> str:
        if self.name:
            return self.name
        return f"unknown card ({self.placeholder})" if self.placeholder else "unknown card"


@dataclass
class StackItem:
    kind: str                     # "spell" | "ability"
    controller: str
    source: str                   # card name
    text: str
    obj_uid: int | None = None    # the spell itself
    source_uid: int | None = None
    source_iid: int | None = None
    targets: list = field(default_factory=list)   # [("obj", uid) | ("player", name)]
    how: set = field(default_factory=set)
    x: int | None = None
    resolving: bool = False
    affected: set = field(default_factory=set)
    failed_search: bool = False
    energy_paid: int = 0
    target_controllers: list = field(default_factory=list)
    source_inc: int = -1


@dataclass
class PlayerState:
    name: str
    life: int = 20
    life_approx: bool = False
    hand: int = 0
    library: int = 60
    library_approx: bool = True
    approx_count: int = 0                            # number of estimated life changes so far
    counters: Counter = field(default_factory=Counter)
    known_top: list = field(default_factory=list)   # uids known on top of library (top first)
    known_bottom: list = field(default_factory=list)  # uids known to be at the bottom (order unknown)


@dataclass
class Step:
    index: int
    event: Event
    turn: int
    active: str | None
    notes: list[str]
    state: dict
