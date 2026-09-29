"""Reconstruct the game state after every logged action.

The MTGO game log is a list of announcements, not a state dump: it never says
when a spell resolves, when a creature dies, how much damage was dealt, which
land a fetchland found, or what anyone drew.  The engine fills those gaps:

* Stack resolution is inferred from what happens next (a new turn, a land drop,
  a sorcery-speed spell, an effect "with X", a trigger from a permanent...).
* Resolved spells/abilities get their oracle effects applied (destroy / exile /
  bounce / damage / board wipes / searches) unless the log showed the result.
* Combat damage is estimated from attackers, blockers, P/T and counters.
* MTGO gives each card a new object id on every zone change and hands ids out
  in increasing order, so a later reference to an unseen id tells us a card
  moved (and roughly when); past states are patched when that happens.

Every inference is labelled: "inferred" when it follows the rules, and a
warning ("⚠") when it is a guess (combat, damage-based deaths, conflicts).
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field

from .carddb import CardDB, CardInfo
from .events import Event, CardRef, number, C
from .mana import Cost, choose_sources, parse_braced_cost, parse_mtgo_cost, produced_colors

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


_SORCERY_SPEED_TYPES = ("Creature", "Sorcery", "Artifact", "Enchantment", "Planeswalker", "Battle")
_EFFECT_KEYWORDS = {
    "mill": ("mill",), "to_graveyard": ("surveil", "graveyard", "mill"), "to_top": ("surveil", "scry", "top"),
    "scry": ("scry",), "discard": ("discard",), "fail_search": ("search",), "choose_cards": ("reveal", "look", "choose"),
    "draw": ("draw",), "life_gain": ("gain",), "life_loss": ("lose",), "counter_on": ("counter",),
    "counter_off": ("counter", "energy"), "reveal_many": ("reveal", "look"), "put_battlefield": ("battlefield",),
    "return_none": ("return",), "create": ("create",), "mulligan": (),
}


class GameEngine:
    def __init__(self, events: list[Event], players: list[str], db: CardDB, deck_sizes: dict[str, int] | None = None,
                 me: str | None = None):
        self.events = events
        self.me = me
        # for inferring the log player's hand: unknown cards entering it, and when each one became known
        self.hand_slots: dict[str, list[int]] = {p: [] for p in players}          # step of each hidden entry
        self.hand_reveals: dict[str, list[tuple]] = {p: [] for p in players}      # (uid, step, fixed entry step|None)
        # every hand movement, for merging with exact snapshots: (step, "open"|"draw", n) / (step, "in"|"out", name)
        self.hand_log: dict[str, list[tuple]] = {p: [] for p in players}
        self.last_untap: dict[str, int] = {p: -1 for p in players}
        self.last_exile_cost: tuple | None = None     # (source name, cards exiled, step) for delve
        self.last_token_by: dict[int, int] = {}       # source uid -> uid of the last token it created
        self._static_cache: dict[str, list] = {}
        self.players_order = players
        self.db = db
        self.players = {p: PlayerState(p) for p in players}
        for p, size in (deck_sizes or {}).items():
            if p in self.players and size:
                self.players[p].library = size
                self.players[p].library_approx = False
        self.objs: dict[int, Obj] = {}
        self.by_iid: dict[int, int] = {}
        self.stack: list[StackItem] = []
        self.turn = 0
        self.active: str | None = None
        self.combat: list[dict] = []       # [{"att": uid, "def": ("player", name)|("obj", uid), "blockers": [uid]}]
        self.combat_done = True
        self.reveal: dict | None = None    # last "reveals N cards with X" (for "chooses ...")
        self.notes: list[str] = []
        self.steps: list[Step] = []
        self._next_uid = 1
        self._i = 0
        # running max object id per event, to date unseen ids
        self._max_iid = []
        m = 0
        for ev in events:
            m = max(m, ev.max_iid)
            self._max_iid.append(m)

    # ------------------------------------------------------------------ helpers
    def info(self, name: str | None) -> CardInfo:
        return self.db.info(name) if name else CardInfo("?", [])

    def opponent(self, p: str | None) -> str | None:
        return next((q for q in self.players_order if q != p), None)

    def note(self, text: str):
        self.notes.append(text)

    def new_obj(self, name, owner, zone, iid=None, **kw) -> Obj:
        o = Obj(self._next_uid, name, owner, owner, zone, **kw)
        o.born = self._i
        self._next_uid += 1
        self.objs[o.uid] = o
        if iid is not None:
            self.adopt(o, iid)
        return o

    def adopt(self, o: Obj, iid: int):
        if o.iid is not None and self.by_iid.get(o.iid) == o.uid:
            del self.by_iid[o.iid]
        old = self.by_iid.get(iid)
        if old is not None and old != o.uid:
            self.objs[old].iid = None
        o.iid = iid
        self.by_iid[iid] = o.uid

    def move(self, o: Obj, zone: str, *, keep_iid=False):
        if o.zone == zone:
            return
        was = o.zone
        o.mods = []
        if was == "hand" and o.owner in self.hand_log:
            self.hand_log[o.owner].append((self._i, "out", o.name))
        elif zone == "hand" and o.owner in self.hand_log and not o.token:
            self.hand_log[o.owner].append((self._i, "in", o.name))
        o.inc += 1
        o.counters = Counter()
        if was == "battlefield":
            o.attacking = False
            o.damage = 0
            for other in self.objs.values():
                if other.attached_to == o.uid and other.zone == "battlefield":
                    other.attached_to = None
                    if other.token:           # e.g. Role tokens fall off
                        other.zone = "gone"
        if was == "library":
            p = self.players[o.owner]
            if o.uid in p.known_top:
                p.known_top.remove(o.uid)
            if o.uid in p.known_bottom:
                p.known_bottom.remove(o.uid)
        o.attached_to = None if zone != "battlefield" else o.attached_to
        o.exile_castable = False
        o.note = ""
        o.uncertain = False
        o.face_down = False if zone != "battlefield" else o.face_down
        if o.token and zone not in ("battlefield", "stack"):
            zone = "gone"
        o.zone = zone
        o.controller = o.owner if zone != "battlefield" else o.controller
        o.tapped = o.tap_guess = False
        if zone == "battlefield" and was != "battlefield":
            o.tapped = self.enters_tapped(o)
        if not keep_iid and o.iid is not None:
            self.by_iid.pop(o.iid, None)
            o.iid = None

    def enters_tapped(self, o: Obj) -> bool:
        """From the oracle text; shocklands are assumed paid, "unless you control a <type>" assumed met."""
        t = self.info(o.name).text.lower()
        if re.search(r"enters tapped\.", t) and "enters tapped unless" not in t and "if you don't, it enters tapped" not in t:
            return True
        if "enters tapped unless you control two or fewer other lands" in t:
            lands = [x for x in self.objs.values() if x.zone == "battlefield" and x.controller == o.controller
                     and x is not o and (self.info(x.name).has("Land") or (x.name is None and x.placeholder))]
            return len(lands) > 2
        return False

    def leave_hidden(self, player: str, zone: str, n: int = 1):
        p = self.players[player]
        if zone == "hand":
            p.hand = max(0, p.hand - n)
        elif zone == "library":
            p.library -= n

    def enter_hidden(self, player: str, zone: str, n: int = 1):
        p = self.players[player]
        if zone == "hand":
            p.hand += n
        elif zone == "library":
            p.library += n

    def to_zone(self, o: Obj, zone: str, **kw):
        """Move an object, keeping hidden-zone counters in sync."""
        if o.zone in HIDDEN:
            self.leave_hidden(o.owner, o.zone)
        if zone in HIDDEN and not o.token:
            self.enter_hidden(o.owner, zone)
        self.move(o, zone, **kw)

    # ---------------------------------------------------------------- finding
    def by_ref(self, ref: CardRef) -> Obj | None:
        uid = self.by_iid.get(ref.iid)
        if uid is None:
            return None
        o = self.objs[uid]
        if o.name != ref.name and o.name is not None and not o.face_down:
            o.name = ref.name        # transformed / face changed
        return o

    def candidates(self, ref: CardRef, zones, owner=None) -> list[Obj]:
        out = []
        for o in self.objs.values():
            if o.name != ref.name or (o.zone == "gone" and not o.uncertain):
                continue
            if o.iid is not None and o.iid >= ref.iid:
                continue
            if owner is not None and owner not in (o.owner, o.controller):
                continue
            out.append(o)

        def key(o: Obj):
            zi = zones.index(o.zone) if o.zone in zones else len(zones)
            return (zi, 0 if owner is None or o.owner == owner else 1, 0 if o.iid is None else 1, -o.uid)
        return sorted(out, key=key)

    def alloc_index(self, iid: int) -> int:
        """Earliest event index after which object id `iid` can have existed."""
        last_below = -1
        for i, m in enumerate(self._max_iid):
            if m < iid:
                last_below = i
            else:
                break
        return last_below + 1

    # Lines whose card id is the id the card had *in hand* (so it dates when it entered the hand);
    # casts and land plays show a new id instead.
    _HAND_ID_KINDS = ("discard", "exile_cost", "reveal_opening", "cycle", "reveal_many", "reveal", "suspend", "plotted")

    def record_hand_reveal(self, o: Obj, ref: CardRef):
        kind = self.events[self._i].kind
        entry = self.alloc_index(ref.iid) if kind in self._HAND_ID_KINDS else None
        self.hand_reveals.setdefault(o.owner, []).append((o.uid, self._i, entry))

    def infer_hand(self, player: str):
        """Show cards in `player`'s hand from the moment they were certainly there.

        Every card that later leaves the hand (cast, played, discarded...) sat in one of the
        hidden hand entries (opening hand, draws, tutors) before that.  When its in-hand id was
        logged, the entry is exact.  Otherwise it is the *latest* entry it can have come from
        while all the other cards still fit (a small scheduling problem): from then on it is
        guaranteed to be in hand.
        """
        slots = sorted(self.hand_slots.get(player, []))
        reveals = self.hand_reveals.get(player, [])
        if not slots or not reveals:
            return
        free = list(slots)
        fixed, loose = [], []
        for uid, seen, entry in reveals:
            if entry is not None:
                cand = [s for s in free if s <= min(entry, seen - 1)]
                if cand:
                    s = max(cand)
                    free.remove(s)
                    fixed.append((uid, seen, s))
                    continue
            loose.append((uid, seen))

        def feasible(pool: list[int], cards: list[tuple]) -> bool:
            pool = sorted(pool)
            for _, seen in sorted(cards, key=lambda c: c[1]):
                slot = next((s for s in pool if s < seen), None)
                if slot is None:
                    return False
                pool.remove(slot)
            return True

        placed = list(fixed)
        for card in loose:
            others = [c for c in loose if c is not card]
            for s in sorted({s for s in free if s < card[1]}, reverse=True):
                rest = list(free)
                rest.remove(s)
                if feasible(rest, others):
                    placed.append((card[0], card[1], s))
                    break
        for uid, seen, start in placed:
            o = self.objs[uid]
            for step in self.steps[start:seen]:
                objs = step.state["objects"]
                if uid not in objs:
                    objs[uid] = {**self.obj_state(o), "zone": "hand", "iid": None, "counters": {},
                                 "controller": o.owner, "note": "", "uncertain": False}

    def take(self, ref: CardRef, from_zones: tuple, owner: str | None, hidden: str | None = "hand") -> Obj:
        """Find the object a ref talks about, which should be in one of `from_zones`.

        Unknown cards come out of `hidden` (hand/library) of `owner`.
        """
        o = self.by_ref(ref)
        if o is not None:
            return o
        cands = self.candidates(ref, from_zones, owner)
        best = cands[0] if cands else None
        if best is not None and best.zone in from_zones:
            self.adopt(best, ref.iid)
            return best
        if "battlefield" in from_zones and owner is not None:
            ph = self.consume_placeholder(ref, owner)
            if ph is not None:
                return ph
        who = owner or self.active or self.players_order[0]
        if hidden in HIDDEN:
            # an unseen card leaves the hand/library; the hidden-zone count already includes it
            o = self.new_obj(ref.name, who, hidden, ref.iid)
            if hidden == "hand":
                self.record_hand_reveal(o, ref)
            return o
        if best is not None and best.zone == "stack" and any(z in from_zones for z in ("graveyard", "exile")):
            self.resolve_through(lambda it, u=best.uid: it.obj_uid == u)
            if best.zone in from_zones:
                self.adopt(best, ref.iid)
                return best
        # the card should have been in from_zones[0] but the log never showed it get there
        if best is not None and best.zone != "stack":
            self.correct(best, from_zones[0], ref)
            return best
        o = self.new_obj(ref.name, who, from_zones[0], ref.iid, uncertain=True,
                         note=f"{WARN} arrived without a log line")
        self.note(f"{WARN} {ref.name} was in {self.zone_label(from_zones[0], who)} although the log never showed it arriving")
        self.retro(o, from_zones[0], ref.iid, "")
        return o

    def correct(self, o: Obj, zone: str, ref: CardRef):
        old = o.zone
        if old in HIDDEN:
            self.leave_hidden(o.owner, old)
        self.move(o, zone)
        self.adopt(o, ref.iid)
        o.uncertain = True
        o.note = f"{WARN} moved here without a log line"
        was = "removed by a combat/damage estimate" if old == "gone" else f"tracked in {old}"
        self.note(f"{WARN} correction: {o.name} was actually in {self.zone_label(zone, o.owner)} ({was})")
        self.retro(o, zone, ref.iid, f"{o.name} → {zone}")

    def retro(self, o: Obj, zone: str, iid: int, why: str):
        """Patch already-recorded states: the object has been in `zone` since its id was allocated."""
        start = self.alloc_index(iid)
        if start >= self._i:
            return
        for step in self.steps[start:self._i]:
            objs = step.state["objects"]
            d = objs.get(o.uid)
            if d is None:
                d = self.obj_state(o)
            else:
                d = dict(d)
            d["zone"] = zone
            d["note"] = f"{WARN} inferred later from the log"
            d["uncertain"] = True
            objs[o.uid] = d

    _PLACEHOLDER_TYPES = {"land": "Land", "plains": "Land", "island": "Land", "swamp": "Land", "mountain": "Land",
                          "forest": "Land", "artifact": "Artifact", "equipment": "Artifact", "creature": "Creature",
                          "enchantment": "Enchantment", "aura": "Enchantment", "planeswalker": "Planeswalker"}

    def fits_placeholder(self, info: CardInfo, placeholder: str) -> bool:
        """Can a card with `info` be the unknown card described by `placeholder`?"""
        wanted = {t for w, t in self._PLACEHOLDER_TYPES.items() if re.search(rf"\b{w}\b", placeholder.lower())}
        return not wanted or any(info.has(t) for t in wanted)

    def consume_placeholder(self, ref: CardRef, owner: str) -> Obj | None:
        """An unknown card put onto the battlefield (fetch/search) turns out to be `ref`.

        With several pending placeholders, pick the one created last before the
        card's object id was allocated.
        """
        info = self.info(ref.name)
        pending = [o for o in self.objs.values()
                   if o.placeholder and o.name is None and (owner is None or o.owner == owner) and o.zone == "battlefield"
                   and self.fits_placeholder(info, o.placeholder)]
        if not pending:
            return None
        alloc = self.alloc_index(ref.iid)
        before = [o for o in pending if o.born <= alloc]
        o = max(before, key=lambda x: x.born) if before else min(pending, key=lambda x: x.born)
        self.note(f"{o.placeholder} was {ref.name}")
        if "you may pay 2 life" in info.text.lower():
            self.change_life(o.owner, -2, f"{ref.name}: assumed paid to enter untapped", approx=True)
            for step in self.steps[o.born:self._i]:
                pl = step.state["players"][o.owner]
                step.state["players"][o.owner] = {**pl, "life": pl["life"] - 2, "life_approx": True}
        o.name = ref.name
        o.placeholder = None
        if o.born > self.last_untap.get(o.owner, -1) and self.enters_tapped(o):
            o.tapped = True               # a land that enters tapped, found before its controller untapped
        o.note = ""
        self.adopt(o, ref.iid)
        for step in self.steps[o.born:self._i]:
            d = step.state["objects"].get(o.uid)
            if d is not None and d["name"] is None:
                step.state["objects"][o.uid] = {**d, "name": ref.name, "placeholder": None}
        return o

    def see(self, ref: CardRef, zone: str, owner: str | None = None) -> Obj:
        """A reference to a card that should currently be in `zone`."""
        o = self.by_ref(ref)
        if o is not None:
            if o.zone != zone and not (zone == "battlefield" and o.zone == "stack"):
                self.correct(o, zone, ref)
            elif o.zone == "stack" and zone == "battlefield":
                self.resolve_through(lambda it: it.obj_uid == o.uid)
            return o
        cands = self.candidates(ref, (zone,), owner)
        exact = [c for c in cands if c.zone == zone]
        if exact:
            self.adopt(exact[0], ref.iid)
            return exact[0]
        if zone == "battlefield":
            on_stack = [c for c in cands if c.zone == "stack"]
            if on_stack:
                self.resolve_through(lambda it: it.obj_uid == on_stack[0].uid)
                self.adopt(on_stack[0], ref.iid)
                return on_stack[0]
            # a pending "search your library ... onto the battlefield" explains unknown arrivals
            self.resolve_through(lambda it: it.kind == "ability" and (owner is None or it.controller == owner)
                                 and re.search(r"onto the battlefield", it.text.lower()) is not None)
            ph = self.consume_placeholder(ref, owner)
            if ph is not None:
                return ph
        if cands and cands[0].zone not in HIDDEN:
            self.correct(cands[0], zone, ref)
            return cands[0]
        who = owner or self.active or self.players_order[0]
        o = self.new_obj(ref.name, who, zone, ref.iid, uncertain=True, note=f"{WARN} arrived without a log line")
        self.note(f"{WARN} {ref.name} found in {self.zone_label(zone, who)} although the log never showed it arriving")
        self.retro(o, zone, ref.iid, "")
        return o

    def zone_label(self, zone: str, owner: str) -> str:
        return f"{owner}'s {zone}"

    # ------------------------------------------------------------------ stack
    def stack_find(self, pred) -> int | None:
        for i in range(len(self.stack) - 1, -1, -1):
            if pred(self.stack[i]):
                return i
        return None

    def resolve_through(self, pred):
        """Resolve everything down to (and including) the topmost item matching pred."""
        i = self.stack_find(pred)
        if i is None:
            return
        while len(self.stack) > i:
            self.finish()

    def resolve_above(self, i: int):
        while len(self.stack) > i + 1:
            self.finish()

    def resolve_all(self):
        while self.stack:
            self.finish()

    def item_matches_source(self, it: StackItem, src: str | None, ref: CardRef | None) -> bool:
        if ref is not None:
            if it.kind == "spell" and it.obj_uid is not None:
                o = self.objs[it.obj_uid]
                if o.iid == ref.iid:
                    return True
            if it.source_iid == ref.iid:
                return True
            name = ref.name
        else:
            name = src
        return name is not None and it.source == name

    def source_of(self, ev: Event) -> tuple[str | None, CardRef | None]:
        src = ev.info.get("src")
        if src is None:
            return None, None
        if src == C:
            idx = ev.info.get("src_idx", len(ev.cards) - 1)
            return None, ev.cards[idx] if idx < len(ev.cards) else None
        m = re.match(r"Triggered Ability from (.+)", src)
        return (m.group(1) if m else src), None

    def is_effect_of(self, ev: Event, it: StackItem) -> bool:
        name, ref = self.source_of(ev)
        if (name or ref) and self.item_matches_source(it, name, ref):
            return True
        if ev.kind == "create" and ev.cards and self.item_matches_source(it, None, ev.cards[0]):
            return True
        if ev.kind in ("choose_cards", "reveal_many") and self.reveal is not None:
            return True
        if ev.kind == "counter_on" and ev.cards:
            o = self.by_ref(ev.cards[0])
            if o is not None and o.uid == it.source_uid:
                return True
        if ev.kind in ("counter_off", "counter_on") and "energy" in ev.text and "{e}" in it.text.lower():
            return True
        if ev.kind == "draw" and ev.info.get("src") is None:
            return "draw" in it.text.lower() and ev.actor == it.controller
        kws = _EFFECT_KEYWORDS.get(ev.kind)
        if kws:
            t = it.text.lower()
            return any(k in t for k in kws)
        if ev.kind == "other" and ("chooses" in ev.text or "names" in ev.text):
            return True
        return False

    def finish(self):
        it = self.stack.pop()
        if it.kind == "spell":
            o = self.objs.get(it.obj_uid)
            # only instants/sorceries do something on resolution; a permanent's
            # abilities show up in the log as their own activations/triggers
            if o is None or not self.info(o.name).is_permanent:
                self.apply_effects(it)
            if o is None or o.zone != "stack":
                return
            info = self.info(o.name)
            if not info.is_permanent:
                self.search_effect(it, it.text.lower())
            if info.is_permanent:
                self.move(o, "battlefield", keep_iid=True)   # MTGO keeps the id stack -> battlefield
                if re.match(r"enchant ", info.text.lower()):
                    hosts = [x for x in self.target_objs(it) if x.zone == "battlefield"]
                    if hosts:
                        self.attach(o, hosts[0], "aura")
                o.controller = it.controller
                if info.has("Planeswalker") and info.loyalty and info.loyalty.isdigit():
                    o.counters["loyalty"] = int(info.loyalty)
                if info.has("Saga"):
                    o.counters["lore"] = 1
                if "evoke" in it.how:
                    o.note = "evoked"
            else:
                rebound = "rebound" in info.text.lower() and "from_hand" in it.how
                dest = "exile" if ("flashback" in it.how or rebound) else "graveyard"
                self.move(o, dest)
                if rebound:
                    o.exile_castable = True
                    o.note = "rebound"
        else:
            self.apply_effects(it)
            self.ability_side_effects(it)

    # ----------------------------------------------------------- oracle effects
    def target_objs(self, it: StackItem) -> list[Obj]:
        out = []
        for kind, v in it.targets:
            if kind == "obj" and v not in it.affected:
                o = self.objs.get(v)
                if o is not None:
                    out.append(o)
        return out

    def power_toughness(self, o: Obj) -> tuple[int | None, int | None]:
        p, t, _, _, _ = self.pt_details(o)
        return p, t

    def static_buffs(self, name: str | None) -> list:
        """Static P/T effects in a card's text: [("attached"|"anthem"|"anthem_other", dp, dt)]."""
        if name not in self._static_cache:
            out = []
            text = self.info(name).text.lower() if name else ""
            for sentence in re.split(r"[.\n]", text):
                if "until end of turn" in sentence:
                    continue
                m = re.search(r"(?:equipped|enchanted) creature gets ([+-]\d+)/([+-]\d+)", sentence)
                if m:
                    out.append(("attached", int(m.group(1)), int(m.group(2))))
                m = re.search(r"(other )?creatures you control get ([+-]\d+)/([+-]\d+)", sentence)
                if m:
                    out.append(("anthem_other" if m.group(1) else "anthem", int(m.group(2)), int(m.group(3))))
            self._static_cache[name] = out
        return self._static_cache[name]

    def pt_details(self, o: Obj):
        """(power, toughness, base power, base toughness, [reasons]) or Nones when P/T is not a number."""
        if o.face_down:
            bp, bt = 2, 2
        else:
            info = self.info(o.name)
            bp, bt = info.int_power(), info.int_toughness()
        if bp is None or bt is None:
            return None, None, None, None, []
        p, t, why = bp, bt, []
        plus = o.counters.get("+1/+1", 0) - o.counters.get("-1/-1", 0)
        p, t = p + plus, t + plus
        for dp, dt, label, eot in o.mods:
            p, t = p + dp, t + dt
            why.append(f"{dp:+d}/{dt:+d} {label}" + (" (until end of turn)" if eot else ""))
        if o.zone == "battlefield":
            for x in self.objs.values():
                if x.zone != "battlefield" or x.face_down:
                    continue
                for kind, dp, dt in self.static_buffs(x.name):
                    applies = (kind == "attached" and x.attached_to == o.uid) or \
                              (kind == "anthem" and x.controller == o.controller and self.info(o.name).has("Creature")) or \
                              (kind == "anthem_other" and x is not o and x.controller == o.controller
                               and self.info(o.name).has("Creature"))
                    if applies:
                        p, t = p + dp, t + dt
                        why.append(f"{dp:+d}/{dt:+d} {x.name}")
        return p, t, bp, bt, why

    def attach(self, what: Obj, host: Obj, why: str):
        if what.zone == "battlefield" and host.zone == "battlefield":
            what.attached_to = host.uid
            self.note(f"{what.label()} attached to {host.label()} ({why})")

    def destroy(self, o: Obj, why: str, sure: bool):
        info = self.info(o.name)
        if "INDESTRUCTIBLE" in info.keywords:
            self.note(f"{why}: {o.label()} is indestructible")
            return
        self.move(o, "graveyard")
        if not sure:
            o.uncertain = True
            o.note = f"{WARN} {why}"
        self.note(f"{'' if sure else WARN + ' '}{o.label()} → graveyard ({why})")

    def apply_effects(self, it: StackItem):
        text = it.text.lower()
        if not text:
            return
        # For modal/multi-paragraph spells only consider the parts that can matter.
        tgts = [o for o in self.target_objs(it)]
        tgt_players = [v for k, v in it.targets if k == "player"]
        src = it.source
        conditional = " if " in text.split("\n")[0]

        def live(o: Obj, zones=("battlefield",)):
            return o.zone in zones

        blink = re.search(r"exile (?:up to one )?(?:other )?target [^.]*?, then return (?:it|that card|them)", text)
        if blink:
            for o in tgts:
                if live(o):
                    self.by_iid.pop(o.iid, None)
                    o.iid = None
                    o.counters = Counter()
                    o.mods = []
                    o.blinked_turn = self.turn
                    o.inc += 1
                    self.note(f"{o.label()} is blinked by {src} (inferred)")
            return

        m = re.search(r"^exile (.+?) at the beginning of the next end step", text)
        if m and it.kind == "ability":
            for o in self.objs.values():
                if o.zone == "battlefield" and (o.name or "").lower() == m.group(1) and o.controller == it.controller:
                    if getattr(o, "blinked_turn", None) == self.turn:
                        self.note(f"{o.label()} was blinked, so it stays (inferred)")
                    else:
                        self.move(o, "exile")
                        self.note(f"{o.label()} → exile ({src}, inferred)")
                    break
            return

        if re.search(r"\btap (?:up to \w+ )?(?:another )?target", text):
            for o in tgts:
                if live(o):
                    o.tapped, o.tap_guess = True, False
        if re.search(r"\buntap (?:up to \w+ )?(?:another )?target", text):
            for o in tgts:
                if live(o):
                    o.tapped = o.tap_guess = False

        m = re.search(r"target creatures? gets? ([+-]\d+)/([+-]\d+) until end of turn", text)
        if m:
            for o in tgts:
                if live(o):
                    o.mods.append((int(m.group(1)), int(m.group(2)), src, True))
        m = re.search(r"creatures you control get ([+-]\d+)/([+-]\d+) until end of turn", text)
        if m:
            for o in self.objs.values():
                if o.zone == "battlefield" and o.controller == it.controller and self.info(o.name).has("Creature"):
                    o.mods.append((int(m.group(1)), int(m.group(2)), src, True))

        if re.search(r"you may (?:cast|play) (?:that card|it|them)", text):
            for o in tgts:
                o.exile_castable = True

        handled = False
        m = re.search(r"return (?:up to \w+ )?target [^.]*? from (?:your|a|their) graveyard to (the battlefield|your hand|(?:its|their) owner'?s'? hand)", text)
        if m:
            dest = "battlefield" if "battlefield" in m.group(1) else "hand"
            for o in tgts:
                if o.zone == "graveyard":
                    self.to_zone(o, dest)
                    o.controller = it.controller
                    self.note(f"{o.label()} → {dest} ({src}, inferred)")
            handled = True
        if re.search(r"\bdestroy (?:up to \w+ )?(?:other |another )?target", text):
            for o in tgts:
                if live(o):
                    self.destroy(o, f"destroyed by {src}" + (", condition assumed met" if conditional else ""), not conditional)
            handled = True
        m = re.search(r"\bexile (?:up to \w+ )?(?:other |another )?target ([^.]*)", text)
        if m and not handled:
            from_gy = "graveyard" in m.group(1)
            for o in tgts:
                if live(o, ("graveyard",) if from_gy else ("battlefield", "graveyard")):
                    power, _ = self.power_toughness(o)
                    controller = o.controller
                    self.move(o, "exile")
                    self.note(f"{o.label()} → exile ({src}, inferred)")
                    if "gains life equal to its power" in text and power:
                        self.change_life(controller, power, f"{src}", approx=False)
            handled = True
        if re.search(r"return (?:up to \w+ )?(?:other )?target [^.]*? to (?:its|their) owners?'? hands?", text) and not handled:
            for o in tgts:
                if live(o):
                    self.to_zone(o, "hand")
                    self.note(f"{o.label()} → {o.owner}'s hand ({src}, inferred)")
            handled = True
        if re.search(r"put target [^.]*? on (?:top|the bottom) of (?:its|their) owner'?s'? library", text) and not handled:
            for o in tgts:
                if live(o):
                    self.to_zone(o, "library")
                    self.note(f"{o.label()} → library ({src}, inferred)")
            handled = True
        m = re.search(r"deals? (\d+|x) damage to (any target|target [^.,]*|each [^.,]*)", text)
        if m:
            n = it.x if m.group(1) == "x" else int(m.group(1))
            if n is not None:
                for p in tgt_players:
                    self.change_life(p, -n, f"{src} damage", approx=False)
                for o in tgts:
                    if live(o):
                        self.damage_permanent(o, n, src)
        m = re.search(r"gets? -(\d+|x)/-(\d+|x)", text)
        if m:
            n = it.x if m.group(2) == "x" else int(m.group(2))
            for o in tgts:
                if live(o) and n is not None:
                    _, t = self.power_toughness(o)
                    if t is not None and t - o.damage <= n:
                        self.destroy(o, f"-{n}/-{n} from {src}", False)
        m = re.search(r"target (?:player|opponent) loses (\d+) life", text)
        if m:
            for p in tgt_players:
                self.change_life(p, -int(m.group(1)), src)
        m = re.search(r"each opponent loses (\d+) life", text)
        if m:
            self.change_life(self.opponent(it.controller), -int(m.group(1)), src)
        m = re.search(r"(?:^|\. |\n)you lose (\d+) life", text)
        if m and it.kind == "spell":
            self.change_life(it.controller, -int(m.group(1)), src)
        m = re.search(r"(?:^|\. |\n)you gain (\d+) life", text)
        if m and it.kind == "spell":
            self.change_life(it.controller, int(m.group(1)), src)
        self.board_wipe(it, text)
        self.edict(it, text, tgt_players)
        m = re.search(r"exile (target player's|each opponent's|each player's|all|your) graveyards?", text)
        if m:
            who = {"target player's": tgt_players, "each opponent's": [self.opponent(it.controller)],
                   "your": [it.controller]}.get(m.group(1), list(self.players))
            for p in who:
                gone = [o for o in self.objs.values() if o.zone == "graveyard" and o.owner == p]
                for o in gone:
                    self.move(o, "exile")
                if gone:
                    self.note(f"{p}'s graveyard ({len(gone)} cards) → exile ({src}, inferred)")

    def edict(self, it: StackItem, text: str, tgt_players: list[str]):
        m = re.search(r"(each opponent|target player|target opponent|each player) sacrifices? (?:a|an|one) ([^.]*?)(?: of their choice)?[.,]", text)
        if not m:
            return
        who = {"each opponent": [self.opponent(it.controller)], "each player": list(self.players)}.get(m.group(1), tgt_players)
        what = m.group(2)
        for p in who:
            pool = []
            for o in self.objs.values():
                if o.zone != "battlefield" or o.controller != p:
                    continue
                info = self.info(o.name)
                if "nontoken" in what and o.token:
                    continue
                kinds = [t for t in ("creature", "artifact", "enchantment", "planeswalker", "land") if t in what]
                if kinds and not any(info.has(k.capitalize()) for k in kinds):
                    continue
                pool.append(o)
            if len(pool) == 1:
                self.destroy(pool[0], f"sacrificed to {it.source}", True)
            elif pool:
                self.note(f"{WARN} {p} sacrifices one of: {', '.join(o.label() for o in pool)} ({it.source}; which one is not logged)")

    def board_wipe(self, it: StackItem, text: str):
        m = re.search(r"destroy (?:all|each) ([^.]*)", text)
        if not m:
            return
        what = m.group(1)
        kinds = [k for k, t in (("artifact", "Artifact"), ("creature", "Creature"), ("enchantment", "Enchantment"),
                                ("planeswalker", "Planeswalker"), ("land", "Land")) if k in what]
        nonland = "nonland permanent" in what
        mv_limit = None
        if "mana value" in what:
            if "{e}" in text or "energy" in text:
                mv_limit = it.energy_paid
            elif it.x is not None:
                mv_limit = it.x
            exact = "less than" not in what
        hit = []
        for o in list(self.objs.values()):
            if o.zone != "battlefield":
                continue
            info = self.info(o.name)
            if not (any(info.has(t) for _, t in [(k, dict(artifact="Artifact", creature="Creature", enchantment="Enchantment", planeswalker="Planeswalker", land="Land")[k]) for k in kinds]) or (nonland and not info.has("Land"))):
                continue
            if mv_limit is not None:
                mv = 0 if o.token else self.info(o.front or o.name).mv
                if (exact and mv != mv_limit) or (not exact and mv > mv_limit):
                    continue
            hit.append(o)
        for o in hit:
            self.destroy(o, f"{it.source}", mv_limit is not None or True)
        if not hit:
            self.note(f"{it.source}: nothing destroyed (inferred)")

    def damage_permanent(self, o: Obj, n: int, src: str):
        info = self.info(o.name)
        if info.has("Planeswalker"):
            o.counters["loyalty"] = max(0, o.counters.get("loyalty", 0) - n)
            self.note(f"{o.label()} loses {n} loyalty ({src})")
            if o.counters["loyalty"] == 0:
                self.destroy(o, "no loyalty left", True)
            return
        _, t = self.power_toughness(o)
        o.damage += n
        if t is not None and o.damage >= t:
            self.destroy(o, f"lethal damage from {src}", False)

    def change_life(self, player: str | None, delta: int, why: str, approx: bool = False):
        if player not in self.players or not delta:
            return
        p = self.players[player]
        p.life += delta
        if approx:
            p.life_approx = True
            p.approx_count += 1
        self.note(f"{'≈ ' if approx else ''}{player} {'gains' if delta > 0 else 'loses'} {abs(delta)} life ({why}) → {p.life}")

    def search_effect(self, it: StackItem, t: str) -> bool:
        m = re.search(r"(each player|its controller|that player|target player)?(?: may)? ?search(?:es)? (?:your|their|his or her) "
                      r"library for ([^.]*?)(?:cards?)?(?: with [^,]*)?, (?:reveal (?:it|them|that card), )?"
                      r"puts? (?:it|them|that card) (onto the battlefield|into (?:your|their) hand)", t)
        if not m:
            h = re.search(r"put (?:a|an|up to one) ([^.]*?) from your hand onto the battlefield", t)
            if h and it.kind == "ability":
                p = self.players[it.controller]
                if p.hand > 0:
                    p.hand -= 1
                    what = re.sub(r"\s*card$", "", h.group(1).strip())
                    ph = self.new_obj(None, it.controller, "battlefield", placeholder=f"{what} put from hand with {it.source}")
                    self.note(f"{it.controller} may have put an {what} from hand onto the battlefield ({it.source}, inferred)")
                return True
            return False
        if it.failed_search:
            return False
        who = m.group(1)
        if who == "each player":
            players = list(self.players)
        elif who in ("its controller", "that player"):
            players = it.target_controllers or [it.controller]
        elif who == "target player":
            players = [v for k, v in it.targets if k == "player"] or [it.controller]
        else:
            players = [it.controller]
        what = re.sub(r"^an? ", "", m.group(2).strip()) or "card"
        for owner in players:
            p = self.players[owner]
            self.shuffle(owner)
            p.library -= 1
            if "battlefield" in m.group(3):
                ph = self.new_obj(None, owner, "battlefield", placeholder=f"{what} found with {it.source}")
                ph.tapped = "onto the battlefield tapped" in t
                self.note(f"{owner} puts an unknown {what} onto the battlefield ({it.source}, inferred)")
            else:
                p.hand += 1
                self.hand_slots[owner].append(self._i)
                self.hand_log[owner].append((self._i, "draw", 1))
        return True

    def shuffle(self, player: str):
        """After a shuffle no library position is known any more."""
        p = self.players[player]
        p.known_top.clear()
        p.known_bottom.clear()

    def ability_side_effects(self, it: StackItem):
        t = it.text.lower()
        src = self.objs.get(it.source_uid) if it.source_uid else None
        if src is not None and it.source_inc >= 0 and src.inc != it.source_inc:
            src = None        # the source left or was blinked: it's a new object now
        if src is not None and src.zone == "battlefield":
            if t.startswith("prowess"):
                src.mods.append((1, 1, "prowess", True))
            m = re.search(r"(?:this creature|~) gets ([+-]\d+)/([+-]\d+) until end of turn", t)
            if m:
                src.mods.append((int(m.group(1)), int(m.group(2)), src.label(), True))
            if t.startswith("equip"):
                hosts = [o for o in self.target_objs(it) if o.zone == "battlefield"]
                if hosts:
                    self.attach(src, hosts[0], "equip")
            if "attach this equipment to it" in t or "then attach this to it" in t:
                if "cloak the top card" in t:
                    host = self.new_obj("Cloaked creature", it.controller, "battlefield", face_down=True)
                    host.note = "face-down 2/2 (cloaked)"
                    self.players[it.controller].library -= 1
                    self.attach(src, host, "cloak")
                elif "you may" not in t and src.uid in self.last_token_by:
                    token = self.objs.get(self.last_token_by[src.uid])
                    if token is not None:
                        self.attach(src, token, "living weapon")
        if t.startswith("evoke") and src is not None and src.zone == "battlefield":
            self.move(src, "graveyard")
            self.note(f"{src.label()} is sacrificed (evoke)")
        if not self.search_effect(it, t) and "shuffle" in t:
            self.shuffle(it.controller)
        if src is not None and src.zone == "battlefield" and self.info(src.name).has("Saga"):
            chapters = len(re.findall(r"^(?:I|II|III|IV|V|VI)(?:, (?:I|II|III|IV|V|VI))* —", self.info(src.name).text, re.M))
            if chapters and src.counters.get("lore", 0) >= chapters:
                self.move(src, "graveyard")
                self.note(f"{src.label()} is sacrificed after its last chapter (inferred)")

    # ------------------------------------------------------------------ combat
    def combat_damage(self):
        if self.combat_done or not self.combat:
            self.combat_done = True
            return
        self.combat_done = True
        dealt_to_player = Counter()
        for c in self.combat:
            att = self.objs.get(c["att"])
            if att is None or att.zone != "battlefield":
                continue
            p, t = self.power_toughness(att)
            blockers = [self.objs[b] for b in c["blockers"] if self.objs[b].zone == "battlefield"]
            if c["blockers"]:
                if not blockers:
                    continue
                info = self.info(att.name)
                if p is not None and p > 0:
                    rest = p
                    for b in blockers:
                        _, bt = self.power_toughness(b)
                        dmg = rest if b is blockers[-1] else min(rest, max(0, (bt or 0) - b.damage))
                        if dmg <= 0:
                            continue
                        rest -= dmg
                        b.damage += dmg
                        if (bt is not None and b.damage >= bt) or "DEATHTOUCH" in info.keywords:
                            self.destroy(b, f"blocked {att.label()} (combat estimate)", False)
                    if "TRAMPLE" in info.keywords and rest > 0 and c["def"][0] == "player":
                        dealt_to_player[c["def"][1]] += rest
                for b in blockers:
                    bp, _ = self.power_toughness(b)
                    if bp:
                        att.damage += bp
                if t is not None and att.damage >= t and att.zone == "battlefield":
                    self.destroy(att, "died in combat (estimate)", False)
                continue
            if p is None:
                self.note(f"{WARN} damage of {att.label()} unknown (variable power)")
                self.players[c["def"][1] if c["def"][0] == "player" else self.opponent(att.controller)].life_approx = True
                continue
            if p <= 0:
                continue
            if c["def"][0] == "player":
                dealt_to_player[c["def"][1]] += p
                if "lifelink" in self.info(att.name).text.lower():
                    self.change_life(att.controller, p, f"lifelink of {att.label()}", approx=True)
            else:
                pw = self.objs.get(c["def"][1])
                if pw is not None and pw.zone == "battlefield":
                    self.damage_permanent(pw, p, att.label())
        for player, dmg in dealt_to_player.items():
            self.change_life(player, -dmg, "combat damage, estimated", approx=True)
        for o in self.objs.values():
            o.attacking = False

    # ------------------------------------------------------------------- state
    def obj_state(self, o: Obj) -> dict:
        return {
            "name": o.name, "iid": o.iid, "owner": o.owner, "controller": o.controller, "zone": o.zone,
            "counters": dict(o.counters), "token": o.token, "attacking": o.attacking,
            "attached_to": o.attached_to, "face_down": o.face_down, "note": o.note,
            "uncertain": o.uncertain, "placeholder": o.placeholder, "damage": o.damage,
            "tapped": o.tapped, "tap_guess": o.tap_guess,
        }

    def snapshot(self) -> dict:
        pos = {}
        for p in self.players.values():
            pos.update({u: f"top{i}" for i, u in enumerate(p.known_top)})
            pos.update({u: "bottom" for u in p.known_bottom})
        objects = {}
        for o in self.objs.values():
            if o.zone != "gone":
                d = self.obj_state(o)
                if o.zone == "library":
                    d["lib_pos"] = pos.get(o.uid)
                if o.zone == "battlefield":
                    pw, tg, bp, bt, why = self.pt_details(o)
                    if pw is not None:
                        d["pt"], d["pt_base"], d["pt_mods"] = f"{pw}/{tg}", f"{bp}/{bt}", why
                objects[o.uid] = d
        return {
            "players": {n: {"life": p.life, "life_approx": p.life_approx, "approx_count": p.approx_count,
                            "hand": p.hand, "library": p.library,
                            "library_approx": p.library_approx, "counters": dict(p.counters)}
                        for n, p in self.players.items()},
            "objects": objects,
            "stack": [{"kind": it.kind, "controller": it.controller, "source": it.source,
                       "text": it.text if it.kind == "ability" else "",
                       "obj": it.obj_uid,
                       "targets": [self.objs[v].name if k == "obj" and v in self.objs else v for k, v in it.targets]}
                      for it in self.stack],
        }

    # -------------------------------------------------------------------- run
    def run(self) -> list[Step]:
        for i, ev in enumerate(self.events):
            self._i = i
            self.notes = []
            try:
                self.settle(ev)
                self.apply(ev)
            except Exception as e:     # never lose the whole game to one odd line
                self.note(f"{WARN} could not process this line ({type(e).__name__}: {e})")
            self.steps.append(Step(i, ev, self.turn, self.active, self.notes, self.snapshot()))
        if self.me in self.players:
            self.infer_hand(self.me)
        return self.steps

    # ------------------------------------------------ implicit stack resolution
    def settle(self, ev: Event):
        k = ev.kind
        if self.stack and self.stack[-1].resolving and not self.is_effect_of(ev, self.stack[-1]):
            self.finish()
        if k in ("turn", "play_land", "attack", "block", "begin_hand", "mulligan", "mull_bottom", "wins", "loses", "concede", "ninjutsu"):
            if k == "ninjutsu":
                return
            self.resolve_all()
            if k in ("turn", "play_land") or k in ("wins", "concede"):
                self.combat_damage()
            return
        if k in ("cast", "activate", "trigger"):
            self.pop_opponent_permanents(ev)
        if k == "cast":
            info = self.info(ev.cards[0].name)
            rest = ev.info.get("rest", "")
            flash = info.has("Instant") or "\nflash" in "\n" + info.text.lower() or "flash\n" in info.text.lower()
            if not flash and "without paying" not in rest and ev.actor == self.active and not self.stack_has_other(ev.actor):
                self.resolve_all()
                self.combat_damage()
            else:
                self.pop_own(ev.actor, refs=ev.cards[1:])
            return
        if k == "activate":
            ref = ev.cards[0]
            o = self.by_ref(ref)
            if o is not None and o.zone == "stack":
                self.resolve_through(lambda it: it.obj_uid == o.uid)
            self.pop_own(ev.actor, only_spells=True)
            return
        if k == "trigger":
            text = (ev.info.get("text") or "").lower()
            src_name, src_ref = self.source_of(ev)
            if src_ref is None and ev.info.get("src") == C:
                src_ref = ev.cards[0]
            if text.startswith(("warp", "rebound")) or "at the beginning of" in text or "beginning of the next end step" in text:
                if not text.startswith("draw a card at the beginning"):
                    self.resolve_all()
                    self.combat_damage()
            if "deals combat damage" in text:
                self.combat_damage()
            if src_ref is not None and not re.match(r"(when|whenever) you cast", text) and "cast this spell" not in text:
                # a permanent's trigger: if that permanent was still a spell on the stack, it resolved
                o = self.by_ref(src_ref)
                if o is not None and o.zone == "stack":
                    self.resolve_through(lambda it: it.obj_uid == o.uid)
                elif o is None:
                    self.resolve_through(lambda it, n=src_ref.name: it.kind == "spell" and self.objs[it.obj_uid].name == n)
            return
        # effects: find which stack item is resolving
        name, ref = self.source_of(ev)
        if k == "create" and ev.cards:
            ref = ev.cards[0]
        if name or ref:
            idx = self.stack_find(lambda it: self.item_matches_source(it, name, ref))
            if idx is not None:
                self.resolve_above(idx)
                self.stack[idx].resolving = True
                return
        if k in ("counter_on",) and ev.cards:
            o = self.by_ref(ev.cards[0])
            if o is not None:
                if o.zone == "stack":
                    self.resolve_through(lambda it: it.obj_uid == o.uid)
                    return
                idx = self.stack_find(lambda it: it.source_uid == o.uid and it.kind == "ability")
                if idx is not None:
                    self.resolve_above(idx)
                    self.stack[idx].resolving = True
                    return
        if self.stack and k in _EFFECT_KEYWORDS and self.is_effect_of(ev, self.stack[-1]):
            self.stack[-1].resolving = True

    def pop_opponent_permanents(self, ev: Event):
        """Acting at instant speed without targeting the opponent's permanent spell
        almost always happens after it resolved (nobody fetches in response to a creature)."""
        text = (ev.info.get("text") or "").lower()
        if ev.kind == "trigger" and "cast" in text:
            return
        ref_iids = {c.iid for c in ev.cards}
        while self.stack:
            top = self.stack[-1]
            if top.kind != "spell" or top.controller == ev.actor or top.resolving:
                return
            o = self.objs.get(top.obj_uid)
            if o is None or not self.info(o.name).is_permanent or o.iid in ref_iids:
                return
            self.finish()

    def stack_has_other(self, actor: str) -> bool:
        return any(it.controller != actor for it in self.stack)

    def pop_own(self, actor: str, only_spells: bool = False, refs: list | None = None):
        """A player acting again right after their own spell: it already resolved.

        Except when the new action targets the source of one of those items
        (e.g. Ephemerate in response to Solitude's evoke trigger).
        """
        targeted = set()
        for r in refs or []:
            o = self.by_ref(r)
            if o is not None:
                targeted.add(o.uid)
        if targeted and any(it.source_uid in targeted or it.obj_uid in targeted for it in self.stack):
            return
        while self.stack and self.stack[-1].controller == actor and not self.stack[-1].resolving:
            if only_spells and self.stack[-1].kind != "spell":
                break
            self.finish()

    # ----------------------------------------------------------- apply events
    def apply(self, ev: Event):
        handler = getattr(self, f"on_{ev.kind}", None)
        if handler is not None:
            handler(ev)

    def on_turn(self, ev: Event):
        self.turn = ev.n
        self.active = ev.actor if ev.actor in self.players else self.active
        self.combat = []
        self.combat_done = True
        for o in self.objs.values():
            o.attacking = False
            o.damage = 0
            if o.note in ("evoked",):
                o.note = ""
            o.mods = [m for m in o.mods if not m[3]]          # "until end of turn" effects end
            if o.zone == "battlefield" and o.controller == self.active and not o.counters.get("stun"):
                o.tapped = o.tap_guess = False                # untap step
        if self.active in self.last_untap:
            self.last_untap[self.active] = self._i

    def on_begin_hand(self, ev: Event):
        p = self.players[ev.actor]
        full = p.library + p.hand
        p.hand = ev.n
        p.library = full - ev.n
        self.hand_slots[ev.actor] = [self._i] * ev.n        # a mulligan replaces the previous hand
        self.hand_log[ev.actor].append((self._i, "open", ev.n))

    def on_mull_bottom(self, ev: Event):
        self.on_begin_hand(ev)

    def on_draw(self, ev: Event):
        p = self.players[ev.actor]
        n = ev.n or 1
        for _ in range(n):
            if p.known_top:
                o = self.objs[p.known_top.pop(0)]
                self.move(o, "hand")
                p.hand += 1
                p.library -= 1
                self.note(f"{ev.actor} draws {o.label()} (known top card)")
            else:
                p.hand += 1
                p.library -= 1
                self.hand_slots[ev.actor].append(self._i)
                self.hand_log[ev.actor].append((self._i, "draw", 1))

    def on_play_land(self, ev: Event):
        o = self.take(ev.cards[0], ("hand",), ev.actor)
        self.to_zone(o, "battlefield", keep_iid=True)
        if self.info(o.name).has("Saga"):
            o.counters["lore"] = 1
        o.controller = ev.actor
        if "you may pay 2 life" in self.info(o.name).text.lower():
            self.change_life(ev.actor, -2, f"{o.name}: assumed paid to enter untapped", approx=True)

    def on_cast(self, ev: Event):
        ref = ev.cards[0]
        rest = ev.info.get("rest", "")
        how = set()
        from_zones = ("hand",)
        if "from the graveyard" in rest or "Flashback" in rest or "escape" in rest:
            from_zones = ("graveyard",)
            how.add("flashback" if "Flashback" in rest else "escape" if "escape" in rest else "graveyard")
        elif "Rebound" in rest:
            from_zones = ("exile",)
            how.add("rebound_cast")
        elif re.search(r" with (?!warp|evoke|kicker|its |Flashback|Rebound|Replicate|dash)", rest) and not rest.startswith(" by "):
            from_zones = ("exile", "hand")
        elif "without paying its mana cost" in rest:
            from_zones = ("exile", "hand", "library")
        if "warp" in rest:
            how.add("warp")
        if "evoke" in rest:
            how.add("evoke")
        cands = self.candidates(ref, from_zones, ev.actor)
        if from_zones == ("hand",) and not self.by_ref(ref):
            castable = [c for c in self.candidates(ref, ("exile", "graveyard"), ev.actor)
                        if c.zone in ("exile", "graveyard") and c.exile_castable]
            if castable and "warp" not in rest:
                from_zones = (castable[0].zone, "hand")
        o = self.take(ref, from_zones, ev.actor, hidden="hand" if "hand" in from_zones else ("library" if "library" in from_zones else "late"))
        if o.zone == "hand" or o.zone not in from_zones and o.zone in HIDDEN:
            how.add("from_hand")
        self.to_zone(o, "stack", keep_iid=True)
        o.controller = ev.actor
        info = self.info(ref.name)
        x = re.search(r"X is (\d+)", rest)
        modes = re.findall(r'"([^"]+)"', rest) if "choosing" in rest else []
        it = StackItem("spell", ev.actor, ref.name, " ".join(modes) if modes else info.text, obj_uid=o.uid, source_uid=o.uid, source_iid=ref.iid,
                       how=how, x=int(x.group(1)) if x else None)
        it.targets = self.targets_of(ev, ev.cards[1:], info.text)
        it.target_controllers = [self.objs[v].controller for k, v in it.targets if k == "obj"]
        self.stack.append(it)
        self.pay_mana(ev.actor, self.spell_cost(ref.name, info, rest), it.x or 0)

    def spell_cost(self, name: str, info: CardInfo, rest: str) -> Cost:
        """Mana actually paid for a spell, according to how the log says it was cast."""
        text = info.text.lower()
        if "without paying" in rest:
            return Cost()
        m = re.search(r"by paying (\{[^}]*\})", rest)
        if m:                                              # warp, dash...
            return parse_braced_cost(m.group(1))
        if "Flashback" in rest:
            m = re.search(r"Flashback (\{[^}]*\})", rest)
            if m:
                return parse_braced_cost(m.group(1))
            return parse_mtgo_cost(info.cost) if "equal to its mana cost" in rest else Cost()
        if "escape cost" in rest:
            m = re.search(r"escape\s*—\s*((?:\{[^}]*\})+)", text)
            return parse_braced_cost(m.group(1)) if m else Cost()
        if "evoke" in rest:
            m = re.search(r"evoke\s*—?\s*((?:\{[^}]*\})+)", text)
            return parse_braced_cost(m.group(1)) if m else Cost()
        if rest.lstrip().startswith("by "):                # pitch / alternative costs (Force of Negation, ...)
            return Cost()
        cost = parse_mtgo_cost(info.cost)
        if "kicker" in rest:
            m = re.search(r"kicker ((?:\{[^}]*\})+)", text)
            if m:
                k = parse_braced_cost(m.group(1))
                cost.generic += k.generic
                cost.pips += k.pips
        if "delve" in text and self.last_exile_cost and self.last_exile_cost[0] == name \
                and self.last_exile_cost[2] >= self._i - 1:
            cost.generic = max(0, cost.generic - self.last_exile_cost[1])
        return cost

    def pay_mana(self, player: str, cost: Cost, x: int = 0, exclude: Obj | None = None):
        """Estimate which untapped permanents were tapped for mana (the log never says)."""
        if not cost or player not in self.players:
            return
        sources = []
        for o in self.objs.values():
            if o.zone != "battlefield" or o.controller != player or o.tapped or o is exclude or o.attacking:
                continue
            info = self.info(o.name)
            colors = produced_colors(o.name, info.text, o.placeholder)
            if colors:
                sources.append((o, colors, info.has("Land") or o.name is None))
        chosen, _ = choose_sources(cost, x, sources)
        for o in chosen:
            o.tapped, o.tap_guess = True, True

    def targets_of(self, ev: Event, refs: list[CardRef], text: str) -> list:
        n = ev.info.get("n_targets", 0)
        refs = refs[len(refs) - n:] if n else []
        out = []
        t = text.lower()
        for r in refs:
            o = self.by_ref(r)
            if o is None:
                in_gy = re.search(r"target [^.]*?cards? (?:in|from) (?:your|a|their|an opponent's|target player's) graveyard", t)
                zone = "graveyard" if in_gy else "battlefield"
                if re.search(r"\btarget (?:[\w-]+,? (?:or )?){0,4}spells?\b", t):
                    zone = "stack"
                o = self.see(r, zone)
            elif o.zone == "stack" and not re.search(r"\btarget (?:[\w-]+,? (?:or )?){0,4}spells?\b", t):
                self.resolve_through(lambda it, u=o.uid: it.obj_uid == u)
            out.append(("obj", o.uid))
        for p in ev.players:
            out.append(("player", p))
        return out

    def on_activate(self, ev: Event):
        ref = ev.cards[0]
        text = ev.info.get("text", "")
        o = self.by_ref(ref)
        if o is None:
            zone = "battlefield"
            cost = self.ability_cost(ref.name, text)
            if "discard this" in cost or "discard ~" in cost or re.search(r"discard [a-z' ,]*" + re.escape(ref.name.lower()), cost):
                zone = "hand"
            o = self.see(ref, zone, ev.actor) if zone == "battlefield" else self.take(ref, ("hand",), ev.actor)
        cost = self.ability_cost(o.name, text)
        it = StackItem("ability", ev.actor, o.name or ref.name, self.full_text(o.name or ref.name, text), source_uid=o.uid, source_iid=ref.iid)
        it.targets = self.targets_of(ev, ev.cards[1:], text)
        it.target_controllers = [self.objs[v].controller for k, v in it.targets if k == "obj"]
        m = re.search(r"pay (\d+) life", cost)
        if m:
            self.change_life(ev.actor, -int(m.group(1)), f"{o.label()} cost")
        paid = parse_braced_cost(cost.split(":")[0])
        if paid.tap and o.zone == "battlefield":
            o.tapped, o.tap_guess = True, False
        if "{q}" in cost and o.zone == "battlefield":
            o.tapped = o.tap_guess = False
        self.pay_mana(ev.actor, paid, 0, exclude=o)
        if re.search(r"sacrifice (this|~|" + re.escape((o.name or "").lower()) + ")", cost) or re.search(r"sacrifice (this|it)\b", cost):
            self.move(o, "graveyard")
        elif re.search(r"exile (this|~|" + re.escape((o.name or "").lower()) + r")\b", cost):
            self.move(o, "exile")
        elif o.zone == "hand" and "discard" in cost:
            self.to_zone(o, "graveyard")
        self.stack.append(it)

    def full_text(self, name: str | None, logged: str) -> str:
        """MTGO truncates long ability texts with '...'; recover the oracle paragraph."""
        if not logged.endswith("...") and not logged.endswith(".."):
            return logged
        prefix = logged.rstrip(".").strip().lower()[:40]
        for para in self.info(name).text.split("\n"):
            eff = para.split(":", 1)[1].strip() if ":" in para and not para.lower().startswith(("when", "whenever", "at ")) else para
            if eff.lower().startswith(prefix):
                return eff
        return logged

    def ability_cost(self, name: str | None, logged: str) -> str:
        """The cost part of the oracle line whose effect matches the logged ability text."""
        info = self.info(name)
        want = logged.rstrip(".").removesuffix("...").strip().lower()[:40]
        for line in info.text.split("\n"):
            if ":" in line:
                cost, eff = line.split(":", 1)
                if want and eff.strip().lower().startswith(want[:25]):
                    return cost.lower().replace(name.lower() if name else "\0", "~")
        return ""

    def on_ninjutsu(self, ev: Event):
        o = self.take(ev.cards[0], ("hand",), ev.actor)
        self.to_zone(o, "battlefield", keep_iid=True)
        o.attacking = True
        for c in self.combat:
            a = self.objs.get(c["att"])
            if a is not None and a.controller == ev.actor and not c["blockers"] and a.zone == "battlefield":
                self.to_zone(a, "hand")
                self.combat.append({"att": o.uid, "def": c["def"], "blockers": []})
                self.note(f"{a.label()} returns to hand (ninjutsu)")
                break

    def on_trigger(self, ev: Event):
        text = ev.info.get("text") or ""
        src = ev.info.get("src")
        if src == C:
            ref = ev.cards[0]
            o = self.by_ref(ref)
            etb = re.match(r"when(?:ever)? [^,]*\benters\b", text.lower()) is not None
            if o is None and etb:
                on_bf = [c for c in self.candidates(ref, ("battlefield",), ev.actor) if c.zone == "battlefield"]
                if not on_bf:
                    # e.g. Goryo's Vengeance returning it: that spell resolved
                    self.resolve_through(lambda it: any(k == "obj" and v in self.objs and self.objs[v].name == ref.name
                                                        for k, v in it.targets))
                o = self.see(ref, "battlefield", ev.actor)
            elif o is not None and etb and o.zone != "battlefield":
                o = self.see(ref, "battlefield", ev.actor)
            if o is None:
                zones = ("exile",) if text.lower().startswith("rebound") else ("battlefield", "exile", "graveyard", "hand")
                cands = [c for c in self.candidates(ref, zones, ev.actor) if c.zone in zones]
                if cands:
                    o = cands[0]
                    self.adopt(o, ref.iid)
                else:
                    o = self.see(ref, zones[0], ev.actor)
            name = ref.name
            src_uid = o.uid if o else None
            tgt_refs = ev.cards[1:]
        else:
            name, src_uid, tgt_refs = src, None, ev.cards
            ref = None
        it = StackItem("ability", ev.actor, name, self.full_text(name, text), source_uid=src_uid, source_iid=ref.iid if ref else None)
        if src_uid is not None:
            it.source_inc = self.objs[src_uid].inc
        it.targets = self.targets_of(ev, tgt_refs, text)
        it.target_controllers = [self.objs[v].controller for k, v in it.targets if k == "obj"]
        self.stack.append(it)

    def on_discard(self, ev: Event):
        o = self.take(ev.cards[0], ("hand",), ev.actor)
        self.to_zone(o, "graveyard")

    def on_to_graveyard(self, ev: Event):
        for ref in ev.cards:
            o = self.take(ref, ("library",), ev.actor, hidden="library")
            self.to_zone(o, "graveyard")

    def on_mill(self, ev: Event):
        for ref in ev.cards:
            if self.by_ref(ref) is None:
                o = self.new_obj(ref.name, ev.actor, "graveyard", ref.iid)
                self.leave_hidden(ev.actor, "library")
                p = self.players[ev.actor]
                if p.known_top:
                    p.known_top.pop(0)

    def on_exile_cost(self, ev: Event):
        src_ref = ev.cards[-1]
        text = self.info(src_ref.name).text.lower()
        nxt = self.events[ev.index + 1] if ev.index + 1 < len(self.events) else None
        hand_cost = re.search(r"exile [^.:]*from your hand", text) is not None and "from your graveyard" not in text
        if nxt is not None and nxt.kind == "cast" and "from your hand" in nxt.info.get("rest", ""):
            hand_cost = True
        # MTGO's actor on these lines is not always the payer; the caster of the source is.
        owner = ev.actor
        if nxt is not None and nxt.kind == "cast" and nxt.cards and nxt.cards[0].name == src_ref.name:
            owner = nxt.actor
        else:
            src = self.by_ref(src_ref) or next(iter(self.candidates(src_ref, ("stack", "battlefield", "graveyard"))), None)
            if src is not None:
                owner = src.controller
        for ref in ev.cards[:-1]:
            o = self.take(ref, ("hand",) if hand_cost else ("graveyard", "hand"), owner, hidden="hand" if hand_cost else "late")
            self.to_zone(o, "exile")
        self.last_exile_cost = (src_ref.name, len(ev.cards) - 1, self._i)

    def on_exile_own(self, ev: Event):
        o = self.see(ev.cards[0], "battlefield", ev.actor)
        self.move(o, "exile")
        if self.stack and self.stack[-1].source == o.name and "warp" in self.stack[-1].text.lower():
            o.exile_castable = True
            o.note = "warped: castable from exile"
        if self.stack and self.stack[-1].source == o.name:
            self.stack[-1].resolving = True

    def on_exile_with(self, ev: Event):
        src = ev.cards[-1]
        if len(ev.cards) == 2 and ev.cards[0].iid == src.iid:
            # "exiles Relic with Relic": the cost of its own ability, activated next
            nxt = self.events[ev.index + 1] if ev.index + 1 < len(self.events) else None
            if nxt is not None and nxt.kind == "activate" and nxt.cards and nxt.cards[0].iid == src.iid:
                return
        for ref in ev.cards[:-1]:
            o = self.by_ref(ref) or self.take(ref, ("battlefield", "graveyard", "hand"), None, hidden="late")
            self.to_zone(o, "exile")
            self.mark_affected(o)

    def on_suspend(self, ev: Event):
        o = self.take(ev.cards[0], ("hand",), ev.actor)
        self.to_zone(o, "exile", keep_iid=True)
        o.counters["time"] = ev.n

    def mark_affected(self, o: Obj):
        for it in self.stack:
            if it.resolving:
                it.affected.add(o.uid)

    def on_counter_spell(self, ev: Event):
        what = ev.info.get("what")
        if what == C:
            o = self.by_ref(ev.cards[0])
            if o is not None:
                idx = self.stack_find(lambda it: it.obj_uid == o.uid)
                flashback = False
                if idx is not None:
                    # only the countered spell leaves; the counterspell itself is the one resolving
                    flashback = "flashback" in self.stack[idx].how
                    self.stack.pop(idx)
                src = ev.info.get("src")
                src_text = self.info(ev.cards[-1].name).text.lower() if src == C and len(ev.cards) > 1 else ""
                dest = "exile" if flashback or "exile it instead" in src_text else "graveyard"
                self.move(o, dest)
                self.note(f"{o.label()} is countered → {dest}")
        else:
            m = re.match(r"Triggered Ability from (.+)", what or "")
            if m:
                idx = self.stack_find(lambda it: it.kind == "ability" and it.source == m.group(1))
                if idx is not None:
                    self.resolve_above(idx)
                    self.stack.pop(idx)

    def on_fizzle(self, ev: Event):
        what = ev.info.get("what")
        if what == C:
            o = self.by_ref(ev.cards[0])
            if o is not None:
                idx = self.stack_find(lambda it: it.obj_uid == o.uid)
                if idx is not None:
                    self.resolve_above(idx)
                    self.stack.pop(idx)
                self.move(o, "graveyard")
        else:
            name = re.sub(r"^Triggered Ability from ", "", what or "")
            idx = self.stack_find(lambda it: it.source == name)
            if idx is not None:
                self.resolve_above(idx)
                self.stack.pop(idx)

    def on_return_hand(self, ev: Event):
        refs = ev.cards[:-1] if ev.info.get("src") == C else ev.cards
        for ref in refs:
            o = self.by_ref(ref)
            if o is None:
                cands = self.candidates(ref, ("battlefield", "graveyard", "exile", "stack"))
                o = cands[0] if cands else self.new_obj(ref.name, ev.actor, "battlefield")
            self.mark_affected(o)
            self.to_zone(o, "hand")
            self.adopt(o, ref.iid)

    def on_put_top(self, ev: Event):
        ref = ev.cards[0]
        o = self.by_ref(ref)
        if o is None:
            cands = self.candidates(ref, ("stack", "battlefield", "hand", "graveyard"))
            o = cands[0] if cands else self.new_obj(ref.name, ev.actor, "hand")
        if o.zone == "stack":
            idx = self.stack_find(lambda it: it.obj_uid == o.uid)
            if idx is not None:
                self.stack.pop(idx)
        self.mark_affected(o)
        self.to_zone(o, "library")
        self.adopt(o, ref.iid)
        self.players[o.owner].known_top.insert(0, o.uid)

    def on_put_battlefield(self, ev: Event):
        ref = ev.cards[0]
        o = self.by_ref(ref)
        if o is None:
            cands = [c for c in self.candidates(ref, ("hand", "graveyard", "exile")) if c.zone in ("hand", "graveyard", "exile")]
            o = cands[0] if cands else None
            if o is None:
                ph = self.consume_placeholder(ref, ev.actor)
                if ph is not None:
                    return
                o = self.new_obj(ref.name, ev.actor, "hand" if self.turn == 0 else "library")
        self.mark_affected(o)
        self.to_zone(o, "battlefield")
        self.adopt(o, ref.iid)
        o.controller = ev.actor
        if ev.info.get("att"):
            o.attacking = True

    def on_reveal_opening(self, ev: Event):
        self.take(ev.cards[0], ("hand",), ev.actor)

    def on_reveal_many(self, ev: Event):
        src = ev.cards[0]
        text = self.info(src.name).text.lower()
        zone = "hand" if re.search(r"reveals? (?:their|your|his or her) hand|look at target (?:player|opponent)'s hand", text) else "library"
        shown = []
        for ref in ev.cards[1:]:
            o = self.by_ref(ref) or self.take(ref, (zone,), ev.actor, hidden=zone)
            shown.append(o.uid)
        if zone == "hand":
            return
        self.reveal = {"src": src.name, "objs": shown, "player": ev.actor}

    def on_reveal(self, ev: Event):
        ref = ev.cards[0]
        if self.by_ref(ref) is None:
            zone = "library" if self.stack and "top" in self.stack[-1].text.lower() else "hand"
            self.take(ref, (zone,), ev.actor, hidden=zone)

    def on_choose_cards(self, ev: Event):
        names = ev.info.get("names", "")
        if C in names or " for " in names:
            return
        if self.reveal is None:
            return
        rv, self.reveal = self.reveal, None
        pool = [self.objs[u] for u in rv["objs"] if self.objs[u].zone == "library"]
        # card names can contain commas ("Teferi, Time Raveler"): match against the revealed names
        chosen, rest_txt = [], names
        for o in sorted(pool, key=lambda x: -len(x.name or "")):
            if o.name and o.name in rest_txt:
                chosen.append(o.name)
                rest_txt = rest_txt.replace(o.name, "", 1)
        text = self.info(rv["src"]).text.lower()
        rest_zone = "graveyard" if "rest into your graveyard" in text else "library"
        rest_top = re.search(r"rest (?:back )?on top", text) is not None
        dest_zone = "battlefield" if "onto the battlefield" in text and "into your hand" not in text else "hand"
        for name in chosen:
            o = next((x for x in pool if x.name == name), None)
            if o is not None:
                pool.remove(o)
                self.to_zone(o, dest_zone)
        for o in pool:
            if rest_zone == "graveyard":
                self.to_zone(o, "graveyard")
        p = self.players[rv["player"]]
        p.known_top = [u for u in p.known_top if self.objs[u].zone == "library"]
        if rest_zone == "library":
            for o in pool:
                if o.uid in p.known_top:
                    p.known_top.remove(o.uid)
                (p.known_top if rest_top else p.known_bottom).append(o.uid)
        where = "graveyard" if rest_zone == "graveyard" else ("top of library" if rest_top else "bottom of library")
        self.note(f"to {dest_zone}: {', '.join(chosen)}; the rest → {where}")

    def on_create(self, ev: Event):
        what = ev.info.get("what", "")
        n = ev.n or 1
        if what.lower().startswith("emblem"):
            self.players[ev.actor].counters["emblem"] += 1
            return
        name = what
        if n > 1 and name.endswith("s"):
            name = name[:-1]
        if not name.endswith("Token") and self.db.known(name + " Token"):
            name = name + " Token"
        attached = None
        if len(ev.cards) > 1 and "attached" in ev.text:
            tgt = self.by_ref(ev.cards[-1])
            attached = tgt.uid if tgt else None
            name = name if name.endswith("Role") or name.endswith("Token") else f"{name} Role"
        maker = self.by_ref(ev.cards[0]) if ev.cards else None
        for _ in range(n):
            o = self.new_obj(name, ev.actor, "battlefield", token=True)
            o.attached_to = attached
            if maker is not None:
                self.last_token_by[maker.uid] = o.uid

    def on_other(self, ev: Event):
        # "X chooses to use Cori-Steel Cutter's ability": the optional "you may attach this Equipment to it"
        if "chooses to use" in ev.text and ev.cards:
            src = self.by_ref(ev.cards[0])
            if src is not None and "you may attach this equipment to it" in self.info(src.name).text.lower():
                token = self.objs.get(self.last_token_by.get(src.uid, -1))
                if token is not None:
                    self.attach(src, token, "optional attach")

    def on_counter_on(self, ev: Event):
        what = ev.info.get("what", "")
        parts = re.findall(r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+) ([^ ]+(?: [^ ]+)?)", what)
        adds = Counter()
        for num, kind in parts:
            kind = kind.replace(" and", "").replace(" counter", "").strip()
            adds[kind] += number(num)
        target = ev.info.get("target", "")
        if C in target:
            for ref in ev.cards:
                o = self.by_ref(ref)
                if o is None and "time" in adds:
                    o = self.take(ref, ("exile",), ev.actor, hidden="hand")
                    if o.zone != "exile":
                        self.to_zone(o, "exile", keep_iid=True)
                        o.note = "suspended"
                o = o or self.see(ref, "battlefield")
                o.counters.update(adds)
        else:
            p = self.players.get(target.strip())
            if p is not None:
                p.counters.update(adds)

    def on_counter_off(self, ev: Event):
        kind = ev.info.get("what", "").strip()
        n = ev.n or 1
        target = ev.info.get("target", "")
        if C in target:
            o = self.by_ref(ev.cards[0]) or self.see(ev.cards[0], "battlefield")
            o.counters[kind] = max(0, o.counters.get(kind, 0) - n)
            if kind == "loyalty" and o.counters[kind] == 0 and self.info(o.name).has("Planeswalker"):
                pass   # the ability cost is paid first; the walker dies right after (handled on next event)
        else:
            p = self.players.get(target.strip())
            if p is not None:
                p.counters[kind] = max(0, p.counters.get(kind, 0) - n)
                if kind == "energy":
                    for it in self.stack:
                        if it.resolving or it is self.stack[-1]:
                            it.energy_paid += n

    def on_attack(self, ev: Event):
        defender_player = ev.players[0] if ev.players else None
        refs = ev.cards
        if ev.info.get("defender_card"):
            pw = self.see(refs[0], "battlefield")
            defender = ("obj", pw.uid)
            refs = refs[1:]
        else:
            defender = ("player", defender_player)
        attacker_side = self.opponent(defender_player) if defender_player else self.active
        for ref in refs:
            o = self.see(ref, "battlefield", attacker_side)
            o.attacking = True
            info = self.info(o.name)
            if "VIGILANCE" not in info.keywords and not re.search(r"(?:^|\n)vigilance", info.text.lower()):
                o.tapped, o.tap_guess = True, False
            self.combat.append({"att": o.uid, "def": defender, "blockers": []})
        self.combat_done = False

    def on_block(self, ev: Event):
        blocker, attacker = ev.cards[0], ev.cards[1]
        b = self.see(blocker, "battlefield")
        a = self.see(attacker, "battlefield")
        for c in self.combat:
            if c["att"] == a.uid:
                c["blockers"].append(b.uid)
        b.note = f"blocking {a.label()}"

    def on_transform(self, ev: Event):
        ref = ev.cards[0]
        o = self.by_ref(ref)
        if o is None:
            back = [c for c in self.candidates(ref, ("exile", "graveyard")) if c.zone in ("exile", "graveyard")]
            if back:
                o = back[0]
                self.move(o, "battlefield")
                self.adopt(o, ref.iid)
                self.note(f"{ref.name} returns to the battlefield transformed")
        o = o or self.see(ref, "battlefield")
        o.front = o.front or o.name
        o.name = ev.cards[1].name
        info = self.info(o.name)
        if info.has("Planeswalker") and info.loyalty and info.loyalty.isdigit() and not o.counters.get("loyalty"):
            o.counters["loyalty"] = 0

    def on_plotted(self, ev: Event):
        o = self.by_ref(ev.cards[0]) or self.take(ev.cards[0], ("hand",), None)
        self.to_zone(o, "exile", keep_iid=True)
        o.exile_castable = True
        o.note = "plotted"

    def on_life_gain(self, ev: Event):
        p = self.players[ev.actor]
        p.life += ev.n

    def on_life_loss(self, ev: Event):
        p = self.players[ev.actor]
        p.life -= ev.n

    def on_fail_search(self, ev: Event):
        for it in reversed(self.stack):
            if "search" in it.text.lower():
                it.failed_search = True
                break

    def on_cycle(self, ev: Event):
        o = self.take(ev.cards[0], ("hand",), ev.actor)
        self.to_zone(o, "graveyard")
