import tempfile
import unittest
from pathlib import Path

from poker.commands import PokerCommands, parse_command
from poker.storage import PokerStore


class CommandTests(unittest.TestCase):
    def test_parser(self):
        self.assertEqual(parse_command("/raise 500"), ("raise", 500))
        self.assertIsNone(parse_command("обычное сообщение"))
        with self.assertRaises(ValueError):
            parse_command("/raise -5")

    def test_lobby_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999)
            self.assertIn("Место 1", bot.handle(101, "/join", "1"))
            self.assertIsNone(bot.handle(101, "/join", "1"))
            self.assertIn("Место 2", bot.handle(202, "/join", "2"))
            self.assertIn("готов", bot.handle(101, "/ready", "3"))
            self.assertIn("отключена", bot.handle(101, "/start", "4"))
            self.assertEqual(store.load(2000000999)[1].stage, "waiting")
            store.close()

    def test_join_requires_private_message_permission(self):
        class Denied:
            def can_message(self, user_id):
                return False
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999, private_delivery=Denied())
            self.assertIn("Разрешите личные сообщения", bot.handle(101, "/join", "a"))
            self.assertEqual(store.load(2000000999), (0, None))
            store.close()

    def test_full_hand_delivery_failure_and_recovery(self):
        class Messages:
            def __init__(self):
                self.calls = []
                self.fail_once = True
            def send(self, **kwargs):
                self.calls.append(kwargs)
                if self.fail_once and len(self.calls) == 2:
                    self.fail_once = False
                    raise RuntimeError("VK temporary failure")
                return 123

        class FakeVK:
            def __init__(self):
                self.messages = Messages()

        from poker.private_cards import VkPrivateCards
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            vk = FakeVK()
            adapter = VkPrivateCards(vk, 123)
            adapter.can_message = lambda user_id: True
            bot = PokerCommands(store, 2000000999, private_delivery=adapter)
            for uid in (101, 202):
                self.assertIn("Место", bot.handle(uid, "/join", "join-" + str(uid)))
                bot.handle(uid, "/ready", "ready-" + str(uid))
            reply = bot.handle(101, "/start", "start-1")
            self.assertIn("приостановлена", reply)
            table = store.load(2000000999)[1]
            self.assertEqual(table.stage, "preflop")
            self.assertEqual(store.private_delivery_status(2000000999, 1), (2, 1))
            self.assertIn("Доставка", bot.handle(table.turn, "/call", "blocked"))
            store.close()
            reopened = PokerStore(Path(tmp) / "poker.db")
            recovered = PokerCommands(reopened, 2000000999, private_delivery=adapter)
            self.assertIn("PREFLOP", recovered.handle(101, "/table", "recovery"))
            self.assertEqual(reopened.private_delivery_status(2000000999, 1), (2, 2))
            self.assertEqual(vk.messages.calls[1]["random_id"],
                             vk.messages.calls[2]["random_id"])
            self.assertEqual(len(vk.messages.calls), 3)
            reopened.close()

    def test_complete_hand_next_hand_and_fresh_session(self):
        class Delivery:
            def can_message(self, uid):
                return True
            def deliver_pending(self, store, room, hand):
                for uid, random_id, msg in store.pending_private_cards(room, hand):
                    store.mark_private_delivered(room, hand, uid)

        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999, private_delivery=Delivery())
            for uid in (101, 202):
                bot.handle(uid, "/join", "join" + str(uid))
                bot.handle(uid, "/ready", "ready" + str(uid))
            self.assertIn("PREFLOP", bot.handle(101, "/start", "start1"))
            first = store.load(2000000999)[1]
            self.assertEqual(first.hand_number, 1)
            self.assertIn("COMPLETE", bot.handle(first.turn, "/fold", "fold1"))
            self.assertIn("WAITING", bot.handle(101, "/next", "next1"))
            for uid in (101, 202):
                bot.handle(uid, "/ready", "ready2" + str(uid))
            self.assertIn("PREFLOP", bot.handle(101, "/start", "start2"))
            second = store.load(2000000999)[1]
            self.assertEqual(second.hand_number, 2)
            self.assertIn("COMPLETE", bot.handle(second.turn, "/fold", "fold2"))
            self.assertIn("WAITING", bot.handle(101, "/newsession", "session2"))
            fresh = store.load(2000000999)[1]
            self.assertTrue(all(p.stack == 10000 for p in fresh.players.values()))
            for uid in (101, 202):
                bot.handle(uid, "/ready", "ready3" + str(uid))
            self.assertIn("PREFLOP", bot.handle(101, "/start", "start3"))
            self.assertEqual(store.load(2000000999)[1].hand_number, 3)
            self.assertEqual(store.private_delivery_status(2000000999, 3), (2, 2))
            store.close()

    def test_unseated_user_cannot_reset_or_start_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999)
            bot.handle(101, "/join", "join101")
            bot.handle(202, "/join", "join202")
            for command in ("/start", "/next", "/newsession"):
                self.assertIn("Only seated players",
                              bot.handle(999, command, "other" + command))
            self.assertEqual(store.load(2000000999)[1].stage, "waiting")
            store.close()


if __name__ == "__main__":
    unittest.main()
