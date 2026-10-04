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

def record_hourly_message(conn, user_id, message_time=None, audio=0, voice=0, video=0, image=0):
    message_time = int(time.time()) if message_time is None else int(message_time)
    with _lock:
        conn.execute("""CREATE TABLE IF NOT EXISTS hourly_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_time INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            audio INTEGER NOT NULL DEFAULT 0,
            voice INTEGER NOT NULL DEFAULT 0,
            video INTEGER NOT NULL DEFAULT 0,
            image INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute(
            """INSERT INTO hourly_messages
            (message_time, user_id, audio, voice, video, image)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (message_time, user_id, audio, voice, video, image),
        )
        conn.commit()

def ensure_hourly_stats_table(conn):
    with _lock:
        conn.execute("""CREATE TABLE IF NOT EXISTS hourly_stats (
            hour_start INTEGER PRIMARY KEY,
            total_messages INTEGER NOT NULL,
            audio_messages INTEGER NOT NULL,
            voice_messages INTEGER NOT NULL,
            video_messages INTEGER NOT NULL,
            image_messages INTEGER NOT NULL,
            unique_users INTEGER NOT NULL,
            activity_score REAL NOT NULL,
            user_activity_score REAL NOT NULL
        )""")
        conn.commit()

def build_hourly_stats(conn, hour_start):
    hour_end = hour_start + 3600
    with _lock:
        rows = conn.execute(
            """SELECT user_id, COUNT(*) AS messages,
                      SUM(audio) AS audio, SUM(voice) AS voice,
                      SUM(video) AS video, SUM(image) AS image
               FROM hourly_messages
               WHERE message_time >= ? AND message_time < ?
               GROUP BY user_id ORDER BY messages DESC, user_id ASC""",
            (hour_start, hour_end),
        ).fetchall()
        total = sum(int(r["messages"]) for r in rows)
        audio = sum(int(r["audio"] or 0) for r in rows)
        voice = sum(int(r["voice"] or 0) for r in rows)
        video = sum(int(r["video"] or 0) for r in rows)
        image = sum(int(r["image"] or 0) for r in rows)
        unique_users = len(rows)
        baseline = conn.execute(
            "SELECT AVG(total_messages) AS avg_messages, AVG(unique_users) AS avg_users FROM hourly_stats"
        ).fetchone()
        avg_messages = float(baseline["avg_messages"] or 0)
        avg_users = float(baseline["avg_users"] or 0)
        activity_score = 50.0 if avg_messages <= 0 else min(100.0, max(0.0, 50.0 * total / avg_messages))
        user_activity_score = 50.0 if avg_users <= 0 else min(100.0, max(0.0, 50.0 * unique_users / avg_users))
        conn.execute(
            """INSERT OR REPLACE INTO hourly_stats
               (hour_start, total_messages, audio_messages, voice_messages,
                video_messages, image_messages, unique_users,
                activity_score, user_activity_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hour_start, total, audio, voice, video, image, unique_users,
             activity_score, user_activity_score),
        )
        conn.commit()
        return {
            "hour_start": hour_start, "total_messages": total,
            "audio_messages": audio, "voice_messages": voice,
            "video_messages": video, "image_messages": image,
            "unique_users": unique_users, "activity_score": activity_score,
            "user_activity_score": user_activity_score,
            "baseline_messages": avg_messages, "baseline_users": avg_users,
            "top_users": [(int(r["user_id"]), int(r["messages"])) for r in rows[:3]],
        }
