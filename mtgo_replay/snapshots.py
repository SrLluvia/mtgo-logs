"""Merge exact MTGO client-log snapshots into reconstructed states.

Snapshots are stamped to the second and arrive every 1-5 seconds, so a
snapshot is only trusted for a step when no other log line happened inside
that second.  Those steps become exact; for the steps in between, the
difference between snapshot and reconstruction (e.g. an unlogged shockland
payment) is carried forward so life / hand / library stay right.
"""
from __future__ import annotations

import bisect
import datetime as dt

from collections import Counter

from .carddb import CardDB

ZONES = {"Battlefield": "battlefield", "Graveyard": "graveyard", "Exile": "exile",
         "LocalExileCanBePlayed": "exile", "Hand": "hand", "Library": "library", "Stack": "stack"}
COUNTS = (("life", "Life"), ("hand", "HandCount"), ("library", "LibraryCount"))


def _times(snaps, first_event: dt.datetime) -> list[dt.datetime]:
    out = []
    for s in snaps:
        t = dt.datetime.combine(first_event.date(), s.time)
        if t < first_event - dt.timedelta(hours=12):      # game crossed midnight
            t += dt.timedelta(days=1)
        out.append(t)
    return out


def _overlay(state: dict, snap, db: CardDB):
    names = {p["Id"]: p["Name"] for p in snap.players}
    by_iid = {o["iid"]: (uid, o) for uid, o in state["objects"].items() if o.get("iid") is not None}
    snap_key = {uid: f"s{o['iid']}" for uid, o in state["objects"].items() if o.get("iid") is not None}
    objects = {}
    # keep what the snapshot cannot tell: the opponent's known hand, known library cards
    for uid, o in state["objects"].items():
        if (o["zone"] == "hand" and o["owner"] not in _visible_hands(snap, names)) or \
                (o["zone"] == "library" and o.get("lib_pos")):
            objects[uid] = o
    for c in snap.cards:
        zone = ZONES.get(c["Zone"])
        if zone is None or zone == "library":      # snapshots don't say where in the library a card is
            continue
        name = db.name_by_catalog(c["CatalogID"]) or f"#{c['CatalogID']}"
        prev = by_iid.get(c["Id"], (None, None))[1]
        info = db.info(name)
        objects[f"s{c['Id']}"] = {
            "name": name, "iid": c["Id"], "owner": names.get(c["Owner"], "?"),
            "controller": names.get(c["Controller"], "?"), "zone": zone,
            "counters": prev["counters"] if prev else {}, "token": info.has("Token"),
            "attacking": prev["attacking"] if prev else False,
            "tapped": prev.get("tapped", False) if prev else False,
            "tap_guess": prev.get("tap_guess", False) if prev else False,
            "pt": prev.get("pt") if prev else None, "pt_base": prev.get("pt_base") if prev else None,
            "pt_mods": prev.get("pt_mods", []) if prev else [],
            "attached_to": snap_key.get(prev.get("attached_to")) if prev else None,
            "face_down": False, "note": "", "uncertain": False, "placeholder": None, "damage": 0,
        }
    state["objects"] = objects


def _visible_hands(snap, names) -> set[str]:
    return {names.get(c["Owner"]) for c in snap.cards if c["Zone"] == "Hand"}


_LEAVE_HAND = ("cast", "play_land", "discard", "cycle", "ninjutsu", "suspend", "exile_cost")


def _carry_forward(state: dict, last: dict):
    """Improve a non-exact step with what the last exact snapshot revealed."""
    objs = state["objects"]
    # 1. unknown (fetched) cards the snapshot identified
    for uid, name in last["named"].items():
        o = objs.get(uid)
        if o is not None and o["name"] is None:
            objs[uid] = {**o, "name": name, "placeholder": None, "note": ""}
    # 2. warnings the snapshot confirmed (step 3, the hand, is done separately: see _hands_between)
    seen = last["public"]
    for uid, o in list(objs.items()):
        if o.get("uncertain") and (o["name"], o["zone"], o["owner"]) in seen:
            objs[uid] = {**o, "uncertain": False, "note": ""}


def _set_hand(state: dict, me: str, names: list[str]):
    objs = state["objects"]
    for uid in [u for u, o in objs.items() if o["zone"] == "hand" and o["owner"] == me]:
        del objs[uid]
    for k, name in enumerate(names):
        objs[f"h{k}"] = {"name": name, "iid": None, "owner": me, "controller": me, "zone": "hand",
                         "counters": {}, "token": False, "attacking": False, "attached_to": None,
                         "face_down": False, "note": "", "uncertain": False, "placeholder": None, "damage": 0}


