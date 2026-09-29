"""Object identity and zone changes.

MTGO gives a card a new object id on every zone change and hands ids out in
increasing order: these helpers find which tracked object a log line talks about,
move objects between zones (keeping hidden-zone counts right) and patch already
recorded states when the log later proves a card moved without saying so.
"""
from __future__ import annotations

import re
from collections import Counter

from .carddb import CardInfo
from .events import C, CardRef
from .model import HIDDEN, WARN, Obj


class IdentityMixin:
    """Mixed into GameEngine (see engine.py for the shared state)."""

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
        self.retro(o, from_zones[0], ref.iid)
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
        self.retro(o, zone, ref.iid)

    def retro(self, o: Obj, zone: str, iid: int):
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
        self.retro(o, zone, ref.iid)
        return o

    def zone_label(self, zone: str, owner: str) -> str:
        return f"{owner}'s {zone}"
