import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("VK_TOKEN", "test-token")
os.environ.setdefault("GROUP_ID", "123456")
os.environ.setdefault("CHAT_PEER_ID", "2000000002")
os.environ.setdefault("MODERATION_CHAT_PEER_ID", "2000000001")

import bot
import database as db


class MenuTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "test.db"))
        self.tz = ZoneInfo("Europe/Moscow")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_menu_is_sent_once_per_local_day_and_is_temporary(self):
        first = int(datetime(2026, 11, 21, 12, tzinfo=self.tz).timestamp())
        next_day = int(datetime(2026, 11, 22, 12, tzinfo=self.tz).timestamp())
        with patch.object(bot, "send_bot_message", return_value=123) as send:
            self.assertTrue(bot.handle_menu_command(None, self.conn, 42, "/menu", first))
            self.assertTrue(bot.handle_menu_command(None, self.conn, 42, "/menu", first + 60))
            self.assertEqual(send.call_count, 1)
            self.assertTrue(send.call_args.kwargs["temporary"])
            self.assertEqual(send.call_args.kwargs["delete_after_seconds"], 120)
            self.assertTrue(bot.handle_menu_command(None, self.conn, 42, "/menu", next_day))
            self.assertEqual(send.call_count, 2)

    def test_failed_send_does_not_consume_cooldown(self):
        now = int(datetime(2026, 11, 21, 12, tzinfo=self.tz).timestamp())
        with patch.object(bot, "send_bot_message", side_effect=[0, 123]) as send:
            self.assertTrue(bot.handle_menu_command(None, self.conn, 42, "/menu", now))
            self.assertTrue(bot.handle_menu_command(None, self.conn, 42, "/menu", now + 1))
            self.assertEqual(send.call_count, 2)

    def test_non_menu_is_ignored(self):
        self.assertFalse(bot.handle_menu_command(None, self.conn, 42, "/menu extra", 1))


if __name__ == "__main__":
    unittest.main()
