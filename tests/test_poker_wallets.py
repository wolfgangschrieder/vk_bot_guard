"""Regression coverage for one-time poker bankrolls and admin replenishment."""
import tempfile
import unittest
from pathlib import Path

from poker.commands import PokerCommands
from poker.storage import PokerStore


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PokerStore(Path(self.tmp.name) / "poker.db")
        self.room = 2000000404

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_reputation_is_applied_only_at_first_join(self):
        ratings = {101: 3, 202: 7}
        bot = PokerCommands(self.store, self.room, initial_chips=lambda uid: ratings[uid] * 1000)
        bot.handle(101, "/join", "join-1")
        bot.handle(202, "/join", "join-2")
        self.assertEqual(self.store.wallet(101), 3000)
        self.assertEqual(self.store.wallet(202), 7000)
        bot.handle(101, "/leave", "leave-1")
        ratings[101] = 100
        bot.handle(101, "/join", "join-3")
        self.assertEqual(self.store.wallet(101), 3000)

    def test_wallet_survives_restart_and_session_reset(self):
        bot = PokerCommands(self.store, self.room, initial_chips=lambda uid: 2000)
        for uid in (101, 202):
            bot.handle(uid, "/join", f"join-{uid}")
            bot.handle(uid, "/ready", f"ready-{uid}")
        class Delivery:
            def can_message(self, uid):
                return True
            def deliver_pending(self, store, room, hand):
                for uid, _, _ in store.pending_private_cards(room, hand):
                    store.mark_private_delivered(room, hand, uid)
        bot.private_delivery = Delivery()
        bot.handle(101, "/start", "start")
        _, table = self.store.load(self.room)
        bot.handle(table.turn, "/fold", "fold")
        balances = {uid: self.store.wallet(uid) for uid in (101, 202)}
        self.assertEqual(sum(balances.values()), 4000)
        bot.handle(101, "/newsession", "session")
        self.assertEqual({uid: self.store.wallet(uid) for uid in balances}, balances)
        self.store.close()
        self.store = PokerStore(Path(self.tmp.name) / "poker.db")
        self.assertEqual({uid: self.store.wallet(uid) for uid in balances}, balances)

    def test_only_zero_wallet_can_be_replenished(self):
        bot = PokerCommands(self.store, self.room, initial_chips=lambda uid: 0)
        bot.handle(101, "/join", "join")
        self.assertEqual(self.store.wallet(101), 0)
        self.store.admin_top_up(self.room, 101, 500, 999, 123456)
        self.assertEqual(self.store.wallet(101), 500)
        self.assertEqual(self.store.load(self.room)[1].players[101].stack, 500)
        with self.assertRaisesRegex(ValueError, "нулевом"):
            self.store.admin_top_up(self.room, 101, 500, 999, 123457)
        with self.assertRaisesRegex(ValueError, "ещё не создавал"):
            self.store.admin_top_up(self.room, 102, 500, 999, 123457)
        audit = self.store.conn.execute("SELECT user_id,admin_id,chips FROM poker_wallet_audit").fetchall()
        self.assertEqual(audit, [(101, 999, 500)])

    def test_admin_cannot_change_wallet_during_hand(self):
        bot = PokerCommands(self.store, self.room, initial_chips=lambda uid: 1000)
        for uid in (101, 202):
            bot.handle(uid, "/join", f"join-{uid}")
            bot.handle(uid, "/ready", f"ready-{uid}")
        class Delivery:
            def can_message(self, uid):
                return True
            def deliver_pending(self, store, room, hand):
                for uid, _, _ in store.pending_private_cards(room, hand):
                    store.mark_private_delivered(room, hand, uid)
        bot.private_delivery = Delivery()
        bot.handle(101, "/start", "start")
        with self.assertRaisesRegex(ValueError, "окончания"):
            self.store.admin_top_up(self.room, 101, 100, 999, 123456)


if __name__ == "__main__":
    unittest.main()
