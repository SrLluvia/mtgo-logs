"""Mana paid for spells and abilities, and which permanents were tapped for it (estimated).
"""
from __future__ import annotations

import re

from .carddb import CardInfo
from .events import C
from .mana import Cost, choose_sources, parse_braced_cost, parse_mtgo_cost, produced_colors
from .model import Obj


class PaymentMixin:
    """Mixed into GameEngine (see engine.py for the shared state)."""

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
