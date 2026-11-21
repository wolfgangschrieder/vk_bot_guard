"""Daily chat statistics export to the project's Google Sheet."""

import logging
import time
from datetime import date, datetime, timedelta

import requests

import config
import database as db

log = logging.getLogger("vk_bot_guard.chat_stats_export")

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEET_NAME = "Статистика"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
HEADERS = [
    "Дата (Москва)", "Сообщения", "Активные участники", "Активность чата, %",
    "Среднее сообщений за предыдущие 7 дней", "Фото", "Видео", "Музыка",
    "Голосовые", "Муты в чате 2000000002", "Герой дня, ID",
    "Сообщения Героя дня",
    "Топ-1 участник, ID", "Топ-1 сообщений", "Топ-2 участник, ID",
    "Топ-2 сообщений", "Топ-3 участник, ID", "Топ-3 сообщений",
    "Топ-4 участник, ID", "Топ-4 сообщений", "Топ-5 участник, ID",
    "Топ-5 сообщений",
]
_last_attempt_at = 0.0
RETRY_SECONDS = 300


def _google_access_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        str(config.GOOGLE_SERVICE_ACCOUNT_FILE), scopes=SCOPES,
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("Google не выдал access token для записи статистики.")
    return credentials.token


def _values_url(a1_range: str) -> str:
    return (
        f"{SHEETS_API}/{config.CHAT_STATS_GOOGLE_SHEET_ID}/values/"
        f"{requests.utils.quote(a1_range, safe='')}"
    )


def _stored_dates(conn, before_date: date) -> list[date]:
    with db._lock:
        row = conn.execute(
            "SELECT MIN(stat_date) FROM ("
            "SELECT stat_date FROM daily_stats UNION ALL "
            "SELECT stat_date FROM chat_mute_stats WHERE peer_id = ?) "
            "WHERE stat_date < ?",
            (config.CHAT_PEER_ID, before_date.isoformat()),
        ).fetchone()
    if not row or not row[0]:
        return []
    first = date.fromisoformat(row[0])
    return [first + timedelta(days=offset)
            for offset in range((before_date - first).days)]


def build_daily_row(conn, stat_date: date) -> list:
    stats = db.get_daily_stats(conn, stat_date)
    with db._lock:
        mute_row = conn.execute(
            "SELECT COALESCE(SUM(mutes), 0) FROM chat_mute_stats "
            "WHERE stat_date = ? AND peer_id = ?",
            (stat_date.isoformat(), config.CHAT_PEER_ID),
        ).fetchone()
    hero = db.get_king(conn, stat_date)
    row = [
        stat_date.isoformat(),
        stats["messages"],
        stats["active_users"],
        "" if stats["activity_percent"] is None else stats["activity_percent"],
        round(stats["activity_average_messages"], 2),
        stats["photos"], stats["videos"], stats["music"], stats["voices"],
        int(mute_row[0] or 0),
        hero[0] if hero else "", hero[1] if hero else "",
    ]
    for index in range(5):
        if index < len(stats["top_users"]):
            user_id, messages = stats["top_users"][index]
            row.extend([user_id, messages])
        else:
            row.extend(["", ""])
    return row


def export_history(conn, before_date: date, *, session=None, force=False) -> bool:
    """Upsert every completed day in SQLite, including gaps as zero-activity days."""
    global _last_attempt_at
    now = time.monotonic()
    if not force and now - _last_attempt_at < RETRY_SECONDS:
        return False
    _last_attempt_at = now

    if not config.CHAT_STATS_GOOGLE_SHEET_ID:
        log.warning("Не задан CHAT_STATS_GOOGLE_SHEET_ID; выгрузка статистики пропущена.")
        return False
    if not config.GOOGLE_SERVICE_ACCOUNT_FILE.is_file():
        log.warning("Не найден ключ Google service account: %s", config.GOOGLE_SERVICE_ACCOUNT_FILE)
        return False

    dates = _stored_dates(conn, before_date)
    if not dates:
        return True

    session = session or requests.Session()
    try:
        token = _google_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        read = session.get(
            _values_url(f"'{SHEET_NAME}'!A:A"), headers=headers,
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
            timeout=15,
        )
        read.raise_for_status()
        existing_rows = read.json().get("values", [])
        if not existing_rows or existing_rows[0][:len(HEADERS)] != HEADERS:
            log.error("Лист «%s»: заголовки не совпадают с форматом выгрузки.", SHEET_NAME)
            return False
        row_by_date = {}
        for row_index, cells in enumerate(existing_rows[1:], start=2):
            if cells:
                value = str(cells[0]).strip()
                try:
                    key = date.fromisoformat(value).isoformat()
                except ValueError:
                    try:
                        key = datetime.strptime(value, "%d.%m.%Y").date().isoformat()
                    except ValueError:
                        continue
                row_by_date[key] = row_index

        next_row = max(2, len(existing_rows) + 1)
        updates = []
        for stat_date in dates:
            key = stat_date.isoformat()
            target_row = row_by_date.get(key)
            if target_row is None:
                target_row = next_row
                next_row += 1
                row_by_date[key] = target_row
            updates.append({
                "range": f"'{SHEET_NAME}'!A{target_row}:V{target_row}",
                "values": [build_daily_row(conn, stat_date)],
            })

        response = session.post(
            f"{SHEETS_API}/{config.CHAT_STATS_GOOGLE_SHEET_ID}/values:batchUpdate",
            headers={**headers, "Content-Type": "application/json"},
            json={"valueInputOption": "RAW", "data": updates},
            timeout=30,
        )
        response.raise_for_status()
        log.info("Статистика чата выгружена: дней=%s, по=%s", len(updates), before_date.isoformat())
        return True
    except Exception:
        log.exception("Не удалось выгрузить статистику чата в Google Таблицу.")
        return False
