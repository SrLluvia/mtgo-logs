"""Combat damage, estimated from attackers, blockers and P/T (the log never records it).
"""
from __future__ import annotations

from collections import Counter

from .model import WARN


class CombatMixin:
    """Mixed into GameEngine (see engine.py for the shared state)."""

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
