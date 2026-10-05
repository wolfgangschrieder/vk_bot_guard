import sqlite3
import tempfile
import unittest
from datetime import date
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

    def test_daily_message_and_mute_stats(self):
        timestamp = 1_760_000_000
        db.record_message(self.conn, 10, timestamp)
        db.record_message(self.conn, 10, timestamp)
        db.record_message(self.conn, 20, timestamp)
        db.record_mute(self.conn, 10, timestamp)

        local_date = __import__("datetime").datetime.fromtimestamp(
            timestamp, __import__("config").CHAT_TZ
        ).date()
        stats = db.get_daily_stats(self.conn, local_date)

        self.assertEqual(stats["messages"], 3)
        self.assertEqual(stats["mutes"], 1)
        self.assertEqual(stats["top_users"][0], (10, 2))

    def test_reputation_one_vote_per_user_per_week(self):
        self.assertTrue(
            db.add_reputation_vote(
                self.conn, 1, 2, 1, "2026-10-05", now=1000
            )
        )
        self.assertFalse(
            db.add_reputation_vote(
                self.conn, 1, 2, -1, "2026-10-05", now=1001
            )
        )
        self.assertEqual(db.get_reputation(self.conn, 2), 1)

        self.assertTrue(
            db.add_reputation_vote(
                self.conn, 1, 2, -1, "2026-10-12", now=2000
            )
        )
        self.assertEqual(db.get_reputation(self.conn, 2), 0)

    def test_king_is_saved_once_per_day(self):
        self.assertTrue(db.save_king(self.conn, date(2026, 10, 5), 42, 100))
        self.assertFalse(db.save_king(self.conn, date(2026, 10, 5), 99, 200))
        self.assertEqual(
            db.get_king(self.conn, date(2026, 10, 5)),
            (42, 100),
        )

    def test_scheduler_event_is_idempotent(self):
        self.assertTrue(db.claim_scheduler_event(self.conn, "daily:2026-10-05-12"))
        self.assertFalse(db.claim_scheduler_event(self.conn, "daily:2026-10-05-12"))

    def test_legacy_database_survives_migration(self):
        self.conn.close()
        path = Path(self.tmp.name) / "legacy.db"

        legacy = sqlite3.connect(path)
        legacy.execute(
            """
            CREATE TABLE users (
                user_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                last_message_time INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0,
                mute_until INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, peer_id)
            )
            """
        )
        legacy.execute(
            "INSERT INTO users(user_id, peer_id, last_message_time, warnings, mute_until) "
            "VALUES (77, 2000000002, 1000, 3, 2000)"
        )
        legacy.commit()
        legacy.close()

        migrated = db.connect(str(path))
        row = migrated.execute(
            "SELECT user_id, peer_id, warnings, mute_until "
            "FROM users WHERE user_id = 77 AND peer_id = 2000000002"
        ).fetchone()

        self.assertEqual(tuple(row), (77, 2000000002, 3, 2000))
        self.assertEqual(
            migrated.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0],
            2,
        )
        self.assertTrue((Path(self.tmp.name) / "backups").exists())

        self.conn = migrated


if __name__ == "__main__":
    unittest.main()
