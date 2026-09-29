"""Working out which saved deck was played."""
import datetime as dt
import unittest
from collections import Counter

from mtgo_replay.decks import Deck, guess_deck, identify_exact

BASE = Counter({"Psychic Frog": 4, "Island": 20, "Fatal Push": 4, "Thoughtseize": 4, "Grizzly Bears": 28})
SIDE = Counter({"Teferi, Time Raveler": 3, "Swamp": 12})


def deck(name, main=BASE, side=SIDE, saved=None):
    return Deck(name, "CMODERN", Counter(main), Counter(side), saved)


V1 = deck("Deck 1.0", saved=dt.datetime(2026, 9, 1))
V2 = deck("Deck 2.0", main=BASE - Counter({"Grizzly Bears": 1}) + Counter({"Swamp": 1}), saved=dt.datetime(2026, 9, 20))
V3 = deck("Deck 3.0", main=BASE - Counter({"Grizzly Bears": 2}) + Counter({"Swamp": 2}), saved=dt.datetime(2026, 9, 30))
SAVED = [V1, V2, V3]


class IdentifyExactTest(unittest.TestCase):
    def test_identical_list(self):
        m = identify_exact(deck("used", main=V2.main, side=V2.side), SAVED)
        self.assertEqual((m.deck.name, m.how), ("Deck 2.0", "exact"))

    def test_sideboarded_list_is_still_the_same_deck(self):
        # game 2: two Teferi came in for two Bears; the 75 cards are unchanged
        main = V1.main - Counter({"Grizzly Bears": 2}) + Counter({"Teferi, Time Raveler": 2})
        side = V1.side - Counter({"Teferi, Time Raveler": 2}) + Counter({"Grizzly Bears": 2})
        m = identify_exact(deck("used", main=main, side=side), SAVED)
        self.assertEqual((m.deck.name, m.how), ("Deck 1.0", "exact"))

    def test_unknown_list(self):
        m = identify_exact(deck("used", main=Counter({"Island": 60}), side=Counter()), SAVED)
        self.assertEqual(m.how, "none")
        self.assertIsNone(m.deck)


class GuessTest(unittest.TestCase):
    def test_versions_that_fit_equally_go_to_the_last_saved_before_the_match(self):
        seen = Counter({"Psychic Frog": 2, "Island": 5})           # in every version
        m = guess_deck(seen, SAVED, before=dt.datetime(2026, 9, 25))
        self.assertEqual(m.deck.name, "Deck 2.0")                  # 3.0 was saved after the match
        self.assertIn("3 saved versions fit equally", m.detail)

    def test_a_card_only_one_version_has(self):
        seen = Counter({"Grizzly Bears": 28})                      # only 1.0 has 28 Bears
        self.assertEqual(guess_deck(seen, SAVED, before=dt.datetime(2026, 9, 25)).deck.name, "Deck 1.0")


if __name__ == "__main__":
    unittest.main()
