"""Inferring the log player's hand from cards that later leave it.
"""
from __future__ import annotations


from .events import C, CardRef
from .model import Obj


class HandInferenceMixin:
    """Mixed into GameEngine (see engine.py for the shared state)."""

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
