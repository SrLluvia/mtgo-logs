"""Mana costs and a best guess of which permanents were tapped to pay them.

The game log never records mana, so paying is estimated: colored pips first,
each from the source with the fewest alternatives, then generic mana from
whatever is least flexible, keeping lands before other sources.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

COLORS = "WUBRG"
BASIC_COLORS = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G", "Wastes": "C"}


@dataclass
class Cost:
    generic: int = 0
    pips: list[str] = field(default_factory=list)   # each entry: the colors that can pay it ("U", "WU", "C")
    x: int = 0                                       # number of {X} in the cost
    tap: bool = False                                # {T}

    def total(self, x_value: int = 0) -> int:
        return self.generic + len(self.pips) + self.x * x_value

    def __bool__(self):
        return bool(self.generic or self.pips or self.x)


def _add_symbol(cost: Cost, sym: str):
    sym = sym.upper()
    if not sym:
        return
    if sym.isdigit():
        cost.generic += int(sym)
    elif sym == "X":
        cost.x += 1
    elif sym == "T":
        cost.tap = True
    elif sym in COLORS or sym == "C":
        cost.pips.append(sym)
    elif "/" in sym:
        a, b = sym.split("/", 1)
        if b == "P":                                  # phyrexian: usually paid with life, tap nothing
            pass
        elif a == "2":                                # {2/W}: one W (cheaper than 2 generic)
            cost.pips.append(b)
        else:
            cost.pips.append(a + b)                   # hybrid


def parse_mtgo_cost(s: str) -> Cost:
    """MTGO's database format: '5UU', 'X#bp-', '#gu-', '#2w-', 'f' (hex generic)."""
    cost = Cost()
    for tok in re.findall(r"#[^-]*-|[0-9a-f]|[WUBRGCX]", s or ""):
        if tok.startswith("#"):
            body = tok[1:-1].upper()
            if body.startswith("2"):
                cost.pips.append(body[1:])
            elif body.endswith("P"):                  # phyrexian: usually paid with life
                pass
            elif body.startswith("C"):
                cost.pips.append(body[1:])            # {C/x}: pay with the color
            else:
                cost.pips.append(body)
        elif tok in "0123456789abcdef":
            cost.generic += int(tok, 16)
        else:
            _add_symbol(cost, tok)
    return cost


def parse_braced_cost(s: str) -> Cost:
    """Oracle / log format: '{1}{W}, {T}' or MTGO's compact '{1WU}'."""
    cost = Cost()
    for body in re.findall(r"\{([^}]*)\}", s or ""):
        if "#" in body:                                # MTGO's own notation inside braces: '{#ur-}'
            part = parse_mtgo_cost(body)
            cost.generic += part.generic
            cost.pips += part.pips
            cost.x += part.x
            continue
        body = body.upper()
        if "/" in body or body.isdigit() or len(body) == 1:
            _add_symbol(cost, body)
        else:                                          # compact: '1WU', '3UB', 'NR'
            for num, letter in re.findall(r"(\d+)|([WUBRGCXT])", body):
                _add_symbol(cost, num or letter)
    return cost


def produced_colors(name: str | None, text: str, placeholder: str | None = None) -> str:
    """Colors a permanent can tap for ('' if it isn't a mana source; '*' = any)."""
    if name in BASIC_COLORS:
        return BASIC_COLORS[name]
    if name and name.startswith("Snow-Covered "):
        return BASIC_COLORS.get(name[len("Snow-Covered "):], "")
    if name is None and placeholder:                  # unknown fetched land: the types it was fetched for
        found = "".join(c for w, c in BASIC_COLORS.items() if w.lower() in placeholder.lower())
        return found or ("*" if "land" in placeholder.lower() else "")
    t = text.lower()
    out = ""
    for m in re.finditer(r"\{t\}(?:, [^:]*)?: add ([^.]*)", t):
        eff = m.group(1)
        if "sacrifice" in m.group(0).split(":")[0]:
            continue
        if "any color" in eff or "any type" in eff:
            return "*"
        out += "".join(c.upper() for c in re.findall(r"\{([wubrgc])\}", eff))
    return "".join(dict.fromkeys(out))


def choose_sources(cost: Cost, x_value: int, sources: list[tuple]) -> tuple[list, int]:
    """Pick which sources pay `cost`.

    `sources`: (obj, colors, is_land). Returns (chosen objects, mana still unpaid).
    """
    pool = list(sources)

    def can(colors: str, need: str) -> bool:
        return colors == "*" or any(c in colors for c in need)

    def flexibility(src) -> tuple:
        colors = src[1]
        return (0 if src[2] else 1, 6 if colors == "*" else len(colors))

    chosen = []
    for need in sorted(cost.pips, key=lambda n: sum(can(s[1], n) for s in pool)):
        options = [s for s in pool if can(s[1], need)]
        if not options:
            continue
        pick = min(options, key=flexibility)
        pool.remove(pick)
        chosen.append(pick[0])
    # a pip no source can pay came from somewhere we can't see: don't tap extra lands for it
    for _ in range(cost.generic + cost.x * x_value):
        if not pool:
            break
        pick = min(pool, key=flexibility)
        pool.remove(pick)
        chosen.append(pick[0])
    return chosen, cost.total(x_value) - len(chosen)
