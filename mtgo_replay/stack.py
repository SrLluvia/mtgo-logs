"""The stack: when spells and abilities resolve.

The log never says a spell resolved; it is inferred from what happens next
(a new turn, a land drop, an effect "with X", a trigger from the permanent...).
"""
from __future__ import annotations

import re

from .events import C, CardRef, Event
from .model import StackItem

# which effect lines can belong to a resolving item, by keyword in its text
_EFFECT_KEYWORDS = {
    "mill": ("mill",), "to_graveyard": ("surveil", "graveyard", "mill"), "to_top": ("surveil", "scry", "top"),
    "scry": ("scry",), "discard": ("discard",), "fail_search": ("search",), "choose_cards": ("reveal", "look", "choose"),
    "draw": ("draw",), "life_gain": ("gain",), "life_loss": ("lose",), "counter_on": ("counter",),
    "counter_off": ("counter", "energy"), "reveal_many": ("reveal", "look"), "put_battlefield": ("battlefield",),
    "return_none": ("return",), "create": ("create",), "mulligan": (),
}


class StackMixin:
    """Mixed into GameEngine (see engine.py for the shared state)."""

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
            _, src_ref = self.source_of(ev)
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
