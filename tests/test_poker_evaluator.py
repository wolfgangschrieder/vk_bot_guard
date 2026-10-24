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

    def test_nine_player_side_pots_and_folded_dead_money(self):
        board = ["2c", "4d", "6h", "8s", "Tc"]
        hands = {
            1: ["As", "Ad"], 2: ["Ks", "Kd"], 3: ["Qs", "Qd"],
            4: ["Js", "Jd"], 5: ["9s", "9d"], 6: ["7s", "7d"],
            7: ["5s", "5d"], 8: ["3s", "3d"],
        }
        contributions = {
            1: 100, 2: 200, 3: 300, 4: 400, 5: 500,
            6: 600, 7: 700, 8: 800, 9: 800,
        }
        payouts = settle_pots(contributions, hands, board, list(range(1, 10)))
        self.assertEqual(sum(payouts.values()), 4400)
        self.assertEqual(payouts[1], 900)
        self.assertEqual(payouts[2], 800)
        self.assertEqual(payouts[3], 700)
        self.assertEqual(payouts[4], 600)
        self.assertEqual(payouts[5], 500)
        self.assertEqual(payouts[6], 400)
        self.assertEqual(payouts[7], 300)
        self.assertEqual(payouts[8], 200)
        self.assertEqual(payouts[9], 0)

    def test_odd_chip_goes_to_first_winner_clockwise(self):
        board = ["Ah", "Kh", "Qh", "Jh", "Th"]
        hands = {1: ["2c", "3d"], 2: ["4c", "5d"], 3: ["6c", "7d"]}
        payouts = settle_pots(
            {1: 101, 2: 101, 3: 101}, hands, board, [3, 1, 2]
        )
        self.assertEqual(payouts, {1: 101, 2: 101, 3: 101})
        payouts = settle_pots(
            {1: 101, 2: 101, 3: 101, 4: 100},
            hands, board, [3, 1, 2, 4]
        )
        self.assertEqual(payouts, {1: 134, 2: 134, 3: 135, 4: 0})


if __name__ == "__main__":
    unittest.main()
