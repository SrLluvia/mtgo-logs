"""Turn one match log into per-game .txt/.json reviews."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import clientlog
from .carddb import CardDB
from . import __version__
from .decks import Deck, deck_from_client_log, guess_deck, identify_exact, load_saved_decks
from .engine import GameEngine
from .events import parse_events
from .gamelog import Match
from .render import write_game
from .snapshots import apply_snapshots

def detect_me(files) -> str | None:
    """The account that appears in (almost) every match is the user."""
    seen = Counter()
    for f in files[:60]:
        try:
            seen.update(set(re.findall(r"@P@P(.+?) joined the game\.", f.read_bytes().decode("utf-8", "replace"))))
        except OSError:
            pass
    return seen.most_common(1)[0][0] if seen else None


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe(name: str) -> str:
    """A player name usable as part of a folder name on Windows."""
    name = re.sub(r"[^\w.-]+", "_", name).strip("._ ")[:60] or "unknown"
    return f"_{name}" if name.upper() in _RESERVED else name


@dataclass
class Context:
    out: Path
    data: Path
    db: CardDB
    decks: list[Deck]
    client: clientlog.ClientData
    me: str | None

    @classmethod
    def load(cls, out: Path, data: Path, me: str | None) -> "Context":
        db = CardDB.load(data)
        return cls(out, data, db, load_saved_decks(db), clientlog.load(data / "clientlogs"), me)


def match_dir(out: Path, m: Match, me: str | None) -> Path:
    """<date>_vs_<opponent>, reusing the folder already written for this match."""
    for marker in out.glob("*/.match"):
        if marker.read_text().strip() == m.match_id:
            return marker.parent
    opp = next((p for p in m.players if p != me), "unknown")
    base = f"{m.start:%Y-%m-%d}_vs_{safe(opp)}"
    name, k = base, 2
    while (out / name).exists():
        name, k = f"{base}_{k}", k + 1
    return out / name


def process_match(ctx: Context, m: Match) -> Path:
    me, db, client = ctx.me, ctx.db, ctx.client
    out_dir = match_dir(ctx.out, m, me)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".match").write_text(m.match_id)

    game_ids = client.games_of_match(m.match_id)
    games = []
    seen = Counter()                     # the player's own cards, as the engine attributed them
    for g in m.games:
        events = parse_events(g.records, g.players)
        game_id = game_ids[g.number - 1] if len(game_ids) >= g.number else None
        exact = deck_from_client_log(client.decks[game_id][1], db) if game_id in client.decks else None
        engine = GameEngine(events, g.players, db, {me: exact.size} if exact else {}, me=me)
        steps = engine.run()
        mine = Counter(o["name"] for o in steps[-1].state["objects"].values()
                       if o["owner"] == me and o["name"] and not o["token"])
        seen |= mine                     # max copies seen in any one game
        games.append((g, events, game_id, exact, engine, steps))

    guessed = guess_deck(seen, ctx.decks, m.start)
    for g, events, game_id, exact, engine, steps in games:
        deck = identify_exact(exact, ctx.decks, m.start) if exact else guessed
        size = exact.size if exact else (deck.deck.size if deck.deck else None)
        decks_txt = {me: f"{deck.label()} ({size} cards) — {deck.detail}" if size else deck.detail} if deck.how != "none" or deck.detail else {}
        source = "reconstructed from the game log (see legend)"
        snaps = client.snapshots.get(game_id) if game_id else None
        if snaps:
            n = apply_snapshots(steps, snaps, db, me, engine.hand_log.get(me, []))
            source = f"game log + {n} exact MTGO snapshots (life, hands, zones)"
        on_play = next((e.actor for e in events if e.kind == "play_first"), None)
        header = {
            "app_version": __version__,
            "match_id": m.match_id, "game_id": game_id, "game": g.number,
            "date": f"{events[0].time:%Y-%m-%d %H:%M}" if events else "",
            "players": g.players if me not in g.players else [me] + [p for p in g.players if p != me],
            "me": me, "on_play": on_play, "winner": g.winner, "decks": decks_txt, "source": source,
            "deck": {"name": deck.deck.name if deck.deck else None, "how": deck.how, "detail": deck.detail},
        }
        write_game(out_dir / f"game{g.number}.txt", out_dir / f"game{g.number}.json", header, steps, db)
    return out_dir
