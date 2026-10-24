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
        self.conn.commit()

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

    def close(self):
        self.conn.close()
