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

    def test_showdown_report_pot_awards_match_settlement(self):
        table = self.make_table(3)
        table.board = ["Ah", "Kd", "Qc", "Js", "2h"]
        table.players[1].hole = ("Tc", "3d")
        table.players[2].hole = ("Td", "4c")
        table.players[3].hole = ("9c", "9d")
        for uid, chips in ((1, 101), (2, 201), (3, 201)):
            table.players[uid].committed = chips
        table.showdown()
        report = table.showdown_report
        self.assertEqual(len(report["hands"]), 3)
        self.assertEqual([pot["amount"] for pot in report["pots"]], [303, 200])
        combined = {}
        for pot in report["pots"]:
            self.assertEqual(sum(pot["awards"].values()), pot["amount"])
            for uid, chips in pot["awards"].items():
                combined[uid] = combined.get(uid, 0) + chips
        self.assertEqual({uid: chips for uid, chips in table.payouts.items() if chips}, combined)
        self.assertEqual(sum(combined.values()), 503)
        table.reset()
        self.assertEqual(table.showdown_report, {})

    def test_fold_win_does_not_reveal_hole_cards(self):
        table = self.make_table()
        table.act(1, "fold")
        self.assertEqual(table.showdown_report, {})

    def test_lobby_unready_timeout_preserves_ready_players_and_chips(self):
        table = Table()
        table.join(1, stack=0)
        table.join(2, stack=500)
        table.join(3, stack=1000)
        for player in table.players.values():
            player.joined_at = 1000
        table.ready(2)
        self.assertEqual(table.expire_unready(now=1299), [])
        self.assertEqual(table.expire_unready(now=1300), [1, 3])
        self.assertEqual(list(table.players), [2])
        self.assertEqual(table.owner_id, 2)
        self.assertEqual(table.players[2].stack, 500)

    def test_lobby_timeout_never_removes_during_active_hand(self):
        table = self.make_table()
        for player in table.players.values():
            player.joined_at = 100
        self.assertEqual(table.expire_unready(now=1000), [])
        self.assertEqual(len(table.players), 2)

    def test_legacy_lobby_seat_gets_full_grace_period(self):
        table = Table()
        table.join(1)
        table.players[1].joined_at = 0
        self.assertEqual(table.expire_unready(now=5000), [])
        self.assertEqual(table.players[1].joined_at, 5000)
        self.assertEqual(table.expire_unready(now=5299), [])
        self.assertEqual(table.expire_unready(now=5300), [1])

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
        self.assertEqual(table.players[1].street_bet, 10)
        self.assertEqual(table.players[2].street_bet, 20)

    def test_fold_awards_pot(self):
        table = self.make_table()
        table.act(1, "fold")
        self.assertEqual(table.stage, "complete")
        self.assertEqual(table.players[2].stack, 10010)
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


    def test_underposted_big_blind_runs_out_without_betting(self):
        table = Table()
        table.join(1)
        table.join(2)
        table.players[2].stack = 10
        table.ready(1)
        table.ready(2)
        table.start()
        self.assertEqual(table.stage, "preflop")
        self.assertEqual(len(table.board), 0)
        table.next_street()  # Only after the delivery layer confirms all hole cards.
        self.assertEqual(table.stage, "complete")
        self.assertEqual(len(table.board), 5)
        self.assertEqual(sum(p.stack for p in table.players.values()), 10010)

    def test_uncalled_showdown_raise_refunded_before_side_pots(self):
        table = Table()
        for uid in (1, 2, 3):
            table.join(uid)
        table.board = ["Ah", "Kd", "Qc", "Js", "2h"]
        for uid, hole in ((1, ("Tc", "3d")), (2, ("9c", "9d")), (3, ("8c", "8d"))):
            table.players[uid].hole = hole
        for uid, amount in ((1, 300), (2, 100), (3, 100)):
            table.players[uid].committed = amount
            table.players[uid].stack -= amount
        before = sum(p.stack + p.committed for p in table.players.values())
        table.showdown()
        self.assertEqual(table.players[1].committed, 100)
        self.assertEqual(table.players[1].stack, 10200)
        self.assertEqual(table.showdown_report["pots"][0]["amount"], 300)
        self.assertEqual(sum(p.stack for p in table.players.values()), before)

    def test_folded_player_cannot_hold_unmatched_showdown_bet(self):
        table = Table()
        table.join(1)
        table.join(2)
        table.players[1].committed = 200
        table.players[1].folded = True
        table.players[2].committed = 100
        with self.assertRaisesRegex(ValueError, "Folded player"):
            table._refund_uncalled_bet()

    def test_randomized_side_pot_conservation_and_report(self):
        from random import Random
        from poker.evaluator import DECK
        rng = Random(20261127)
        for _ in range(60):
            count = rng.randint(2, 9)
            table = Table()
            for uid in range(1, count + 1):
                table.join(uid)
            cards = rng.sample(DECK, count * 2 + 5)
            table.board = list(cards[:5])
            levels = sorted(rng.sample(range(1, 900), count))
            for index, player in enumerate(table.seated()):
                player.hole = tuple(cards[5 + 2 * index:7 + 2 * index])
                contribution = levels[index]
                player.committed = contribution
                player.stack -= contribution
            # The top contributor must remain eligible for a legal showdown.
            for player in table.seated()[:-1]:
                player.folded = rng.choice([False, False, True])
            before = sum(p.stack + p.committed for p in table.players.values())
            table.showdown()
            self.assertEqual(sum(p.stack for p in table.players.values()), before)
            self.assertEqual(sum(table.payouts.values()),
                             sum(p.committed for p in table.players.values()))
            self.assertEqual(sum(pot["amount"] for pot in table.showdown_report["pots"]),
                             sum(p.committed for p in table.players.values()))
            self.assertEqual(sum(sum(pot["awards"].values())
                                 for pot in table.showdown_report["pots"]),
                             sum(table.payouts.values()))

    def test_new_session_preserves_all_stacks(self):
        table = self.make_table()
        table.act(1, "fold")
        table.new_session()
        self.assertEqual(table.stage, "waiting")
        self.assertEqual(table.hand_number, 1)  # Outbox IDs stay unique across sessions
        self.assertEqual([p.stack for p in table.seated()], [9990, 10010])
        self.assertTrue(all(not p.ready and not p.hole for p in table.seated()))

    def test_new_session_cannot_interrupt_hand(self):
        table = self.make_table()
        with self.assertRaises(ValueError):
            table.new_session()

    def test_no_raise_when_every_opponent_is_all_in(self):
        table = Table()
        for uid in (1, 2, 3):
            table.join(uid)
            table.ready(uid)
        table.players[2].stack = 10
        table.players[3].stack = 10
        table.start()
        self.assertEqual(table.turn, 1)
        before = table.players[1].stack
        with self.assertRaises(ValueError):
            table.act(1, "raise", 500)
        self.assertEqual(table.players[1].stack, before)
        table.act(1, "call")
        self.assertEqual(table.stage, "complete")
        self.assertEqual(sum(p.stack for p in table.players.values()), 10020)

    def test_timeout_folds_when_facing_bet(self):
        table = Table()
        for uid in (101, 202):
            table.join(uid)
            table.ready(uid)
        table.start()
        current = table.turn
        started = table.turn_started_at
        self.assertIsNone(table.expire_turn(started + 119))
        self.assertEqual(table.turn, current)
        self.assertEqual(table.expire_turn(started + 120), (current, "fold"))
        self.assertEqual(table.stage, "complete")
        self.assertNotIn(current, table.players)
        self.assertIn(current, table.ejected_balances)
        self.assertEqual(table.turn_started_at, 0.0)

    def test_timeout_folds_even_when_check_is_free_and_ejects_at_completion(self):
        table = self.make_table(3)
        for _ in range(3):
            owed = table.current_bet - table.players[table.turn].street_bet
            table.act(table.turn, "call" if owed else "check")
        self.assertEqual(table.stage, "flop")
        first = table.turn
        table.expire_turn(table.turn_started_at + 120)
        self.assertIn(first, table.pending_ejections)
        self.assertTrue(table.players[first].folded)
        self.assertIn(first, table.players)
        second = table.turn
        table.expire_turn(table.turn_started_at + 120)
        self.assertEqual(table.stage, "complete")
        self.assertNotIn(first, table.players)
        self.assertNotIn(second, table.players)
        self.assertEqual(len(table.ejected_balances), 2)

    def test_timeout_checks_when_no_call_required(self):
        table = Table()
        for uid in (101, 202):
            table.join(uid)
            table.ready(uid)
        table.start()
        table.act(table.turn, "call")
        table.act(table.turn, "check")
        self.assertEqual(table.stage, "flop")
        current = table.turn
        started = table.turn_started_at
        self.assertEqual(table.expire_turn(started + 120), (current, "fold"))
        self.assertEqual(table.stage, "complete")

    def test_three_way_unequal_allins_conserve_chips(self):
        table = Table()
        for uid in (1, 2, 3):
            table.join(uid)
            table.ready(uid)
        table.players[1].stack = 500
        table.players[2].stack = 1500
        table.players[3].stack = 3000
        table.start()
        self.assertEqual(table.turn, 1)
        table.act(1, "allin")
        table.act(2, "allin")
        table.act(3, "call")
        self.assertEqual(table.stage, "complete")
        self.assertEqual(len(table.board), 5)
        self.assertEqual(sum(p.stack for p in table.players.values()), 5000)
        self.assertEqual(sum(table.payouts.values()), 3500)
        self.assertTrue(all(p.stack >= 0 for p in table.players.values()))

    def test_fresh_session_does_not_reuse_hand_number(self):
        table = self.make_table()
        table.act(table.turn, "fold")
        table.new_session()
        for uid in table.players:
            table.ready(uid)
        table.start()
        self.assertEqual(table.hand_number, 2)

    def test_owner_transfers_when_leaving_lobby(self):
        table = Table()
        table.join(101)
        table.join(202)
        table.join(303)
        self.assertEqual(table.owner_id, 101)
        table.leave(101)
        self.assertEqual(table.owner_id, 202)
        table.leave(202)
        self.assertEqual(table.owner_id, 303)
        table.leave(303)
        self.assertEqual(table.owner_id, 0)
        table.join(404)
        self.assertEqual(table.owner_id, 404)

    def test_cumulative_short_allins_reopen_raising(self):
        table = self.make_table(4)
        table.stage = "flop"
        table.turn = 1
        table.current_bet = 0
        table.min_raise = 100
        for p in table.players.values():
            p.street_bet = 0
            p.acted = False
            p.can_raise = True
            p.last_action_bet = 0
        table.players[2].stack = 60
        table.players[3].stack = 120
        table.act(1, "check")
        table.act(2, "allin")
        self.assertEqual(table.current_bet, 60)
        table.act(3, "allin")
        self.assertEqual(table.current_bet, 120)
        table.act(4, "call")
        self.assertEqual(table.turn, 1)
        table.act(1, "raise", 240)
        self.assertEqual(table.current_bet, 240)

    def test_one_short_allin_does_not_reopen_raising(self):
        table = self.make_table(3)
        table.stage = "flop"
        table.turn = 1
        table.current_bet = 0
        table.min_raise = 100
        for p in table.players.values():
            p.street_bet = 0
            p.acted = False
            p.can_raise = True
            p.last_action_bet = 0
        table.players[2].stack = 60
        table.act(1, "check")
        table.act(2, "allin")
        table.act(3, "call")
        self.assertEqual(table.turn, 1)
        with self.assertRaisesRegex(ValueError, "Betting not reopened"):
            table.act(1, "raise", 200)
        self.assertEqual(table.turn, 1)
        table.act(1, "call")

    def test_nine_players_check_down_to_showdown(self):
        table = self.make_table(9)
        self.assertEqual(table.turn, 4)
        starting_chips = sum(p.stack + p.committed for p in table.players.values())
        for _ in range(9):
            current = table.turn
            owed = table.current_bet - table.players[current].street_bet
            table.act(current, "call" if owed else "check")
        self.assertEqual(table.stage, "flop")
        for street in ("turn", "river", "complete"):
            for _ in range(9):
                table.act(table.turn, "check")
            self.assertEqual(table.stage, street)
        self.assertEqual(len(table.board), 5)
        self.assertEqual(sum(p.stack for p in table.players.values()), starting_chips)
        self.assertEqual(sum(table.payouts.values()), 180)
        self.assertEqual(len(set(table.board)), 5)


    def test_timeout_then_normal_fold_finalizes_ejection_and_preserves_chips(self):
        table = self.make_table(3)
        initial = sum(p.stack + p.committed for p in table.players.values())
        timed_out = table.turn
        table.expire_turn(table.turn_started_at + 120)
        self.assertIn(timed_out, table.pending_ejections)
        while table.stage != "complete":
            table.act(table.turn, "fold")
        self.assertNotIn(timed_out, table.players)
        self.assertIn(timed_out, table.ejected_balances)
        self.assertEqual(table.pending_ejections, [])
        self.assertEqual(
            sum(p.stack for p in table.players.values()) + sum(table.ejected_balances.values()),
            initial,
        )

    def test_timeout_then_normal_showdown_finalizes_ejection(self):
        table = self.make_table(3)
        initial = sum(p.stack + p.committed for p in table.players.values())
        # Complete preflop; then time out one player on the flop.
        while table.stage == "preflop":
            uid = table.turn
            owed = table.current_bet - table.players[uid].street_bet
            table.act(uid, "call" if owed else "check")
        timed_out = table.turn
        table.expire_turn(table.turn_started_at + 120)
        self.assertEqual(table.stage, "flop")
        while table.stage != "complete":
            uid = table.turn
            owed = table.current_bet - table.players[uid].street_bet
            table.act(uid, "call" if owed else "check")
        self.assertNotIn(timed_out, table.players)
        self.assertIn(timed_out, table.ejected_balances)
        self.assertEqual(table.pending_ejections, [])
        self.assertEqual(
            sum(p.stack for p in table.players.values()) + sum(table.ejected_balances.values()),
            initial,
        )

    def test_owner_timeout_transfers_ownership_after_normal_completion(self):
        table = self.make_table(3)
        owner = table.owner_id
        while table.turn != owner and table.stage == "preflop":
            uid = table.turn
            owed = table.current_bet - table.players[uid].street_bet
            table.act(uid, "call" if owed else "check")
        self.assertEqual(table.turn, owner)
        table.expire_turn(table.turn_started_at + 120)
        while table.stage != "complete":
            table.act(table.turn, "fold")
        self.assertNotIn(owner, table.players)
        self.assertIn(table.owner_id, table.players)
        self.assertNotEqual(table.owner_id, owner)

    def test_cancel_undelivered_hand_refunds_blinds_and_preserves_hand_id(self):
        table = self.make_table(3)
        before = sum(p.stack + p.committed for p in table.players.values())
        number = table.hand_number
        table.cancel_undelivered_hand()
        self.assertEqual(table.stage, "waiting")
        self.assertEqual(table.hand_number, number)
        self.assertEqual(sum(p.stack for p in table.players.values()), before)
        self.assertTrue(all(p.committed == 0 and not p.hole for p in table.players.values()))

    def test_side_pot_ties_and_odd_chip_conservation(self):
        from poker.evaluator import settle_pots
        board = ("Ah", "Kd", "Qc", "Js", "2h")
        hands = {1: ("Tc", "3d"), 2: ("Td", "4c"), 3: ("9c", "9d")}
        contributions = {1: 101, 2: 201, 3: 201}
        payouts = settle_pots(contributions, hands, board, [2, 1, 3])
        self.assertEqual(payouts, {1: 151, 2: 352, 3: 0})
        self.assertEqual(sum(payouts.values()), sum(contributions.values()))

if __name__ == "__main__":
    unittest.main()
