import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db


class RouletteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.temp.name) / "roulette.db"))
        self.peer = 2000000002
        self.day = "2026-11-26"

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def create(self, challenger=1, opponent=2, now=1000):
        return db.create_chat_roulette(
            self.conn, self.peer, challenger, opponent, now, now + 300,
            self.day, is_admin=False,
        )

    def test_six_box_game_turns_and_shared_profile(self):
        status, game_id = self.create()
        self.assertEqual(status, "created")
        with patch("secrets.randbelow", return_value=3):
            status, _ = db.accept_chat_roulette(
                self.conn, self.peer, 2, 1001, 1301, self.day,
            )
        self.assertEqual(status, "accepted")
        game = db.get_chat_duel(self.conn, game_id)
        self.assertEqual(game["target_number"], 4)
        self.assertEqual(game["next_player_id"], 1)
        self.assertEqual(db.open_chat_roulette_box(self.conn, self.peer, 2, 1, 1002)[0], "turn")
        self.assertEqual(db.open_chat_roulette_box(self.conn, self.peer, 1, 1, 1002)[0], "safe")
        self.assertEqual(db.open_chat_roulette_box(self.conn, self.peer, 2, 1, 1003)[0], "occupied")
        self.assertEqual(db.open_chat_roulette_box(self.conn, self.peer, 2, 4, 1003)[0], "lost")
        self.assertEqual(db.get_chat_duel_record(self.conn, 1, self.peer), (1, 0))
        self.assertEqual(db.get_chat_duel_record(self.conn, 2, self.peer), (0, 1))
        self.assertEqual(db.get_reputation(self.conn, 1), 1)
        self.assertEqual(db.get_reputation(self.conn, 2), -1)
        self.assertEqual(db.open_chat_roulette_box(self.conn, self.peer, 1, 5, 1004)[0], "missing")

    def test_challenger_always_starts(self):
        for challenger, opponent in ((1, 2), (3, 4), (5, 6)):
            status, game_id = self.create(challenger=challenger, opponent=opponent)
            self.assertEqual(status, "created")
            with patch("secrets.randbelow", return_value=5):
                status, _ = db.accept_chat_roulette(
                    self.conn, self.peer, opponent, 1001, 1301, self.day,
                )
            self.assertEqual(status, "accepted")
            game = db.get_chat_duel(self.conn, game_id)
            self.assertEqual(game["next_player_id"], challenger)
            with db.atomic(self.conn):
                self.conn.execute("UPDATE chat_duels SET status='expired' WHERE duel_id=?", (game_id,))

    def test_five_invitations_and_one_started_game(self):
        for i in range(5):
            status, game_id = self.create(opponent=i + 2, now=1000 + i)
            self.assertEqual(status, "created")
            with db.atomic(self.conn):
                self.conn.execute("UPDATE chat_duels SET status='expired' WHERE duel_id=?", (game_id,))
        self.assertEqual(self.create(opponent=20)[0], "limit")
        # Other users' invitations remain acceptable despite own challenge quota.
        status, game_id = self.create(challenger=21, opponent=1)
        self.assertEqual(status, "created")
        self.assertEqual(db.accept_chat_roulette(
            self.conn, self.peer, 1, 1001, 1301, self.day,
        )[0], "accepted")

    def test_roulette_and_duel_quota_are_separate(self):
        self.assertEqual(self.create()[0], "created")
        status, _ = db.create_chat_duel(
            self.conn, self.peer, 1, 3, 1001, 1301, self.day, is_admin=False,
        )
        self.assertEqual(status, "busy")
        # A roulette invitation must never be accepted as a normal duel.
        self.assertEqual(db.accept_chat_duel(
            self.conn, self.peer, 2, 1001, 1301, self.day, is_admin=False,
        )[0], "missing")

    def test_expired_turn_cannot_be_played(self):
        self.create()
        db.accept_chat_roulette(self.conn, self.peer, 2, 1001, 1301, self.day)
        self.assertEqual(db.open_chat_roulette_box(
            self.conn, self.peer, 1, 1, 1302,
        )[0] in ("expired", "turn"), True)


if __name__ == "__main__":
    unittest.main()
