import tempfile
import unittest
from pathlib import Path

import database as db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.tmp.name) / "test.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_first_message_is_allowed(self):
        result = db.register_message(self.conn, 1, 2, now=1000)
        self.assertFalse(result["should_mute"])

    def test_second_message_within_hour_is_muted(self):
        db.register_message(self.conn, 1, 2, now=1000)
        result = db.register_message(self.conn, 1, 2, now=1001)
        self.assertTrue(result["should_mute"])
        self.assertFalse(result["already_muted"])

    def test_message_after_hour_is_allowed(self):
        db.register_message(self.conn, 1, 2, now=1000)
        result = db.register_message(self.conn, 1, 2, now=4600)
        self.assertFalse(result["should_mute"])

    def test_muted_user_stays_muted(self):
        db.register_message(self.conn, 1, 2, now=1000)
        db.register_message(self.conn, 1, 2, now=1001)
        result = db.register_message(self.conn, 1, 2, now=1002)
        self.assertTrue(result["already_muted"])


if __name__ == "__main__":
    unittest.main()
