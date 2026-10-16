import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

import config
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
        self.assertEqual(stats["photos"], 0)
        self.assertEqual(stats["videos"], 0)
        self.assertEqual(stats["music"], 0)
        self.assertEqual(stats["voices"], 0)
        self.assertEqual(stats["top_users"][0], (10, 2))

    def test_daily_stats_includes_media_columns(self):
        timestamp = 1_760_000_000
        db.record_message(
            self.conn,
            10,
            timestamp,
            {"image": 2, "video": 3, "audio": 4, "voice": 5},
        )

        local_date = __import__("datetime").datetime.fromtimestamp(
            timestamp, __import__("config").CHAT_TZ
        ).date()
        stats = db.get_daily_stats(self.conn, local_date)

        self.assertEqual(
            {
                "photos": stats["photos"],
                "videos": stats["videos"],
                "music": stats["music"],
                "voices": stats["voices"],
            },
            {"photos": 2, "videos": 3, "music": 4, "voices": 5},
        )

    def test_daily_active_users_and_activity_vs_previous_seven_days(self):
        stat_date = date(2026, 10, 9)
        for days_ago in range(1, 8):
            timestamp = int(datetime.combine(
                stat_date - timedelta(days=days_ago), time(12), config.CHAT_TZ
            ).timestamp())
            for index in range(10):
                db.record_message(self.conn, 10 + index % 2, timestamp)

        today = int(datetime.combine(stat_date, time(12), config.CHAT_TZ).timestamp())
        for _ in range(13):
            db.record_message(self.conn, 10, today)
        for _ in range(2):
            db.record_message(self.conn, 20, today)
        db.record_mute(self.conn, 30, today)

        stats = db.get_daily_stats(self.conn, stat_date)

        self.assertEqual(stats["active_users"], 2)
        self.assertEqual(stats["activity_average_messages"], 10)
        self.assertEqual(stats["activity_percent"], 150)

    def test_activity_percent_is_unavailable_without_historical_messages(self):
        stat_date = date(2026, 10, 9)
        timestamp = int(datetime.combine(stat_date, time(12), config.CHAT_TZ).timestamp())
        db.record_message(self.conn, 10, timestamp)

        stats = db.get_daily_stats(self.conn, stat_date)

        self.assertEqual(stats["active_users"], 1)
        self.assertIsNone(stats["activity_percent"])

    def test_daily_chat_records_are_saved_only_when_beaten(self):
        first_day = date(2026, 10, 1)
        first_timestamp = int(datetime.combine(first_day, time(12), config.CHAT_TZ).timestamp())
        db.record_message(self.conn, 10, first_timestamp)
        db.record_message(self.conn, 20, first_timestamp)
        self.assertEqual(
            db.record_daily_chat_stats(self.conn, first_day),
            {"active_users": 2},
        )

        second_day = first_day + timedelta(days=1)
        second_timestamp = int(datetime.combine(second_day, time(12), config.CHAT_TZ).timestamp())
        for user_id in (10, 20, 30):
            for _ in range(14 // 3 + (user_id == 10)):
                db.record_message(self.conn, user_id, second_timestamp)
        records = db.record_daily_chat_stats(self.conn, second_day)

        self.assertEqual(records["active_users"], 3)
        self.assertGreater(records["activity_percent"], 100)
        self.assertEqual(db.record_daily_chat_stats(self.conn, second_day), {})

    def test_record_migration_backfills_existing_daily_history(self):
        first_day = date(2026, 10, 1)
        for day, user_messages in (
            (first_day, ((10, 4), (20, 4))),
            (first_day + timedelta(days=1), ((10, 8), (20, 4), (30, 4))),
        ):
            timestamp = int(datetime.combine(day, time(12), config.CHAT_TZ).timestamp())
            for user_id, message_count in user_messages:
                for _ in range(message_count):
                    db.record_message(self.conn, user_id, timestamp)

        self.conn.execute("DROP TABLE chat_stat_records")
        self.conn.execute("DELETE FROM schema_migrations WHERE version = 9")
        self.conn.commit()
        migrated = db.connect(str(Path(self.tmp.name) / "test.db"))
        rows = {
            row["metric"]: (row["record_value"], row["stat_date"])
            for row in migrated.execute("SELECT * FROM chat_stat_records")
        }

        self.assertEqual(rows["active_users"], (3.0, "2026-10-02"))
        self.assertEqual(rows["activity_percent"], (1400.0, "2026-10-02"))
        migrated.close()

    def test_daily_user_stats(self):
        timestamp = 1_760_000_000
        db.record_message(self.conn, 10, timestamp)
        db.record_message(self.conn, 10, timestamp)
        db.record_message(self.conn, 20, timestamp)
        db.record_mute(self.conn, 10, timestamp)

        local_date = __import__("datetime").datetime.fromtimestamp(
            timestamp, __import__("config").CHAT_TZ
        ).date()
        stats = db.get_daily_user_stats(self.conn, local_date, [10, 20, 30])

        self.assertEqual(stats[10], {"messages": 2, "mutes": 1})
        self.assertEqual(stats[20], {"messages": 1, "mutes": 0})
        self.assertEqual(stats[30], {"messages": 0, "mutes": 0})

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
        self.assertFalse(
            db.add_reputation_vote(
                self.conn, 1, 3, 1, "2026-10-05", now=1002
            )
        )

        self.assertTrue(
            db.add_reputation_vote(
                self.conn, 1, 2, -1, "2026-10-12", now=2000
            )
        )
        self.assertEqual(db.get_reputation(self.conn, 2), 0)

    def test_processed_message_is_idempotent(self):
        self.assertTrue(db.claim_processed_message(self.conn, 2000000001, 123, 1000))
        self.assertFalse(db.claim_processed_message(self.conn, 2000000001, 123, 1001))
        self.assertTrue(db.claim_processed_message(self.conn, 2000000002, 123, 1002))

    def test_reputation_duplicate_can_be_detected_silently(self):
        self.assertFalse(db.has_reputation_vote(self.conn, 1, "2026-10-05"))
        self.assertTrue(db.add_reputation_vote(self.conn, 1, 2, 1, "2026-10-05", now=1000))
        self.assertTrue(db.has_reputation_vote(self.conn, 1, "2026-10-05"))

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

    def test_chat_blacklist_add_remove_and_idempotency(self):
        self.assertTrue(db.add_chat_blacklist(self.conn, 42, 99, now=1000))
        self.assertFalse(db.add_chat_blacklist(self.conn, 42, 77, now=1001))
        self.assertTrue(db.is_chat_blacklisted(self.conn, 42))
        self.assertEqual(db.get_chat_blacklist(self.conn), [42])
        self.assertTrue(db.remove_chat_blacklist(self.conn, 42))
        self.assertFalse(db.remove_chat_blacklist(self.conn, 42))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 42))

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
            db.SCHEMA_VERSION,
        )
        self.assertTrue((Path(self.tmp.name) / "backups").exists())

        self.conn = migrated


if __name__ == "__main__":
    unittest.main()
