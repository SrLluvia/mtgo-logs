"""Card database built from MTGO's own offline CardDataSource XML files.

MTGO identifies cards with two numbers:
  * DigitalObjectCatalogID ("CatId") - used by decks and by mtgo.log snapshots.
  * CARDTEXTURE_NUMBER               - the first number in game log card refs.
Both are mapped to a card name here; per-name info holds what the engine needs
(types, P/T, loyalty, oracle text, a few keywords).

Parsing ~650 XML files takes a few seconds, so the result is cached as JSON and
rebuilt only when the MTGO card data changes.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import paths

CACHE_VERSION = 4

_TYPE_FLAGS = {
    "IS_CREATURE": "Creature", "IS_LAND": "Land", "INSTANT": "Instant", "SORCERY": "Sorcery",
    "IS_ARTIFACT": "Artifact", "IS_ENCHANTMENT": "Enchantment", "IS_PLANESWALKER": "Planeswalker",
    "IS_BATTLE": "Battle", "IS_TOKEN": "Token", "IS_SAGA": "Saga", "IS_LEGENDARY": "Legendary",
    "IS_BASICLAND": "Basic",
}
_KEYWORD_FLAGS = {
    "FLYING", "TRAMPLE", "VIGILANCE", "HASTE", "FIRST_STRIKE", "DOUBLE_STRIKE", "DEATHTOUCH",
    "REACH", "MENACE", "DEFENDER", "INDESTRUCTIBLE", "HEXPROOF", "INFECT",
}


@dataclass
class CardInfo:
    name: str
    types: list[str]
    power: str | None = None
    toughness: str | None = None
    loyalty: str | None = None
    mv: int = 0
    text: str = ""
    keywords: tuple[str, ...] = ()

    def has(self, t: str) -> bool:
        return t in self.types

    @property
    def is_permanent(self) -> bool:
        return not (self.has("Instant") or self.has("Sorcery"))

    def int_power(self) -> int | None:
        return _to_int(self.power)

    def int_toughness(self) -> int | None:
        return _to_int(self.toughness)


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _load_strings(folder: Path, name: str) -> dict[str, str]:
    table = {}
    for _, el in ET.iterparse(folder / f"{name}.xml"):
        if el.tag.endswith("_ITEM"):
            table[el.get("id")] = el.text or ""
            el.clear()
    return table


def _clean_text(t: str) -> str:
    t = t.replace("\\n", "\n")
    return re.sub(r"@i|@b", "", t.replace("@-", "—")).strip()


def build(folder: Path) -> dict:
    names = _load_strings(folder, "CARDNAME_STRING")
    oracle = _load_strings(folder, "REAL_ORACLETEXT_STRING")
    loyalty = _load_strings(folder, "LOYALTY_STRING")
    mana_value = _load_strings(folder, "CONVERTED_MANA_COST")

    raw: dict[int, dict] = {}
    for f in sorted(folder.glob("client_*.xml")):
        for _, el in ET.iterparse(f):
            if el.tag != "DigitalObject":
                continue
            cat = int(el.get("DigitalObjectCatalogID").removeprefix("DOC_"))
            attrs = {}
            for child in el:
                attrs[child.tag] = child.get("value") if child.get("value") is not None else child.get("id", "1")
            raw[cat] = attrs
            el.clear()

    cat_to_name: dict[int, str] = {}
    tex_to_name: dict[int, str] = {}
    cards: dict[str, dict] = {}

    def resolve(cat, depth=0):
        a = raw.get(cat)
        if a is None or depth > 5:
            return None
        if "CARDNAME_STRING" not in a and "CLONE_ID" in a:
            base = resolve(int(a["CLONE_ID"]), depth + 1)
            return None if base is None else {**base, **a}
        return a

    for cat, a0 in raw.items():
        a = resolve(cat)
        if not a or "CARDNAME_STRING" not in a:
            continue
        name = names.get(a["CARDNAME_STRING"], "").strip()
        if not name:
            continue
        cat_to_name[cat] = name
        if "CARDTEXTURE_NUMBER" in a0:
            tex_to_name[int(a0["CARDTEXTURE_NUMBER"])] = name
        if name in cards and a.get("DIGITAL_OBJECT_TYPE_CODE_STRING") not in ("CARD", "TOKN"):
            continue
        if name not in cards or not cards[name]["text"]:
            cards[name] = {
                "types": [t for flag, t in _TYPE_FLAGS.items() if a.get(flag, "0") not in ("0", None)],
                "power": a.get("POWER"),
                "toughness": a.get("TOUGHNESS"),
                "loyalty": loyalty.get(a.get("LOYALTY_STRING", ""), "") or None,
                "mv": _to_int(mana_value.get(a.get("CONVERTED_MANA_COST", ""))) or 0,
                "text": _clean_text(oracle.get(a.get("REAL_ORACLETEXT_STRING", ""), "")),
                "keywords": sorted(k for k in _KEYWORD_FLAGS if a.get(k, "0") not in ("0", None)),
            }
    return {"version": CACHE_VERSION, "cat": cat_to_name, "tex": tex_to_name, "cards": cards}


class CardDB:
    def __init__(self, data: dict):
        self._cat = {int(k): v for k, v in data["cat"].items()}
        self._tex = {int(k): v for k, v in data["tex"].items()}
        self._cards = data["cards"]

    @classmethod
    def load(cls, cache_dir: Path, folder: Path | None = None) -> "CardDB":
        folder = folder or paths.card_data_dir()
        cache = cache_dir / "cards.json"
        stamp = None
        if folder is not None:
            stamp = int((folder / "CARDNAME_STRING.xml").stat().st_mtime)
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION and (stamp is None or data.get("stamp") == stamp):
                return cls(data)
        if folder is None:
            raise FileNotFoundError("MTGO CardDataSource folder not found")
        print("Building card database from MTGO data (one-time)...")
        data = build(folder)
        data["stamp"] = stamp
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return cls(data)

    def known(self, name: str) -> bool:
        return name in self._cards

    def name_by_catalog(self, cat: int) -> str | None:
        return self._cat.get(cat)

    def name_by_texture(self, tex: int) -> str | None:
        return self._tex.get(tex)

    def info(self, name: str) -> CardInfo:
        d = self._cards.get(name)
        if d is None and name.endswith(" Token"):
            d = self._cards.get(name[: -len(" Token")])
        if d is None:
            types = ["Token", "Creature"] if name.endswith("Token") else []
            return CardInfo(name, types)
        return CardInfo(name, d["types"], d["power"], d["toughness"], d["loyalty"], d["mv"], d["text"], tuple(d["keywords"]))
