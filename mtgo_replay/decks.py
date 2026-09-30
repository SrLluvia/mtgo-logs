"""Saved MTGO decks and working out which one was used in a match.

MTGO never logs a deck's *name*.  Two sources can identify it:
  * the exact 75-card list MTGO writes to mtgo.log for each game (when the
    snapshots were saved): matched against the saved decks card by card;
  * otherwise, the cards the player was seen owning during the match: several
    saved versions often contain all of them, so ties go to the version saved
    most recently before the match.
The viewer lets the user override the result ("my deck").
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

from . import paths
from .carddb import CardDB


@dataclass
class Deck:
    name: str
    format: str
    main: Counter
    side: Counter
    saved: dt.datetime | None = None          # when it was last saved in MTGO (local time)

    @property
    def size(self) -> int:
        return sum(self.main.values())

    @property
    def cards(self) -> Counter:
        return self.main + self.side


@dataclass
class DeckMatch:
    deck: Deck | None
    how: str            # "exact" | "closest" | "guessed" | "none"
    detail: str

    def label(self) -> str:
        return self.deck.name if self.deck is not None else "unnamed"


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _saved_roots():
    for f in paths.deck_files():
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        if root.get("GroupingType") == "Deck":
            yield f, root


def saved_deck_names() -> list[str]:
    """Names of the decks saved in MTGO (no card database needed)."""
    return sorted({root.get("Name", f.stem) for f, root in _saved_roots()}, key=str.lower)


def load_saved_decks(db: CardDB) -> list[Deck]:
    decks = []
    for f, root in _saved_roots():
        deck = _deck_from_items(
            root.get("Name", f.stem), root.get("FormatCode", ""), db,
            ((int(i.get("CatId")), int(i.get("Quantity", 1)), i.get("IsSideboard") == "true") for i in root.iter("Item")))
        deck.saved = _parse_timestamp(root.get("Timestamp"))
        decks.append(deck)
    return decks


def deck_from_client_log(items: list[dict], db: CardDB) -> Deck:
    return _deck_from_items("(from MTGO client log)", "", db,
                            ((i["CatalogId"], i.get("Quantity", 1), i.get("InSideboard", False)) for i in items))


def _deck_from_items(name, fmt, db, items) -> Deck:
    main, side = Counter(), Counter()
    for cat, qty, sb in items:
        card = db.name_by_catalog(cat) or f"#{cat}"
        (side if sb else main)[card] += qty
    return Deck(name, fmt, main, side)


def _most_recent(decks: list[Deck], before: dt.datetime | None) -> Deck:
    """Among equally good candidates: the one saved last before the match (else the newest)."""
    def key(d: Deck):
        saved = d.saved or dt.datetime.min
        return (before is None or saved <= before, saved)
    return max(decks, key=key)


def identify_exact(used: Deck, decks: list[Deck], before: dt.datetime | None = None) -> DeckMatch:
    """Which saved deck is this exact list? Compares all 75 cards (sideboarding only swaps cards)."""
    if not decks:
        return DeckMatch(None, "none", "exact list from MTGO; no saved decks found")
    total = sum(used.cards.values())
    overlap = {id(d): sum((used.cards & d.cards).values()) for d in decks}
    best = max(overlap.values())
    candidates = [d for d in decks if overlap[id(d)] == best]
    deck = _most_recent(candidates, before)
    differ = total - best
    if differ == 0:
        return DeckMatch(deck, "exact", f"exact list from MTGO, identical to your saved deck")
    if differ <= 5:
        return DeckMatch(deck, "closest", f"exact list from MTGO; closest saved deck ({differ} card{'s' if differ > 1 else ''} differ)")
    return DeckMatch(None, "none", f"exact list from MTGO; no saved deck matches it (closest: {deck.name}, {differ} cards differ)")


def guess_deck(seen: Counter, decks: list[Deck], before: dt.datetime | None = None) -> DeckMatch:
    """The saved deck that explains the most cards the player was seen owning."""
    total = sum(seen.values())
    if not total or not decks:
        return DeckMatch(None, "none", "")
    covered = {id(d): sum(min(n, d.cards.get(c, 0)) for c, n in seen.items()) for d in decks}
    best = max(covered.values())
    candidates = [d for d in decks if covered[id(d)] == best]
    deck = _most_recent(candidates, before)
    detail = f"guessed: {best}/{total} of your cards seen are in it"
    if len(candidates) > 1:
        detail += f"; {len(candidates)} saved versions fit equally, picked the one saved last before the match"
    return DeckMatch(deck, "guessed", detail)
