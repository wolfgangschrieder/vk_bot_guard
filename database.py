import sqlite3
import threading
import time
from typing import Optional

import config

_lock = threading.Lock()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(config.DB_PATH)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            last_message_time INTEGER NOT NULL DEFAULT 0,
            warnings INTEGER NOT NULL DEFAULT 0,
            mute_until INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, peer_id)
        )
        """
    )
    conn.commit()
    return conn


def _get_or_create(conn: sqlite3.Connection, user_id: int, peer_id: int) -> sqlite3.Row:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (user_id, peer_id)
        VALUES (?, ?)
        """,
        (user_id, peer_id),
    )
    return conn.execute(
        "SELECT * FROM users WHERE user_id = ? AND peer_id = ?",
        (user_id, peer_id),
    ).fetchone()


def register_message(
    conn: sqlite3.Connection,
    user_id: int,
    peer_id: int,
    now: Optional[int] = None,
) -> dict:
    """Register a message using a sliding one-hour window."""
    now = int(time.time()) if now is None else int(now)

    with _lock:
        row = _get_or_create(conn, user_id, peer_id)
        last = int(row["last_message_time"])
        warnings = int(row["warnings"])
        mute_until = int(row["mute_until"])

        if mute_until > now:
            # If the configuration was changed from a longer mute period,
            # never keep an active restriction longer than the current policy.
            max_mute_until = now + config.MUTE_SECONDS
            if mute_until > max_mute_until:
                mute_until = max_mute_until
                conn.execute(
                    """
                    UPDATE users
                    SET mute_until = ?
                    WHERE user_id = ? AND peer_id = ?
                    """,
                    (mute_until, user_id, peer_id),
                )
                conn.commit()

            return {
                "should_mute": False,
                "already_muted": True,
                "seconds_left": mute_until - now,
                "warnings": warnings,
                "mute_until": mute_until,
            }

        if last == 0 or now - last >= config.RATE_LIMIT_SECONDS:
            conn.execute(
                """
                UPDATE users
                SET last_message_time = ?
                WHERE user_id = ? AND peer_id = ?
                """,
                (now, user_id, peer_id),
            )
            conn.commit()
            return {
                "should_mute": False,
                "already_muted": False,
                "seconds_left": 0,
                "warnings": warnings,
                "mute_until": 0,
            }

        warnings += 1
        mute_until = now + config.MUTE_SECONDS
        conn.execute(
            """
            UPDATE users
            SET last_message_time = ?, warnings = ?, mute_until = ?
            WHERE user_id = ? AND peer_id = ?
            """,
            (now, warnings, mute_until, user_id, peer_id),
        )
        conn.commit()
        return {
            "should_mute": True,
            "already_muted": False,
            "seconds_left": config.RATE_LIMIT_SECONDS - (now - last),
            "warnings": warnings,
            "mute_until": mute_until,
        }


def clear_expired_mutes(
    conn: sqlite3.Connection,
    now: Optional[int] = None,
) -> list[tuple[int, int]]:
    now = int(time.time()) if now is None else int(now)

    with _lock:
        rows = conn.execute(
            """
            SELECT user_id, peer_id
            FROM users
            WHERE mute_until > 0 AND mute_until <= ?
            """,
            (now,),
        ).fetchall()

        expired = [(int(row["user_id"]), int(row["peer_id"])) for row in rows]

        if expired:
            conn.execute(
                """
                UPDATE users
                SET mute_until = 0
                WHERE mute_until > 0 AND mute_until <= ?
                """,
                (now,),
            )
            conn.commit()

        return expired
