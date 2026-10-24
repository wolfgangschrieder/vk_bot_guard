"""SQLite snapshot storage for the independent poker table.

This module never opens or migrates moderator.db.
"""
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .engine import Player, Table


def serialize(table):
    return json.dumps(asdict(table), ensure_ascii=False, separators=(",", ":"))


def deserialize(payload):
    raw = json.loads(payload)
    players = raw.pop("players")
    table = Table(**raw)
    table.players = {
        int(uid): Player(**{**data, "hole": tuple(data["hole"])})
        for uid, data in players.items()
    }
    table.payouts = {int(uid): amount for uid, amount in table.payouts.items()}
    return table


class PokerStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS poker_state ("
            "room_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, "
            "snapshot TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS poker_processed_events ("
            "room_id INTEGER NOT NULL, event_id TEXT NOT NULL, "
            "PRIMARY KEY(room_id, event_id))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS poker_private_outbox ("
            "room_id INTEGER NOT NULL, hand_number INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, random_id INTEGER NOT NULL, "
            "message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(room_id, hand_number, user_id), "
            "UNIQUE(random_id))"
        )
        self.conn.commit()

    def is_processed(self, room_id, event_id):
        """Return whether an incoming VK event was already committed."""
        row = self.conn.execute(
            "SELECT 1 FROM poker_processed_events WHERE room_id=? AND event_id=?",
            (room_id, str(event_id)),
        ).fetchone()
        return row is not None

    def load(self, room_id):
        row = self.conn.execute(
            "SELECT revision, snapshot FROM poker_state WHERE room_id=?", (room_id,)
        ).fetchone()
        return (row[0], deserialize(row[1])) if row else (0, None)

    def save(self, room_id, table, expected_revision, event_id=None):
        """Atomic optimistic revision check + optional VK event deduplication.

        Returns new revision, or None for a previously processed event.
        Raises ValueError on stale revisions.
        """
        with self.conn:
            if event_id is not None:
                seen = self.conn.execute(
                    "SELECT 1 FROM poker_processed_events WHERE room_id=? AND event_id=?",
                    (room_id, str(event_id)),
                ).fetchone()
                if seen:
                    return None
            current = self.conn.execute(
                "SELECT revision FROM poker_state WHERE room_id=?", (room_id,)
            ).fetchone()
            revision = current[0] if current else 0
            if revision != expected_revision:
                raise ValueError("Stale poker state revision")
            self.conn.execute(
                "INSERT INTO poker_state(room_id, revision, snapshot) VALUES (?, ?, ?) "
                "ON CONFLICT(room_id) DO UPDATE SET "
                "revision=excluded.revision, snapshot=excluded.snapshot",
                (room_id, revision + 1, serialize(table)),
            )
            if event_id is not None:
                self.conn.execute(
                    "INSERT INTO poker_processed_events(room_id,event_id) VALUES (?,?)",
                    (room_id, str(event_id)),
                )
        return revision + 1

    def save_hand_with_private_cards(self, room_id, table, expected_revision, messages, event_id=None):
        """Atomically persist a newly dealt hand and every private card job.

        messages maps user_id to (stable_random_id, message_text).
        No VK network calls happen in this transaction.
        """
        if table.stage not in ("preflop", "flop", "turn", "river", "complete"):
            raise ValueError("No dealt hand to persist")
        if set(messages) != set(table.players):
            raise ValueError("Must queue private cards for every seated player")
        with self.conn:
            if event_id is not None and self.is_processed(room_id, event_id):
                return None
            row = self.conn.execute(
                "SELECT revision FROM poker_state WHERE room_id=?", (room_id,)
            ).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise ValueError("Stale poker state revision")
            self.enqueue_private_cards(room_id, table.hand_number, messages)
            self.conn.execute(
                "INSERT INTO poker_state(room_id,revision,snapshot) VALUES (?,?,?) "
                "ON CONFLICT(room_id) DO UPDATE SET "
                "revision=excluded.revision,snapshot=excluded.snapshot",
                (room_id, revision + 1, serialize(table)),
            )
            if event_id is not None:
                self.conn.execute(
                    "INSERT INTO poker_processed_events(room_id,event_id) VALUES (?,?)",
                    (room_id, str(event_id)),
                )
        return revision + 1

    def enqueue_private_cards(self, room_id, hand_number, messages):
        """Persist card deliveries before any network call.

        messages maps user_id to (stable_random_id, message_text).
        Existing jobs are never overwritten, even after a restart.
        """
        with self.conn:
            for user_id, (random_id, message) in messages.items():
                if type(random_id) is not int or not 0 < random_id <= 2147483647:
                    raise ValueError("Invalid VK random_id")
                if not message:
                    raise ValueError("Empty private message")
                existing = self.conn.execute(
                    "SELECT random_id, message FROM poker_private_outbox "
                    "WHERE room_id=? AND hand_number=? AND user_id=?",
                    (room_id, hand_number, user_id),
                ).fetchone()
                if existing:
                    if existing != (random_id, message):
                        raise ValueError("Conflicting private-card delivery")
                    continue
                self.conn.execute(
                    "INSERT INTO poker_private_outbox "
                    "(room_id,hand_number,user_id,random_id,message) "
                    "VALUES (?,?,?,?,?)",
                    (room_id, hand_number, user_id, random_id, message),
                )

    def pending_private_cards(self, room_id, hand_number):
        return self.conn.execute(
            "SELECT user_id,random_id,message FROM poker_private_outbox "
            "WHERE room_id=? AND hand_number=? AND delivered=0 ORDER BY user_id",
            (room_id, hand_number),
        ).fetchall()

    def mark_private_delivered(self, room_id, hand_number, user_id):
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE poker_private_outbox SET delivered=1 "
                "WHERE room_id=? AND hand_number=? AND user_id=?",
                (room_id, hand_number, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Private delivery job not found")

    def close(self):
        self.conn.close()
