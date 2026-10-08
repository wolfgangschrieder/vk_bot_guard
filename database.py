import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import config

_lock = threading.Lock()
SCHEMA_VERSION = 4


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(config.DB_PATH)
    existed = Path(path).exists()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    _ensure_schema(conn, path if existed else None)
    return conn


def _backup_before_migration(path: str) -> None:
    source = Path(path)
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"{source.stem}_{stamp}.db"
    shutil.copy2(source, destination)


def _ensure_schema(conn: sqlite3.Connection, db_path: str | None = None) -> None:
    with _lock:
        migration_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()

        if migration_table:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current = int((row["version"] if row else 0) or 0)
        else:
            current = 0

        if db_path and current < SCHEMA_VERSION:
            _backup_before_migration(db_path)

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )

        if current == 0:
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, int(time.time())),
            )
            current = 1

        if current < 2:
            _migration_2(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (2, int(time.time())),
            )
            current = 2

        if current < 3:
            _migration_3(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (3, int(time.time())),
            )
            current = 3

        if current < 4:
            _migration_4(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (4, int(time.time())),
            )
            current = 4

        # Repair databases created by the previous migration path: schema
        # version 4 could exist even though migration 3 was skipped.
        _migration_3(conn)

        conn.commit()


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_user_stats (
            stat_date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            messages INTEGER NOT NULL DEFAULT 0,
            mutes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (stat_date, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_stats (
            stat_date TEXT PRIMARY KEY,
            messages INTEGER NOT NULL DEFAULT 0,
            mutes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS king_history (
            stat_date TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            messages INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reputation_votes (
            giver_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            week_key TEXT NOT NULL,
            value INTEGER NOT NULL CHECK(value IN (-1, 1)),
            created_at INTEGER NOT NULL,
            PRIMARY KEY (giver_id, week_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_cooldowns (
            user_id INTEGER NOT NULL,
            command TEXT NOT NULL,
            target_user_id INTEGER NOT NULL DEFAULT 0,
            last_used_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, command, target_user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_messages (
            message_id INTEGER PRIMARY KEY,
            peer_id INTEGER NOT NULL,
            delete_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_state (
            event_key TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL
        )
        """
    )


def _migration_3(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            peer_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            processed_at INTEGER NOT NULL,
            PRIMARY KEY (peer_id, message_id)
        )
        """
    )


def _migration_4(conn: sqlite3.Connection) -> None:
    for column in ("photos", "videos", "music", "voices"):
        conn.execute(
            f"ALTER TABLE daily_stats ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
        )


def _get_or_create(
    conn: sqlite3.Connection,
    user_id: int,
    peer_id: int,
) -> sqlite3.Row:
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


def record_message(
    conn: sqlite3.Connection,
    user_id: int,
    message_time: Optional[int] = None,
    media_counts: Optional[dict[str, int]] = None,
) -> None:
    """Increment daily chat and per-user message counters."""
    timestamp = int(time.time()) if message_time is None else int(message_time)
    stat_date = datetime.fromtimestamp(
        timestamp, config.CHAT_TZ
    ).date().isoformat()
    media_counts = media_counts or {}
    photos = max(0, int(media_counts.get("image", 0)))
    videos = max(0, int(media_counts.get("video", 0)))
    music = max(0, int(media_counts.get("audio", 0)))
    voices = max(0, int(media_counts.get("voice", 0)))

    with _lock:
        conn.execute(
            """
            INSERT INTO daily_user_stats(stat_date, user_id, messages, mutes)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(stat_date, user_id)
            DO UPDATE SET messages = messages + 1
            """,
            (stat_date, user_id),
        )
        conn.execute(
            """
            INSERT INTO daily_stats(
                stat_date, messages, mutes, photos, videos, music, voices
            )
            VALUES (?, 1, 0, ?, ?, ?, ?)
            ON CONFLICT(stat_date)
            DO UPDATE SET
                messages = messages + 1,
                photos = photos + excluded.photos,
                videos = videos + excluded.videos,
                music = music + excluded.music,
                voices = voices + excluded.voices
            """,
            (stat_date, photos, videos, music, voices),
        )
        conn.commit()


def record_mute(
    conn: sqlite3.Connection,
    user_id: int,
    mute_time: Optional[int] = None,
) -> None:
    """Increment only aggregate mute counters; no per-mute history is stored."""
    timestamp = int(time.time()) if mute_time is None else int(mute_time)
    stat_date = datetime.fromtimestamp(
        timestamp, config.CHAT_TZ
    ).date().isoformat()

    with _lock:
        conn.execute(
            """
            INSERT INTO daily_user_stats(stat_date, user_id, messages, mutes)
            VALUES (?, ?, 0, 1)
            ON CONFLICT(stat_date, user_id)
            DO UPDATE SET mutes = mutes + 1
            """,
            (stat_date, user_id),
        )
        conn.execute(
            """
            INSERT INTO daily_stats(stat_date, messages, mutes)
            VALUES (?, 0, 1)
            ON CONFLICT(stat_date)
            DO UPDATE SET mutes = mutes + 1
            """,
            (stat_date,),
        )
        conn.commit()


def get_daily_user_stats(
    conn: sqlite3.Connection,
    stat_date: date,
    user_ids: list[int],
) -> dict[int, dict[str, int]]:
    if not user_ids:
        return {}

    key = stat_date.isoformat()
    unique_ids = list(dict.fromkeys(int(uid) for uid in user_ids if int(uid) > 0))
    if not unique_ids:
        return {}

    placeholders = ",".join("?" for _ in unique_ids)
    with _lock:
        rows = conn.execute(
            f"""
            SELECT user_id, messages, mutes
            FROM daily_user_stats
            WHERE stat_date = ? AND user_id IN ({placeholders})
            """,
            (key, *unique_ids),
        ).fetchall()

    result = {
        user_id: {"messages": 0, "mutes": 0}
        for user_id in unique_ids
    }
    for row in rows:
        user_id = int(row["user_id"])
        result[user_id] = {
            "messages": int(row["messages"] or 0),
            "mutes": int(row["mutes"] or 0),
        }
    return result


def get_daily_stats(
    conn: sqlite3.Connection,
    stat_date: date,
) -> dict:
    key = stat_date.isoformat()
    with _lock:
        total = conn.execute(
            "SELECT messages, mutes, photos, videos, music, voices FROM daily_stats WHERE stat_date = ?",
            (key,),
        ).fetchone()
        top = conn.execute(
            """
            SELECT user_id, messages
            FROM daily_user_stats
            WHERE stat_date = ? AND messages > 0
            ORDER BY messages DESC, user_id ASC
            LIMIT 5
            """,
            (key,),
        ).fetchall()

    return {
        "date": key,
        "messages": int(total["messages"]) if total else 0,
        "mutes": int(total["mutes"]) if total else 0,
        "photos": int(total["photos"]) if total else 0,
        "videos": int(total["videos"]) if total else 0,
        "music": int(total["music"]) if total else 0,
        "voices": int(total["voices"]) if total else 0,
        "top_users": [
            (int(row["user_id"]), int(row["messages"])) for row in top
        ],
    }


def get_weekly_stats(
    conn: sqlite3.Connection,
    start_date: date,
    end_date: date,
) -> dict:
    with _lock:
        totals = conn.execute(
            """
            SELECT COALESCE(SUM(messages), 0) AS messages,
                   COALESCE(SUM(mutes), 0) AS mutes,
                   COALESCE(SUM(photos), 0) AS photos,
                   COALESCE(SUM(videos), 0) AS videos,
                   COALESCE(SUM(music), 0) AS music,
                   COALESCE(SUM(voices), 0) AS voices
            FROM daily_stats
            WHERE stat_date >= ? AND stat_date < ?
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchone()

        top = conn.execute(
            """
            SELECT user_id, SUM(messages) AS messages
            FROM daily_user_stats
            WHERE stat_date >= ? AND stat_date < ?
            GROUP BY user_id
            HAVING SUM(messages) > 0
            ORDER BY messages DESC, user_id ASC
            LIMIT 5
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()

    return {
        "messages": int(totals["messages"] or 0),
        "mutes": int(totals["mutes"] or 0),
        "photos": int(totals["photos"] or 0),
        "videos": int(totals["videos"] or 0),
        "music": int(totals["music"] or 0),
        "voices": int(totals["voices"] or 0),
        "top_users": [
            (int(row["user_id"]), int(row["messages"])) for row in top
        ],
    }


def get_king(
    conn: sqlite3.Connection,
    stat_date: date,
) -> Optional[tuple[int, int]]:
    key = stat_date.isoformat()
    with _lock:
        row = conn.execute(
            """
            SELECT user_id, messages
            FROM king_history
            WHERE stat_date = ?
            """,
            (key,),
        ).fetchone()
    if not row:
        return None
    return int(row["user_id"]), int(row["messages"])


def save_king(
    conn: sqlite3.Connection,
    stat_date: date,
    user_id: int,
    messages: int,
) -> bool:
    key = stat_date.isoformat()
    with _lock:
        existing = conn.execute(
            "SELECT 1 FROM king_history WHERE stat_date = ?",
            (key,),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """
            INSERT INTO king_history(stat_date, user_id, messages)
            VALUES (?, ?, ?)
            """,
            (key, user_id, messages),
        )
        conn.commit()
        return True


def has_reputation_vote(
    conn: sqlite3.Connection,
    giver_id: int,
    week_key: str,
) -> bool:
    with _lock:
        row = conn.execute(
            "SELECT 1 FROM reputation_votes WHERE giver_id = ? AND week_key = ? LIMIT 1",
            (giver_id, week_key),
        ).fetchone()
    return row is not None


def get_reputation(conn: sqlite3.Connection, user_id: int) -> int:
    with _lock:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(value), 0) AS reputation
            FROM reputation_votes
            WHERE receiver_id = ?
            """,
            (user_id,),
        ).fetchone()
    return int(row["reputation"] or 0)


def add_reputation_vote(
    conn: sqlite3.Connection,
    giver_id: int,
    receiver_id: int,
    value: int,
    week_key: str,
    now: Optional[int] = None,
) -> bool:
    if giver_id == receiver_id or value not in (-1, 1):
        return False

    timestamp = int(time.time()) if now is None else int(now)
    with _lock:
        existing = conn.execute(
            "SELECT 1 FROM reputation_votes WHERE giver_id = ? AND week_key = ? LIMIT 1",
            (giver_id, week_key),
        ).fetchone()
        if existing:
            return False

        try:
            conn.execute(
                """
                INSERT INTO reputation_votes
                    (giver_id, receiver_id, week_key, value, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (giver_id, receiver_id, week_key, value, timestamp),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def claim_processed_message(
    conn: sqlite3.Connection,
    peer_id: int,
    message_id: int,
    processed_at: Optional[int] = None,
) -> bool:
    if peer_id <= 0 or message_id <= 0:
        return True

    timestamp = int(time.time()) if processed_at is None else int(processed_at)
    with _lock:
        try:
            conn.execute(
                """
                INSERT INTO processed_messages(peer_id, message_id, processed_at)
                VALUES (?, ?, ?)
                """,
                (peer_id, message_id, timestamp),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_command_cooldown(
    conn: sqlite3.Connection,
    user_id: int,
    command: str,
    target_user_id: int = 0,
) -> int:
    with _lock:
        row = conn.execute(
            """
            SELECT last_used_at
            FROM command_cooldowns
            WHERE user_id = ? AND command = ? AND target_user_id = ?
            """,
            (user_id, command, target_user_id),
        ).fetchone()
    return int(row["last_used_at"]) if row else 0


def set_command_cooldown(
    conn: sqlite3.Connection,
    user_id: int,
    command: str,
    target_user_id: int,
    now: Optional[int] = None,
) -> None:
    timestamp = int(time.time()) if now is None else int(now)
    with _lock:
        conn.execute(
            """
            INSERT INTO command_cooldowns(user_id, command, target_user_id, last_used_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, command, target_user_id)
            DO UPDATE SET last_used_at = excluded.last_used_at
            """,
            (user_id, command, target_user_id, timestamp),
        )
        conn.commit()


def queue_bot_message(
    conn: sqlite3.Connection,
    message_id: int,
    peer_id: int,
    delete_at: int,
) -> None:
    if message_id <= 0:
        return
    with _lock:
        conn.execute(
            """
            INSERT OR REPLACE INTO bot_messages(message_id, peer_id, delete_at)
            VALUES (?, ?, ?)
            """,
            (message_id, peer_id, delete_at),
        )
        conn.commit()


def get_due_bot_messages(
    conn: sqlite3.Connection,
    now: Optional[int] = None,
) -> list[tuple[int, int]]:
    timestamp = int(time.time()) if now is None else int(now)
    with _lock:
        rows = conn.execute(
            """
            SELECT message_id, peer_id
            FROM bot_messages
            WHERE delete_at <= ?
            ORDER BY delete_at ASC
            """,
            (timestamp,),
        ).fetchall()
    return [(int(row["message_id"]), int(row["peer_id"])) for row in rows]


def remove_bot_message(conn: sqlite3.Connection, message_id: int) -> None:
    with _lock:
        conn.execute(
            "DELETE FROM bot_messages WHERE message_id = ?",
            (message_id,),
        )
        conn.commit()


def scheduler_event_claimed(
    conn: sqlite3.Connection,
    event_key: str,
) -> bool:
    with _lock:
        row = conn.execute(
            "SELECT 1 FROM scheduler_state WHERE event_key = ? LIMIT 1",
            (event_key,),
        ).fetchone()
    return row is not None


def claim_scheduler_event(
    conn: sqlite3.Connection,
    event_key: str,
    now: Optional[int] = None,
) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    with _lock:
        try:
            conn.execute(
                """
                INSERT INTO scheduler_state(event_key, created_at)
                VALUES (?, ?)
                """,
                (event_key, timestamp),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
