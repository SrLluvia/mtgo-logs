"""Render reconstructed games as a readable .txt and a frontend-friendly .json."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .carddb import CardDB
from .engine import Step, WARN

ZONE_ORDER = ("battlefield", "hand", "graveyard", "exile", "library")
LEGEND = (f"Legend: (inferred) = not in the log, follows from the rules  ·  {WARN} = estimate / guess  ·  "
          "≈ = approximate value  ·  [exact] = state taken from MTGO's own snapshot  ·  "
          "tapped? = tapped for mana (estimate: the log never says which lands paid)")


def card_view(uid, o: dict, db: CardDB, objects: dict) -> dict:
    v = {"id": uid, "name": o["name"] or "unknown card"}
    if o.get("placeholder") and not o["name"]:
        v["name"] = f"? ({o['placeholder']})"
    if o["counters"]:
        v["counters"] = {k: n for k, n in o["counters"].items() if n}
    info = db.info(o["name"]) if o["name"] else None
    if info:
        v["card"] = o["name"]              # real card name (for images); "name" is the display label
        v["types"] = [t for t in info.types if t not in ("Legendary", "Basic")]
    if info and info.has("Creature") and o["zone"] == "battlefield":
        p, t = info.int_power(), info.int_toughness()
        plus = o["counters"].get("+1/+1", 0) - o["counters"].get("-1/-1", 0)
        if p is not None and t is not None:
            v["pt"] = f"{p + plus}/{t + plus}"
    for k in ("token", "attacking", "face_down", "uncertain"):
        if o.get(k):
            v[k] = True
    if o["zone"] == "battlefield" and o.get("tapped"):
        v["tapped"] = True
        if o.get("tap_guess"):
            v["tap_guess"] = True          # tapped for mana according to our estimate
    if o.get("note"):
        v["note"] = o["note"]
    if o.get("attached_to") and o["attached_to"] in objects:
        v["attached_to"] = objects[o["attached_to"]]["name"]
    if o["controller"] != o["owner"]:
        v["owner"] = o["owner"]
    return v


def state_view(state: dict, db: CardDB) -> dict:
    objects = state["objects"]

    def lib_pos(c):
        return objects[c["id"]].get("lib_pos")
    players = {}
    for name, p in state["players"].items():
        zones = {z: [] for z in ZONE_ORDER}
        for uid, o in objects.items():
            who = o["controller"] if o["zone"] == "battlefield" else o["owner"]
            if who == name and o["zone"] in zones:
                zones[o["zone"]].append(card_view(uid, o, db, objects))
        known_hand = len(zones["hand"])
        players[name] = {
            "life": p["life"], "life_approx": p["life_approx"],
            "hand_count": max(p["hand"], known_hand), "hand_known": zones["hand"],
            "library_count": p["library"], "library_approx": p["library_approx"],
            # only cards whose position is known: put on top (in order) or at the bottom
            "library_top": sorted((c for c in zones["library"] if (lib_pos(c) or "").startswith("top")),
                                  key=lambda c: int(lib_pos(c)[3:])),
            "library_bottom": [c for c in zones["library"] if lib_pos(c) == "bottom"],
            "counters": {k: n for k, n in p["counters"].items() if n},
            "battlefield": zones["battlefield"], "graveyard": zones["graveyard"], "exile": zones["exile"],
        }
        if p.get("exact"):
            players[name]["exact"] = True
    return {"players": players, "stack": state["stack"]}


def _fmt_card(c: dict) -> str:
    s = c["name"]
    extra = []
    if c.get("counters"):
        extra += [f"{k} ×{n}" if k not in ("loyalty",) else f"loyalty {n}" for k, n in c["counters"].items()]
    if c.get("pt") and c.get("counters", {}).get("+1/+1") or c.get("pt") and c.get("counters", {}).get("-1/-1"):
        extra.append(c["pt"])
    if c.get("attacking"):
        extra.append("attacking")
    if c.get("tapped"):
        extra.append("tapped?" if c.get("tap_guess") else "tapped")
    if c.get("attached_to"):
        extra.append(f"on {c['attached_to']}")
    if c.get("owner"):
        extra.append(f"owned by {c['owner']}")
    if extra:
        s += " [" + ", ".join(extra) + "]"
    if c.get("note"):
        s += f" ({c['note']})"
    return s


def _fmt_list(cards: list[dict]) -> str:
    counts = Counter(_fmt_card(c) for c in cards)
    return ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in counts.items())


def render_state_lines(view: dict, order: list[str]) -> list[str]:
    out = []
    width = max(len(p) for p in order)
    for name in order:
        p = view["players"][name]
        life = f"{'≈' if p['life_approx'] else ''}{p['life']}"
        lib = f"{'~' if p['library_approx'] else ''}{p['library_count']}"
        head = f"  {name:<{width}}  life {life} · hand {p['hand_count']} · library {lib}"
        if p["counters"]:
            head += " · " + ", ".join(f"{k} {n}" for k, n in p["counters"].items())
        if p.get("exact"):
            head += "  [exact]"
        out.append(head)
        rows = [("battlefield", p["battlefield"]), ("hand (known)", p["hand_known"]),
                ("graveyard", p["graveyard"]), ("exile", p["exile"]),
                ("library top", p["library_top"]), ("library bottom", p["library_bottom"])]
        for label, cards in rows:
            if cards:
                out.append(f"      {label:<15} {_fmt_list(cards)}")
    if view["stack"]:
        items = []
        for it in reversed(view["stack"]):
            s = f"{it['source']}" + (" (ability)" if it["kind"] == "ability" else "") + f" by {it['controller']}"
            if it["targets"]:
                s += " → " + ", ".join(str(t) for t in it["targets"])
            items.append(s)
        out.append("  stack (top first): " + " | ".join(items))
    return out


def write_game(path_txt: Path, path_json: Path, header: dict, steps: list[Step], db: CardDB):
    order = header["players"]
    lines = [
        "MTGO GAME REVIEW",
        f"{header['date']}  ·  {' vs '.join(order)}  ·  game {header['game']} of the match",
        f"On the play: {header.get('on_play') or '?'}  ·  Winner: {header.get('winner') or '?'}",
    ]
    for p, d in header.get("decks", {}).items():
        lines.append(f"{p}'s deck: {d}")
    lines += [f"State source: {header['source']}", LEGEND, ""]
    json_steps = []
    for st in steps:
        ev = st.event
        view = state_view(st.state, db)
        if ev.kind == "turn":
            lines += ["", f"══════════ TURN {st.turn} · {st.active} ══════════", ""]
            json_steps.append({"index": st.index, "time": ev.time.isoformat(), "turn": st.turn, "active": st.active,
                               "kind": ev.kind, "text": ev.text, "notes": st.notes, "state": view})
            continue
        lines.append(f"[#{st.index:03d} T{st.turn}] {ev.text}")
        for n in st.notes:
            lines.append(f"    → {n}")
        lines += render_state_lines(view, order)
        lines.append("")
        json_steps.append({"index": st.index, "time": ev.time.isoformat(), "turn": st.turn, "active": st.active,
                           "kind": ev.kind, "text": ev.text, "notes": st.notes, "state": view})
    path_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path_json.write_text(json.dumps({"header": header, "steps": json_steps}, ensure_ascii=False, indent=1), encoding="utf-8")
