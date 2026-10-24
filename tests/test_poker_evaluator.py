import unittest

from poker.evaluator import DECK, determine_winners, evaluate_five, evaluate_seven, settle_pots


class EvaluatorTests(unittest.TestCase):
    def test_deck(self):
        self.assertEqual(len(DECK), 52)
        self.assertEqual(len(set(DECK)), 52)

    def test_wheel_straight(self):
        self.assertEqual(evaluate_five(["Ac", "2d", "3h", "4s", "5c"]), (4, 5))

    def test_royal_flush_beats_quads(self):
        royal = evaluate_five(["Ah", "Kh", "Qh", "Jh", "Th"])
        quads = evaluate_five(["Ac", "Ad", "As", "Ah", "2c"])
        self.assertGreater(royal, quads)

    def test_best_seven(self):
        self.assertEqual(evaluate_seven(["Ah", "Kh", "Qh", "Jh", "Th", "2c", "3d"]), (8, 14))

    def test_board_tie(self):
        board = ["Ah", "Kh", "Qh", "Jh", "Th"]
        self.assertEqual(determine_winners({1: ["2c", "3d"], 2: ["4c", "5d"]}, board), [1, 2])

    def test_side_pots_with_folded_contribution(self):
        board = ["2c", "4d", "6h", "8s", "Tc"]
        hands = {1: ["As", "Ad"], 2: ["Ks", "Kd"], 3: ["Qs", "Qd"]}
        payouts = settle_pots({1: 100, 2: 200, 3: 200, 4: 200}, hands, board, [1, 2, 3, 4])
        self.assertEqual(payouts, {1: 400, 2: 300, 3: 0, 4: 0})

    def test_invalid_duplicate_cards(self):
        with self.assertRaises(ValueError):
            evaluate_seven(["Ah"] * 7)

    def test_chip_conservation(self):
        board = ["2c", "4d", "6h", "8s", "Tc"]
        hands = {1: ["As", "Ad"], 2: ["Ks", "Kd"]}
        amounts = {1: 25, 2: 100, 3: 100}
        payouts = settle_pots(amounts, hands, board, [1, 2, 3])
        self.assertEqual(sum(payouts.values()), sum(amounts.values()))


if __name__ == "__main__":
    unittest.main()
