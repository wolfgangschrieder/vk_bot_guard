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


if __name__ == "__main__":
    unittest.main()
