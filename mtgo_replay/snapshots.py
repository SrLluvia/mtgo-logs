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
    objects = {}
    # keep what the snapshot cannot tell: the opponent's known hand, known library cards
    for uid, o in state["objects"].items():
        if o["zone"] in ("hand", "library") and o["owner"] not in _visible_hands(snap, names):
            objects[uid] = o
    for c in snap.cards:
        zone = ZONES.get(c["Zone"])
        if zone is None:
            continue
        name = db.name_by_catalog(c["CatalogID"]) or f"#{c['CatalogID']}"
        prev = by_iid.get(c["Id"], (None, None))[1]
        info = db.info(name)
        objects[f"s{c['Id']}"] = {
            "name": name, "iid": c["Id"], "owner": names.get(c["Owner"], "?"),
            "controller": names.get(c["Controller"], "?"), "zone": zone,
            "counters": prev["counters"] if prev else {}, "token": info.has("Token"),
            "attacking": prev["attacking"] if prev else False, "attached_to": None,
            "face_down": False, "note": "", "uncertain": False, "placeholder": None, "damage": 0,
        }
    state["objects"] = objects


def _visible_hands(snap, names) -> set[str]:
    return {names.get(c["Owner"]) for c in snap.cards if c["Zone"] == "Hand"}


_LEAVE_HAND = ("cast", "play_land", "discard", "cycle", "ninjutsu", "suspend", "exile_cost")


def _carry_forward(state: dict, last: dict, hand: list[str], me: str | None):
    """Improve a non-exact step with what the last exact snapshot revealed."""
    objs = state["objects"]
    # 1. unknown (fetched) cards the snapshot identified
    for uid, name in last["named"].items():
        o = objs.get(uid)
        if o is not None and o["name"] is None:
            objs[uid] = {**o, "name": name, "placeholder": None, "note": ""}
    # 2. warnings the snapshot confirmed
    seen = last["public"]
    for uid, o in list(objs.items()):
        if o.get("uncertain") and (o["name"], o["zone"], o["owner"]) in seen:
            objs[uid] = {**o, "uncertain": False, "note": ""}
    # 3. my hand
    if me is not None:
        for uid in [u for u, o in objs.items() if o["zone"] == "hand" and o["owner"] == me]:
            del objs[uid]
        for k, name in enumerate(hand):
            objs[f"h{k}"] = {"name": name, "iid": None, "owner": me, "controller": me, "zone": "hand",
                             "counters": {}, "token": False, "attacking": False, "attached_to": None,
                             "face_down": False, "note": "", "uncertain": False, "placeholder": None, "damage": 0}


def apply_snapshots(steps, snaps, db: CardDB, date=None, me: str | None = None) -> int:
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
    hand: list[str] = []
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
            hand = [o["name"] for o in after.values() if o["zone"] == "hand" and o["owner"] == me]
            continue
        ev = st.event
        if me is not None and ev.actor == me and ev.kind in _LEAVE_HAND:
            for ref in (ev.cards[:-1] if ev.kind == "exile_cost" else ev.cards[:1]):
                if ref.name in hand:
                    hand.remove(ref.name)
        if last is not None:
            _carry_forward(st.state, last, hand, me)
        for name, off in offsets.items():
            mine = players.get(name)
            if mine is None:
                continue
            for k, _ in COUNTS:
                mine[k] += off[k]
            mine["library_approx"] = False
            mine["life_approx"] = mine.get("approx_count", 0) > approx_at.get(name, 0)
    return len(exact)
