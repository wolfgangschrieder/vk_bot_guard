import unittest

from poker.engine import Table


class TableTests(unittest.TestCase):
    def make_table(self, count=2):
        table = Table()
        for uid in range(1, count + 1):
            table.join(uid)
            table.ready(uid)
        table.start()
        return table

    def test_ready_gate(self):
        table = Table()
        table.join(1)
        table.join(2)
        table.ready(1)
        with self.assertRaises(ValueError):
            table.start()

    def test_max_nine(self):
        table = Table()
        for uid in range(1, 10):
            table.join(uid)
        with self.assertRaises(ValueError):
            table.join(10)

    def test_heads_up_blinds_and_first_action(self):
        table = self.make_table()
        self.assertEqual(table.stage, "preflop")
        self.assertEqual(table.turn, 1)
        self.assertEqual(table.players[1].street_bet, 50)
        self.assertEqual(table.players[2].street_bet, 100)

    def test_fold_awards_pot(self):
        table = self.make_table()
        table.act(1, "fold")
        self.assertEqual(table.stage, "complete")
        self.assertEqual(table.players[2].stack, 10050)
        self.assertEqual(sum(p.stack for p in table.players.values()), 20000)

    def test_check_facing_bet_is_invalid(self):
        table = self.make_table()
        with self.assertRaises(ValueError):
            table.act(1, "check")
        self.assertEqual(table.turn, 1)

    def test_all_streets_to_showdown(self):
        table = self.make_table()
        table.act(1, "call")
        table.act(2, "check")
        self.assertEqual(table.stage, "flop")
        for expected in ("turn", "river", "complete"):
            first = table.turn
            table.act(first, "check")
            table.act(table.turn, "check")
            self.assertEqual(table.stage, expected)
        self.assertEqual(sum(p.stack for p in table.players.values()), 20000)
        self.assertEqual(len(table.board), 5)

    def test_no_actions_after_complete(self):
        table = self.make_table()
        table.act(1, "fold")
        with self.assertRaises(ValueError):
            table.act(2, "check")

    def test_allin_heads_up_finishes_without_extra_actions(self):
        table = self.make_table()
        table.act(1, "allin")
        table.act(2, "call")
        self.assertEqual(table.stage, "complete")
        self.assertEqual(len(table.board), 5)
        self.assertEqual(sum(p.stack for p in table.players.values()), 20000)

    def test_invalid_raise_does_not_mutate_stack(self):
        table = self.make_table()
        before = table.players[1].stack
        with self.assertRaises(ValueError):
            table.act(1, "raise", -500)
        self.assertEqual(table.players[1].stack, before)

    def test_dealer_rotates_between_hands(self):
        table = self.make_table()
        first = table.dealer
        table.act(1, "fold")
        table.reset()
        for uid in table.players:
            table.ready(uid)
        table.start()
        self.assertNotEqual(table.dealer, first)


if __name__ == "__main__":
    unittest.main()
