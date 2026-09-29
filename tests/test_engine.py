"""The state-reconstruction engine on small synthetic games."""
import unittest

from mtgo_replay.engine import GameEngine

from helpers import card_db, events, ref


def run(lines, me="Alice"):
    steps = GameEngine(events(lines), ["Alice", "Bob"], card_db(), me=me).run()
    return steps


def zone_of(step, name, owner=None):
    return [o["zone"] for o in step.state["objects"].values()
            if o["name"] == name and (owner is None or o["owner"] == owner)]


START = ["Alice chooses to play first.",
         "Alice begins the game with seven cards in hand.",
         "Bob begins the game with seven cards in hand.",
         "Turn 1: Alice"]


class ZonesAndStackTest(unittest.TestCase):
    def test_permanent_spell_resolves_and_removal_is_inferred(self):
        steps = run(START + [
            "Alice plays " + ref("Island", 100) + ".",
            "Alice casts " + ref("Psychic Frog", 101) + ".",
            "Turn 1: Bob",                                                  # the Frog resolved
            "Bob casts " + ref("Fatal Push", 102) + " targeting " + ref("Psychic Frog", 101) + ".",
            "Turn 2: Alice",                                                # Push resolved
        ])
        self.assertEqual(zone_of(steps[5], "Psychic Frog"), ["stack"])
        self.assertEqual(zone_of(steps[6], "Psychic Frog"), ["battlefield"])
        self.assertEqual(zone_of(steps[-1], "Psychic Frog"), ["graveyard"])
        self.assertEqual(zone_of(steps[-1], "Fatal Push"), ["graveyard"])
        self.assertTrue(any("Psychic Frog → graveyard" in n for n in steps[-1].notes))

    def test_hand_and_library_counts(self):
        steps = run(START + ["Alice plays " + ref("Island", 100) + ".", "Turn 1: Bob", "Bob draws a card."])
        alice, bob = steps[-1].state["players"]["Alice"], steps[-1].state["players"]["Bob"]
        self.assertEqual((alice["hand"], alice["library"]), (6, 53))
        self.assertEqual((bob["hand"], bob["library"]), (8, 52))

    def test_fetched_land_is_named_when_it_shows_up(self):
        steps = run(START + [
            "Alice plays " + ref("Polluted Delta", 100) + ".",
            "Alice activates an ability of " + ref("Polluted Delta", 100)
            + " (Search your library for an Island or Swamp card, put it onto the battlefield, then shuffle.)",
            "Alice puts a triggered ability from " + ref("Meticulous Archive", 105)
            + " onto the stack (When this land enters, surveil 1.).",
        ])
        last = steps[-1]
        self.assertEqual(last.state["players"]["Alice"]["life"], 19)          # fetch cost
        self.assertEqual(zone_of(last, "Meticulous Archive"), ["battlefield"])
        self.assertEqual(zone_of(last, "Polluted Delta"), ["graveyard"])
        archive = next(o for o in last.state["objects"].values() if o["name"] == "Meticulous Archive")
        self.assertTrue(archive["tapped"])                                   # "This land enters tapped."


class PlaneswalkerTest(unittest.TestCase):
    def test_walker_dies_when_its_cost_takes_it_to_zero(self):
        steps = run(START + [
            "Alice casts " + ref("Teferi, Time Raveler", 100) + ".",
            "Turn 1: Bob", "Turn 2: Alice",
            "Alice removes three loyalty counters from " + ref("Teferi, Time Raveler", 100) + ".",
            "Alice removes a loyalty counter from " + ref("Teferi, Time Raveler", 100) + ".",
            "Alice activates an ability of " + ref("Teferi, Time Raveler", 100)
            + " (Return up to one target artifact, creature, or enchantment to its owner's hand. Draw a card.)",
        ])
        self.assertEqual(zone_of(steps[-1], "Teferi, Time Raveler"), ["graveyard"])


class EquipmentAndPTTest(unittest.TestCase):
    def test_optional_attach_and_prowess(self):
        steps = run(START + [
            "Alice casts " + ref("Cori-Steel Cutter", 100) + ".",
            "Alice plays " + ref("Island", 99) + ".",                        # the Cutter resolved
            "Alice puts a triggered ability from " + ref("Cori-Steel Cutter", 100)
            + " onto the stack (Whenever you cast your second spell each turn, create a 1/1 white Monk creature...).",
            "Alice's " + ref("Cori-Steel Cutter", 100) + " creates a Monk Token.",
            "Alice chooses to use " + ref("Cori-Steel Cutter", 100) + "'s ability.",
            "Alice puts a triggered ability from " + ref("Monk Token", 110) + " onto the stack (Prowess).",
            "Alice plays " + ref("Island", 111) + ".",                       # prowess resolved
        ])
        monk = next(o for o in steps[-1].state["objects"].values() if o["name"] == "Monk Token")
        self.assertEqual(monk["pt"], "3/3")                                   # 1/1 +1/+1 Cutter +1/+1 prowess
        self.assertEqual(monk["pt_base"], "1/1")
        cutter = next(o for o in steps[-1].state["objects"].values() if o["name"] == "Cori-Steel Cutter")
        self.assertIsNotNone(cutter["attached_to"])
        after_turn = run(START + [
            "Alice casts " + ref("Cori-Steel Cutter", 100) + ".",
            "Alice's " + ref("Cori-Steel Cutter", 100) + " creates a Monk Token.",
            "Alice puts a triggered ability from " + ref("Monk Token", 110) + " onto the stack (Prowess).",
            "Turn 1: Bob",
        ])[-1]
        monk = next(o for o in after_turn.state["objects"].values() if o["name"] == "Monk Token")
        self.assertEqual(monk["pt"], "1/1")                                   # "until end of turn" is over


class HandInferenceTest(unittest.TestCase):
    def test_cards_played_later_were_in_hand(self):
        steps = run(START + [
            "Alice plays " + ref("Island", 100) + ".",
            "Turn 1: Bob", "Turn 2: Alice", "Alice draws a card.",
            "Alice casts " + ref("Thoughtseize", 110) + " targeting Bob.",
        ])
        # Thoughtseize may have been drawn on turn 2, so it is only certain from that draw on
        draw = next(i for i, s in enumerate(steps) if s.event.kind == "draw")
        in_hand = lambda i: [o["name"] for o in steps[i].state["objects"].values()
                             if o["zone"] == "hand" and o["owner"] == "Alice"]
        self.assertIn("Thoughtseize", in_hand(draw))
        self.assertNotIn("Thoughtseize", in_hand(draw - 1))


if __name__ == "__main__":
    unittest.main()