def _hands_between(log: list[tuple], a: int, b: int | None, start: list[str], end: list[str] | None,
                   last_step: int) -> dict[int, list[str]]:
    """Exact hand for every step strictly between two exact snapshots a and b.

    Cards in the hand at `a` stay until the log shows them leaving.  Cards that show up
    later (in the hand at `b`, or leaving before it) were drawn in between: each is put
    on the latest draw it can have come from, so it is shown from when it was certainly there.
    """
    stop = b if b is not None else last_step + 1
    events = [e for e in log if a < e[0] <= (b if b is not None else last_step)]
    present = Counter(start)
    timeline: list[tuple[int, str, str]] = []     # (step, "+"/"-", name) for known cards
    slots: list[int] = []
    drawn: list[tuple[str, int]] = []             # (name, deadline): unknown draws identified later
    for step, kind, val in sorted(events, key=lambda e: e[0]):
        if kind in ("draw", "open"):
            slots += [step] * val
        elif kind == "in":
            present[val] += 1
            timeline.append((step, "+", val))
        elif kind == "out":
            if present[val] > 0:
                present[val] -= 1
                timeline.append((step, "-", val))
            else:
                drawn.append((val, step))
    if end is not None:
        for name, n in (Counter(end) - present).items():
            drawn += [(name, b + 1)] * n

    def feasible(pool, cards):
        pool = sorted(pool)
        for _, dl in sorted(cards, key=lambda c: c[1]):
            s = next((x for x in pool if x < dl), None)
            if s is None:
                return False
            pool.remove(s)
        return True

    placed = []
    for card in drawn:
        others = [c for c in drawn if c is not card]
        for s in sorted({x for x in slots if x < card[1]}, reverse=True):
            rest = list(slots)
            rest.remove(s)
            if feasible(rest, others):
                placed.append((card[0], s, card[1]))
                break

    out = {}
    cur = Counter(start)
    tl = sorted(timeline)
    k = 0
    for t in range(a + 1, stop):
        while k < len(tl) and tl[k][0] <= t:
            _, sign, name = tl[k]
            cur[name] += 1 if sign == "+" else -1
            k += 1
        names = list(cur.elements())
        names += [n for n, s, dl in placed if s <= t < dl]
        out[t] = names
    return out


def apply_snapshots(steps, snaps, db: CardDB, me: str | None = None,
                    hand_log: list[tuple] | None = None) -> int:
    if not steps or not snaps:
        return 0
    stimes = _times(snaps, steps[0].event.time)
    etimes = [st.event.time for st in steps]
    exact: dict[int, object] = {}
    for j, s0 in enumerate(stimes):
        i = bisect.bisect_left(etimes, s0) - 1
        if i < 0:
            continue
        if i + 1 < len(etimes) and etimes[i + 1] < s0 + dt.timedelta(seconds=1):
            continue           # another line happened inside that second: ambiguous
        exact[i] = snaps[j]

    offsets: dict[str, dict[str, int]] = {}
    approx_at: dict[str, int] = {}
    last = None
    exact_hands: dict[int, list[str]] = {}
    for i, st in enumerate(steps):
        players = st.state["players"]
        snap = exact.get(i)
        if snap is not None:
            before = st.state["objects"]
            for p in snap.players:
                mine = players.get(p["Name"])
                if mine is None:
                    continue
                offsets[p["Name"]] = {k: p[f] - mine[k] for k, f in COUNTS}
                approx_at[p["Name"]] = mine.get("approx_count", 0)
                for k, f in COUNTS:
                    mine[k] = p[f]
                mine.update(life_approx=False, library_approx=False, exact=True)
            _overlay(st.state, snap, db)
            after = st.state["objects"]
            known_iids = {o.get("iid") for o in before.values()}
            revealed = [o for o in after.values() if o["zone"] == "battlefield" and o["iid"] not in known_iids]
            named = dict(last["named"]) if last else {}
            for uid, o in before.items():
                if o["name"] is None and o["zone"] == "battlefield":
                    match = next((r for r in revealed if r["controller"] == o["controller"]), None)
                    if match is not None:
                        revealed.remove(match)
                        named[uid] = match["name"]
            last = {"named": named,
                    "public": {(o["name"], o["zone"], o["owner"]) for o in after.values()
                               if o["zone"] in ("battlefield", "graveyard", "exile")}}
            exact_hands[i] = [o["name"] for o in after.values() if o["zone"] == "hand" and o["owner"] == me]
            continue
        if last is not None:
            _carry_forward(st.state, last)
        if st.event.kind in ("begin_hand", "mull_bottom", "mulligan") and st.event.actor in offsets:
            # a (re)dealt hand sets absolute counts: an earlier correction no longer applies
            offsets[st.event.actor] = {**offsets[st.event.actor], "hand": 0, "library": 0}
        for name, off in offsets.items():
            mine = players.get(name)
            if mine is None:
                continue
            for k, _ in COUNTS:
                mine[k] += off[k]
            mine["library_approx"] = False
            mine["life_approx"] = mine.get("approx_count", 0) > approx_at.get(name, 0)

    # my hand between (and after) exact snapshots, from the engine's record of hand movements
    if me is not None and hand_log is not None and exact_hands:
        marks = sorted(exact_hands)
        for a, b in zip(marks, marks[1:] + [None]):
            filled = _hands_between(hand_log, a, b, exact_hands[a], exact_hands[b] if b is not None else None,
                                    len(steps) - 1)
            for t, names in filled.items():
                _set_hand(steps[t].state, me, names)
    return len(exact)
