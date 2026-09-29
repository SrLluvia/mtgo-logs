"""Turn one match log into per-game .txt/.json reviews."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import clientlog
from .carddb import CardDB
from .decks import Deck, deck_from_client_log, guess_deck, load_saved_decks
from .engine import GameEngine
from .events import parse_events
from .gamelog import Match
from .render import write_game
from .snapshots import apply_snapshots

_ME_KINDS = ("cast", "play_land", "activate", "discard", "exile_cost", "reveal_opening", "cycle", "ninjutsu")


def detect_me(files) -> str | None:
    """The account that appears in (almost) every match is the user."""
    seen = Counter()
    for f in files[:60]:
        try:
            seen.update(set(re.findall(r"@P@P(.+?) joined the game\.", f.read_bytes().decode("utf-8", "replace"))))
        except OSError:
            pass
    return seen.most_common(1)[0][0] if seen else None


def safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name)


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
    all_events = [parse_events(g.records, g.players) for g in m.games]
    seen = Counter()
    for evs in all_events:
        for ev in evs:
            if ev.actor == me and ev.kind in _ME_KINDS and ev.cards:
                seen[ev.cards[0].name] += 1
    deck, score = guess_deck(seen, ctx.decks)

    for g, events in zip(m.games, all_events):
        game_id = game_ids[g.number - 1] if len(game_ids) >= g.number else None
        decks_txt, sizes = {}, {}
        if game_id in client.decks:
            d = deck_from_client_log(client.decks[game_id][1], db)
            sizes[me] = d.size
            decks_txt[me] = f"{deck.name if deck and score > 0.9 else 'unnamed'} ({d.size} cards, exact list from MTGO client log)"
        elif deck is not None and score > 0:
            sizes[me] = deck.size
            decks_txt[me] = f"{deck.name} ({deck.size} cards) — guessed: {score:.0%} of the cards you played are in it"
        steps = GameEngine(events, g.players, db, sizes).run()
        source = "reconstructed from the game log (see legend)"
        snaps = client.snapshots.get(game_id) if game_id else None
        if snaps:
            n = apply_snapshots(steps, snaps, db, m.start.date(), me)
            source = f"game log + {n} exact MTGO snapshots (life, hands, zones)"
        on_play = next((e.actor for e in events if e.kind == "play_first"), None)
        header = {
            "match_id": m.match_id, "game_id": game_id, "game": g.number,
            "date": f"{events[0].time:%Y-%m-%d %H:%M}" if events else "",
            "players": g.players if me not in g.players else [me] + [p for p in g.players if p != me],
            "me": me, "on_play": on_play, "winner": g.winner, "decks": decks_txt, "source": source,
        }
        write_game(out_dir / f"game{g.number}.txt", out_dir / f"game{g.number}.json", header, steps, db)
    return out_dir
