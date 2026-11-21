import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import chat_stats_export as export
import config
import database as db


class ChatStatsExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.tmp.name) / "stats.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def timestamp(self, day: int) -> int:
        return int(datetime(2026, 11, day, 12, tzinfo=config.CHAT_TZ).timestamp())

    def test_daily_row_contains_aggregate_media_mute_hero_and_top_five(self):
        target = date(2026, 11, 20)
        for user_id, count in ((21, 3), (22, 2), (23, 1)):
            for _ in range(count):
                db.record_message(self.conn, user_id, self.timestamp(20), {"image": 1})
        db.record_mute(self.conn, 21, self.timestamp(20), peer_id=config.CHAT_PEER_ID)
        db.save_king(self.conn, target, 21, 3)

        row = export.build_daily_row(self.conn, target)

        self.assertEqual(len(row), len(export.HEADERS))
        self.assertEqual(row[:12], [
            "2026-11-20", 6, 3, "", 0, 6, 0, 0, 0, 1, 21, 3,
        ])
        self.assertEqual(row[12:18], [21, 3, 22, 2, 23, 1])

    def test_export_backfills_missing_dates_and_updates_existing_dates(self):
        for day in (19, 21):
            db.record_message(self.conn, 7, self.timestamp(day))
        key_file = Path(self.tmp.name) / "service-account.json"
        key_file.write_text("{}", encoding="utf-8")
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {"values": [export.HEADERS]}
        session.get.return_value = response
        session.post.return_value.raise_for_status.return_value = None

        with patch.object(config, "GOOGLE_SERVICE_ACCOUNT_FILE", key_file), \
                patch.object(config, "CHAT_STATS_GOOGLE_SHEET_ID", "sheet-id"), \
                patch.object(export, "_google_access_token", return_value="test-token"):
            self.assertTrue(export.export_history(
                self.conn, date(2026, 11, 22), session=session, force=True,
            ))

        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(len(payload["data"]), 3)
        self.assertEqual(
            [entry["range"] for entry in payload["data"]],
            ["'Статистика'!A2:V2", "'Статистика'!A3:V3", "'Статистика'!A4:V4"],
        )
        self.assertEqual([entry["values"][0][0] for entry in payload["data"]], [
            "2026-11-19", "2026-11-20", "2026-11-21",
        ])

    def test_export_rewrites_a_date_already_in_the_sheet(self):
        db.record_message(self.conn, 7, self.timestamp(20))
        key_file = Path(self.tmp.name) / "service-account.json"
        key_file.write_text("{}", encoding="utf-8")
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {"values": [export.HEADERS, ["2026-11-20"]]}
        session.get.return_value = response
        session.post.return_value.raise_for_status.return_value = None

        with patch.object(config, "GOOGLE_SERVICE_ACCOUNT_FILE", key_file), \
                patch.object(config, "CHAT_STATS_GOOGLE_SHEET_ID", "sheet-id"), \
                patch.object(export, "_google_access_token", return_value="test-token"):
            self.assertTrue(export.export_history(
                self.conn, date(2026, 11, 21), session=session, force=True,
            ))

        self.assertEqual(session.post.call_args.kwargs["json"]["data"][0]["range"],
                         "'Статистика'!A2:V2")


if __name__ == "__main__":
    unittest.main()
