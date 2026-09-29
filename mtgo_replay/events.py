"""Turn raw game-log lines into structured events.

Card references look like  @[Psychic Frog@:252834,440:@]  where 252834 is the
card texture number and 440 the MTGO object id.  MTGO gives a card a fresh
object id every time it changes zone, and ids are handed out in increasing
order, which the engine exploits to date moves the log never mentions.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .gamelog import Record

CARD_RE = re.compile(r"@\[(.+?)@:(\d+),(\d+):@\]")
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "their next": 1}


def number(word: str) -> int:
    word = word.strip().lower()
    return int(word) if word.isdigit() else _NUM.get(word, 1)


@dataclass
class CardRef:
    name: str
    texture: int
    iid: int


@dataclass
class Event:
    index: int
    time: dt.datetime
    kind: str
    raw: str
    text: str                       # readable line (card refs replaced by names)
    actor: str | None = None
    cards: list[CardRef] = field(default_factory=list)
    players: list[str] = field(default_factory=list)   # player names targeted / mentioned
    n: int = 0
    info: dict = field(default_factory=dict)

    @property
    def card(self) -> CardRef | None:
        return self.cards[0] if self.cards else None

    @property
    def max_iid(self) -> int:
        return max((c.iid for c in self.cards), default=0)


def _readable(raw: str) -> str:
    t = CARD_RE.sub(lambda m: m.group(1), raw)
    t = t.replace("@P", "").replace("@R", "")
    return t.replace("@-", "—").strip()


def _join_continuations(records: list[Record]) -> list[Record]:
    """Long lines (e.g. revealing ten cards) are split over several records."""
    out: list[Record] = []
    for r in records:
        for part in r.text.replace("\r", "").split("\n"):
            if out and out[-1].text.rstrip().endswith(",") and part.startswith("@P@["):
                out[-1] = Record(out[-1].time, out[-1].text + " " + part[2:])
            elif part.strip():
                out.append(Record(r.time, part))
    return out


# Templates are matched against the line with card refs replaced by "\x01".
C = "\x01"
P = r"(?P<actor>.+?)"
CL = rf"{C}(?:(?:, | and |, and ){C})*"   # list of cards
PATTERNS: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("turn", r"^Turn (?P<n>\d+): (?P<actor>.+?)(?P<extra> - Extra Turn)?$"),
    ("begin_hand", rf"^{P} begins the game with (?P<n>\w+) cards? in hand\.$"),
    ("mull_bottom", rf"^{P} puts (?P<k>\w+) cards? on the bottom of their library and begins the game with (?P<n>\w+) cards? in hand\.$"),
    ("mulligan", rf"^{P} mulligans to (?P<n>\w+) cards?\.$"),
    ("play_first", rf"^{P} chooses to (?P<w>play|draw) first\.$"),
    ("skip_draw", rf"^{P} skips their draw step\.$"),
    ("draw", rf"^{P} draws (?P<n>a|an|\w+|their next) cards?(?: with (?P<src>{C}|[^.]+?))?\.$"),
    ("draw_prevented", rf"^{P} is prevented from drawing"),
    ("play_land", rf"^{P} plays {C}\.$"),
    ("cast", rf"^{P} casts {C}(?P<rest>.*)$"),
    ("ninjutsu", rf"^{P} activates Ninjutsu ability of {C}\.?$"),
    ("activate", rf"^{P} activates an ability of {C} \((?P<text>.*?)\)(?P<rest>.*)$"),
    ("trigger", rf"^{P} puts a triggered ability from (?P<src>{C}|.+?) onto the stack(?: \((?P<text>.*?)\))?(?P<rest>(?: targeting .*)?)\.?$"),
    ("discard", rf"^{P} discards {C}\.$"),
    ("to_graveyard", rf"^{P} puts {CL} into their graveyard(?: and (?P<n>\w+) cards? on top of their library)?\.$"),
    ("to_top", rf"^{P} puts (?P<n>\w+) cards? on top of their library\.$"),
    ("scry", rf"^{P} scrys (?P<n>\d+)"),
    ("exile_cost", rf"^{P} exiles {CL} with with {C}\.$"),
    ("exile_own", rf"^{P} exiles {C} with its own ability\.$"),
    ("suspend", rf"^{P} exiles {C} with (?P<n>\w+) time counters?\.$"),
    ("exile_with", rf"^{P} exiles {CL} with {C}(?:'s ability)?\.$"),
    ("counter_spell", rf"^{P} counters (?P<what>{C}|Triggered Ability from .+?) with (?P<src>{C}|Triggered Ability from .+?)\.$"),
    ("fizzle", rf"^{P}'s (?P<what>{C}|.+?) is removed from the stack because it has no legal targets\.$"),
    ("return_hand", rf"^{P} returns {CL} to (?:its|their) owner's hand with (?:with )?(?P<src>{C}|.+?)(?:'s ability)?\.$"),
    ("return_none", rf"^{P} returns no cards"),
    ("put_top", rf"^{P} puts {C} on top of their library with (?P<src>{C}|.+?)(?:'s ability)?\.$"),
    ("put_battlefield", rf"^{P} puts {C} onto the battlefield(?P<att> attacking .+)?\.?$"),
    ("reveal_opening", rf"^{P} reveals {C} from their opening hand\.$"),
    ("reveal_many", rf"^{P} reveals (?P<n>\d+) cards with (?P<src>{C}): {CL}\.$"),
    ("reveal", rf"^{P} reveals {C}(?: with {C})?\.$"),
    ("choose_cards", rf"^{P} chooses (?P<names>(?!to )(?![a-z]).+?)(?: with {C}'s ability)?\.$"),
    ("mill", rf"^{P} mills {CL}\.$"),
    ("create", rf"^{P}'s {C} creates (?P<n>a|an|\w+) (?P<what>.+?)(?: attached to {C})?\.(?P<rest>.*)$"),
    ("counter_on", rf"^{P} puts (?P<what>.+?) counters? on (?P<target>{C}(?: and {C})?|.+?)\.$"),
    ("counter_off", rf"^{P} removes (?P<n>a|an|\w+) (?P<what>.+?) counters? from (?P<target>{C}|.+?)\.$"),
    ("attack", rf"^{P} is being attacked by (?P<att>.+)$"),
    ("block", rf"^{C} blocks {C}\.$"),
    ("transform", rf"^{C} transforms into {C}\.$"),
    ("plotted", rf"^{C} becomes plotted\.$"),
    ("life_gain", rf"^{P} gains (?P<n>\d+) life\.$"),
    ("life_loss", rf"^{P} loses (?P<n>\d+) life\.$"),
    ("fail_search", rf"^{P} fails to find"),
    ("wins", rf"^{P} wins the game\.$"),
    ("loses", rf"^{P} loses the game\.$"),
    ("concede", rf"^{P} has conceded from the game\.$"),
    ("left", rf"^{P} has (?:left the game|lost connection)"),
    ("match_result", r"^(?:.+ (?:leads|wins) the match|Match Tied) \d+-\d+$"),
    ("face_up", rf"^{P} turns cloaked creature"),
    ("cycle", rf"^{P} cycles {C}\.$"),
]]


def parse_events(records: list[Record], players: list[str]) -> list[Event]:
    events = []
    for rec in _join_continuations(records):
        raw = rec.text
        refs = [CardRef(m.group(1), int(m.group(2)), int(m.group(3))) for m in CARD_RE.finditer(raw)]
        tmpl = CARD_RE.sub(C, raw).replace("@P", "").replace("@R", "").strip()
        ev = Event(len(events), rec.time, "other", raw, _readable(raw), cards=refs)
        for kind, pat in PATTERNS:
            m = pat.match(tmpl)
            if m:
                ev.kind = kind
                g = m.groupdict()
                ev.actor = g.get("actor")
                if g.get("n"):
                    ev.n = number(g["n"])
                ev.info = {k: v for k, v in g.items() if v is not None and k not in ("actor",)}
                if g.get("src") == C:
                    ev.info["src_idx"] = tmpl[:m.start("src")].count(C)
                break
        # Owner of "X's Card ..." lines, and players named anywhere (targets)
        if ev.actor and ev.actor not in players:
            owner = next((p for p in players if ev.actor.startswith(p)), None)
            if owner and ev.kind in ("attack",):
                ev.info["defender_card"] = True
            ev.actor = owner or ev.actor
        rest = ev.info.get("rest", "") or ""
        if "targeting" in rest:
            tgt = rest.split("targeting", 1)[1]
            ev.players = [p for p in players if re.search(rf"(?:^|\s|,){re.escape(p)}(?:\s|,|\.|$)", tgt)]
            ev.info["n_targets"] = tgt.count(C)
        if ev.kind == "attack":
            ev.players = [p for p in players if tmpl.startswith(p)]
        events.append(ev)
    return events
