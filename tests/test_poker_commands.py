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


if __name__ == "__main__":
    unittest.main()
