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

import re
from collections import Counter

from .carddb import CardDB, CardInfo
from .combat import CombatMixin
from .effects import EffectsMixin
from .events import C, CardRef, Event, number
from .hands import HandInferenceMixin
from .identity import IdentityMixin
from .mana import parse_braced_cost
from .model import HIDDEN, WARN, Obj, PlayerState, StackItem, Step
from .payments import PaymentMixin
from .stack import StackMixin

__all__ = ["GameEngine", "Step", "WARN"]


class GameEngine(IdentityMixin, HandInferenceMixin, StackMixin, EffectsMixin, CombatMixin, PaymentMixin):
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
        if o.zone == "battlefield" and "loyalty" in o.counters and o.counters["loyalty"] <= 0                 and self.info(o.name).has("Planeswalker"):
            # paying a loyalty cost down to 0: state-based action, the ability still resolves
            self.destroy(o, "no loyalty left after activating", True)

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
                self.new_obj(ref.name, ev.actor, "graveyard", ref.iid)
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
