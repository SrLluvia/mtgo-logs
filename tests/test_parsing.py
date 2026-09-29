"""Low-level parsing: the binary game log, log lines, mana costs, folder names."""
import tempfile
import unittest
from pathlib import Path

from mtgo_replay.gamelog import read_match, read_records
from mtgo_replay.mana import Cost, choose_sources, parse_braced_cost, parse_mtgo_cost, produced_colors
from mtgo_replay.pipeline import safe

from helpers import events, ref, write_dat

GAME = [
    "@P@PAlice joined the game.", "@P@PBob joined the game.",
    "@PAlice chooses to play first.",
    "@PAlice begins the game with seven cards in hand.", "@PBob begins the game with seven cards in hand.",
    "@PTurn 1: Alice", "@PAlice plays " + ref("Island", 100) + ".",
    "@PBob has conceded from the game.", "@PAlice wins the game.",
    "@P@PAlice joined the game.", "@P@PBob joined the game.",
    "@PBob chooses to play first.",
    "@PBob begins the game with seven cards in hand.", "@PAlice begins the game with seven cards in hand.",
    "@PAlice wins the game.", "@PAlice wins the match 2-0",
]


class GameLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_reads_records_and_splits_games(self):
        m = read_match(write_dat(self.dir / "Match_GameLog_x.dat", "abc-123", GAME))
        self.assertEqual(m.match_id, "abc-123")
        self.assertEqual(len(m.records), len(GAME))
        self.assertEqual([g.number for g in m.games], [1, 2])
        self.assertEqual(m.players, ["Alice", "Bob"])
        self.assertEqual(m.games[0].winner, "Alice")

    def test_truncated_file_keeps_complete_records(self):
        path = write_dat(self.dir / "Match_GameLog_y.dat", "abc", GAME)
        data = path.read_bytes()
        path.write_bytes(data[:-5])                  # MTGO still writing the last line
        _, recs = read_records(path)
        self.assertEqual(len(recs), len(GAME) - 1)


class EventParsingTest(unittest.TestCase):
    def test_kinds_actors_and_card_refs(self):
        evs = events([
            "Turn 3: Alice",
            "Alice casts " + ref("Fatal Push", 50) + " targeting " + ref("Psychic Frog", 40) + ".",
            "Bob draws two cards with " + ref("Psychic Frog", 41) + ".",
            "Alice reveals 2 cards with " + ref("Thoughtseize", 52) + ": " + ref("Island", 10) + " and " + ref("Swamp", 11) + ".",
            "Alice casts " + ref("Thoughtseize", 53) + " targeting Bob.",
        ])
        self.assertEqual([e.kind for e in evs], ["turn", "cast", "draw", "reveal_many", "cast"])
        self.assertEqual(evs[0].n, 3)
        self.assertEqual(evs[1].actor, "Alice")
        self.assertEqual([c.name for c in evs[1].cards], ["Fatal Push", "Psychic Frog"])
        self.assertEqual(evs[1].info["n_targets"], 1)
        self.assertEqual(evs[2].n, 2)
        self.assertEqual(evs[3].info["src_idx"], 0)          # the source is the first card, not the last
        self.assertEqual(evs[4].players, ["Bob"])

    def test_readable_text(self):
        (e,) = events(["Alice plays " + ref("Island", 7) + "."])
        self.assertEqual(e.text, "Alice plays Island.")


class ManaTest(unittest.TestCase):
    def test_mtgo_database_costs(self):
        self.assertEqual(parse_mtgo_cost("5UU"), Cost(5, ["U", "U"]))
        self.assertEqual(parse_mtgo_cost("f"), Cost(15))                      # hex generic
        self.assertEqual(parse_mtgo_cost("X#bp-").x, 1)
        self.assertEqual(parse_mtgo_cost("1#bp-#bp-").pips, [])               # phyrexian: paid with life
        self.assertEqual(parse_mtgo_cost("#gu-").pips, ["GU"])                # hybrid

    def test_braced_costs(self):
        self.assertEqual(parse_braced_cost("{1WU}"), Cost(1, ["W", "U"]))
        self.assertEqual(parse_braced_cost("{#ur-}").pips, ["UR"])            # MTGO notation inside braces
        c = parse_braced_cost("{1}{W}, {T}")
        self.assertTrue(c.tap)
        self.assertEqual(c.total(), 2)

    def test_produced_colors(self):
        self.assertEqual(produced_colors("Island", ""), "U")
        self.assertEqual(produced_colors("Watery Grave", "({T}: Add {U} or {B}.)"), "UB")
        self.assertEqual(produced_colors("Mox", "{T}: Add one mana of any color."), "*")
        self.assertEqual(produced_colors("Delta", "{T}, Pay 1 life, Sacrifice this land: Search ..."), "")
        self.assertEqual(produced_colors(None, "", "plains or swamp found with Marsh Flats"), "WB")

    def test_colored_pips_use_the_least_flexible_source(self):
        island, grave, swamp = object(), object(), object()
        chosen, unpaid = choose_sources(Cost(1, ["U"]), 0, [(grave, "UB", True), (island, "U", True), (swamp, "B", True)])
        self.assertIs(chosen[0], island)             # U from the Island, keep the dual
        self.assertEqual(len(chosen), 2)
        self.assertEqual(unpaid, 0)


class SafeNameTest(unittest.TestCase):
    def test_folder_names(self):
        self.assertEqual(safe("Player_42"), "Player_42")
        self.assertEqual(safe("a/b\\c"), "a_b_c")
        self.assertEqual(safe(".."), "unknown")
        self.assertEqual(safe("CON"), "_CON")
        self.assertLessEqual(len(safe("x" * 500)), 60)


if __name__ == "__main__":
    unittest.main()
