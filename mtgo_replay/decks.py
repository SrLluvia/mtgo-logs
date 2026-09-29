"""Saved MTGO decks and guessing which one was used in a match."""
from __future__ import annotations

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

    @property
    def size(self) -> int:
        return sum(self.main.values())


def load_saved_decks(db: CardDB) -> list[Deck]:
    decks = []
    for f in paths.deck_files():
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        if root.get("GroupingType") != "Deck":
            continue
        decks.append(_deck_from_items(
            root.get("Name", f.stem), root.get("FormatCode", ""), db,
            ((int(i.get("CatId")), int(i.get("Quantity", 1)), i.get("IsSideboard") == "true") for i in root.iter("Item")),
        ))
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


def guess_deck(seen: Counter, decks: list[Deck]) -> tuple[Deck | None, float]:
    """Pick the saved deck that explains the most cards seen from the player.

    Score = fraction of seen card copies present in main+side.  Ties go to the
    deck with fewer unexplained main-deck cards (a tighter fit).
    """
    total = sum(seen.values())
    if not total or not decks:
        return None, 0.0
    best, best_key = None, None
    for d in decks:
        pool = d.main + d.side
        covered = sum(min(n, pool.get(c, 0)) for c, n in seen.items())
        key = (covered / total, -len(set(d.main) - set(seen)))
        if best_key is None or key > best_key:
            best, best_key = d, key
    return best, best_key[0]
