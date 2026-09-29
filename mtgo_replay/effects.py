"""Oracle-text effects of resolving spells and abilities, P/T and attachments.
"""
from __future__ import annotations

import re
from collections import Counter

from .model import WARN, Obj, StackItem


class EffectsMixin:
    """Mixed into GameEngine (see engine.py for the shared state)."""

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
            self.destroy(o, f"{it.source}", True)
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
