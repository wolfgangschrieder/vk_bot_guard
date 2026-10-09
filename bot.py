import logging
import json
import uuid
from logging.handlers import RotatingFileHandler
import re
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import vk_api
import requests
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.exceptions import ApiError, VkApiError

import config
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(config.LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("vk_bot_guard")

CHAT_PEER_START = 2_000_000_000


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 10)
        return super().request(method, url, **kwargs)


def is_target_chat(peer_id: int) -> bool:
    return peer_id in {
        config.CHAT_PEER_ID,
        config.MODERATION_CHAT_PEER_ID,
    }


def is_bot_feature_chat(peer_id: int) -> bool:
    return peer_id == config.CHAT_PEER_ID


def is_moderation_chat(peer_id: int) -> bool:
    return peer_id == config.MODERATION_CHAT_PEER_ID


def delete_message(vk, message) -> int:
    message_id = message_field(message, "id")
    cmid = message_field(message, "conversation_message_id")
    peer_id = message_field(message, "peer_id")
    if message_id > 0:
        params = {"message_ids": str(message_id), "delete_for_all": 1}
    elif cmid > 0 and peer_id > 0:
        params = {"peer_id": peer_id, "conversation_message_ids": str(cmid), "delete_for_all": 1}
    else:
        log.error("Нельзя удалить сообщение без id/cmid: peer=%s", peer_id)
        return 0
    return db.enqueue(ACTIVE_CONN, "delete", {"params": params})

def apply_mute(vk, peer_id: int, user_id: int, *, reason=None, king_id=None, now=None, deletion_id=None) -> bool:
    now = int(time.time()) if now is None else int(now)
    if db.mute_deadline(ACTIVE_CONN, peer_id, user_id) > now:
        return False
    db.enqueue(ACTIVE_CONN, "mute", {
        "peer_id": peer_id, "user_id": user_id, "until": now + config.MUTE_SECONDS,
        "reason": reason, "king_id": king_id, "requested_at": now,
        "deletion_id": deletion_id,
    })
    return True

def remove_user_from_chat(vk, peer_id: int, user_id: int) -> bool:
    chat_id = peer_id - CHAT_PEER_START
    if chat_id <= 0:
        return False

    try:
        vk.messages.removeChatUser(chat_id=chat_id, user_id=user_id)
        log.warning(
            "Заблокированный пользователь удалён из беседы: user=%s peer=%s",
            user_id,
            peer_id,
        )
        return True
    except ApiError as err:
        log.error(
            "Не удалось удалить пользователя user=%s peer=%s: %s",
            user_id,
            peer_id,
            err,
        )
        return False


def message_field(message, name: str, default: int = 0) -> int:
    if not message:
        return default

    if isinstance(message, dict):
        value = message.get(name, default)
    else:
        try:
            value = message[name]
        except Exception:
            value = getattr(message, name, default)

    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def event_object(event):
    obj = getattr(event, "obj", None)
    if obj is not None:
        return obj
    return getattr(event, "object", None)


def event_field(event_obj, name: str, default=None):
    if not event_obj:
        return default

    if isinstance(event_obj, dict):
        return event_obj.get(name, default)

    try:
        return event_obj[name]
    except Exception:
        return getattr(event_obj, name, default)


def handle_chat_update(vk, event) -> None:
    """Remove configured users immediately when they join the blocklist chat."""
    obj = event_object(event)
    if not obj:
        return

    peer_id = message_field(obj, "peer_id")
    if peer_id <= 0:
        chat_id = message_field(obj, "chat_id")
        peer_id = CHAT_PEER_START + chat_id if chat_id > 0 else 0

    if (
        peer_id != config.BLOCKLIST_CHAT_PEER_ID
        or peer_id == config.CHAT_PEER_ID
    ):
        return

    action = event_field(obj, "action")
    if isinstance(action, dict):
        action_type = action.get("type")
        member_id = action.get("member_id") or action.get("user_id")
    else:
        action_type = action
        member_id = event_field(obj, "member_id") or event_field(obj, "user_id")

    if action_type not in {"chat_invite_user", "chat_invite_user_by_link"}:
        return

    try:
        member_id = int(member_id or 0)
    except (TypeError, ValueError):
        member_id = 0

    if member_id in config.BLOCKED_USER_IDS:
        remove_user_from_chat(vk, peer_id, member_id)


def scan_blocklist_chat(vk) -> None:
    if not config.BLOCKED_USER_IDS:
        return

    try:
        response = vk.messages.getConversationMembers(
            peer_id=config.BLOCKLIST_CHAT_PEER_ID,
            count=1000,
        )
        members = response.get("items", []) if isinstance(response, dict) else []
        present_ids = {
            int(member.get("member_id", 0))
            for member in members
            if isinstance(member, dict) and int(member.get("member_id", 0) or 0) > 0
        }
        for user_id in present_ids & config.BLOCKED_USER_IDS:
            remove_user_from_chat(vk, config.BLOCKLIST_CHAT_PEER_ID, user_id)
    except Exception:
        log.error("Ошибка проверки blocklist-чата:\n%s", traceback.format_exc())


def blocklist_watchdog(vk) -> None:
    while True:
        try:
            scan_blocklist_chat(vk)
        except Exception:
            log.error("Ошибка blocklist watchdog:\n%s", traceback.format_exc())
        time.sleep(5)


def message_text(message) -> str:
    if isinstance(message, dict):
        return str(message.get("text", "") or "")
    return str(getattr(message, "text", "") or "")


def message_attachments(message):
    if isinstance(message, dict):
        return message.get("attachments") or []
    return getattr(message, "attachments", None) or []


def has_media_attachment(message) -> bool:
    for attachment in message_attachments(message):
        attachment_type = str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower()
        if attachment_type in {"audio", "audio_message", "video", "sticker"}:
            return True
    return False


def has_sticker_attachment(message) -> bool:
    return any(
        str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower() == "sticker"
        for attachment in message_attachments(message)
    )


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def contains_any_prohibited_term(text: str) -> bool:
    normalized = normalized_text(text)
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized)
        for term in config.PROHIBITED_LEXICON
    ) or any(re.search(pattern, normalized) for pattern in config.PRICE_PATTERNS)

def contains_porn_sale_term(text: str) -> bool:
    normalized = normalized_text(text)
    return (
        "порновидео" in normalized
        or "домашнее видео" in normalized
        or "продам видео" in normalized
    )


def contains_political_term(text: str) -> bool:
    normalized = normalized_text(text)
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized)
        for term in config.POLITICAL_TERMS
    )


def contains_phone_number(text: str) -> bool:
    patterns = (
        r"(?<!\d)(?:\+7|8|7)[\s().-]*(?:\d[\s().-]*){10}(?!\d)",
        r"(?<!\d)\+\d[\s().-]*(?:\d[\s().-]*){9,14}(?!\d)",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def card_luhn(number: str) -> bool:
    digits = re.sub(r"\D", "", number)
    if not 13 <= len(digits) <= 19:
        return False

    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def contains_card_number(text: str) -> bool:
    for match in re.finditer(r"(?<!\d)(?:\d[\s-]?){12,18}\d(?!\d)", text):
        if card_luhn(match.group(0)):
            return True
    return False


def contains_card_or_phone(text: str) -> bool:
    return contains_card_number(text) or contains_phone_number(text)


def classify_media(message):
    counters = {"audio": 0, "voice": 0, "video": 0, "image": 0}
    for attachment in message_attachments(message):
        attachment_type = str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower()
        if attachment_type == "audio":
            counters["audio"] += 1
        elif attachment_type == "audio_message":
            counters["voice"] += 1
        elif attachment_type == "video":
            counters["video"] += 1
        elif attachment_type == "photo":
            counters["image"] += 1
    return counters


def send_bot_message(
    vk, message: str, *, temporary: bool = False, peer_id: int | None = None,
    delete_after_seconds: int | None = None, depends_on: int | None = None,
) -> int:
    delay = (config.BOT_REASON_DELETE_SECONDS if delete_after_seconds is None
             else max(1, int(delete_after_seconds)))
    # This returns a durable delivery ID, not VK's message ID. VK delivery is retried.
    return db.enqueue(ACTIVE_CONN, "send", {
        "peer_id": peer_id or config.CHAT_PEER_ID, "message": message,
        "temporary": temporary, "delay": delay,
        "depends_on": depends_on,
    })

def send_mute_reason(vk, user_id: int, reason: str, peer_id: int, deletion_id=None) -> None:
    delete_after = (
        config.MODERATION_REASON_DELETE_SECONDS
        if is_moderation_chat(peer_id)
        else config.BOT_REASON_DELETE_SECONDS
    )
    names = get_user_names(vk, [user_id])
    user_name = names.get(user_id) or "Пользователь"
    send_bot_message(
        vk,
        f"{user_name}, {reason}",
        temporary=True,
        peer_id=peer_id,
        delete_after_seconds=delete_after,
        depends_on=deletion_id,
    )


def record_successful_mute(conn, user_id: int, peer_id: int, now=None) -> None:
    db.record_mute(conn, user_id, now, peer_id=peer_id)

def handle_content_violation(vk, conn, message, from_id: int, peer_id: int) -> bool:
    text = message_text(message)

    if is_moderation_chat(peer_id):
        if contains_porn_sale_term(text):
            reason = config.PORN_SALE_MUTE_REASON
        elif has_sticker_attachment(message):
            reason = config.STICKER_MUTE_REASON
        elif has_media_attachment(message):
            reason = config.MEDIA_MUTE_REASON
        elif contains_any_prohibited_term(text):
            reason = config.PROSTITUTION_MUTE_REASON
        else:
            return False
    elif is_bot_feature_chat(peer_id):
        # В 2000000002 слова из запрещённой лексики не являются
        # основанием для мута. Здесь модерируем только карты/телефоны.
        if contains_card_or_phone(text):
            reason = config.CARD_PHONE_MUTE_REASON
        else:
            return False
    else:
        return False

    deleted = delete_message(vk, message)
    muted = apply_mute(vk, peer_id, from_id, reason=reason, deletion_id=deleted)

    log.warning(
        "Автоматическое нарушение: user=%s deleted=%s muted=%s",
        from_id,
        deleted,
        muted,
    )
    return True


def mention(user_id: int, name: str | None = None) -> str:
    label = name or f"ID {user_id}"
    return f"[id{user_id}|{label}]"


def get_user_names(vk, user_ids: list[int]) -> dict[int, str]:
    if not user_ids:
        return {}

    unique_ids = list(dict.fromkeys(int(uid) for uid in user_ids if int(uid) > 0))
    try:
        profiles = vk.users.get(user_ids=",".join(map(str, unique_ids)))
    except ApiError as err:
        log.warning("Не удалось получить имена пользователей: %s", err)
        return {}

    return {
        int(profile["id"]): f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        for profile in profiles
    }


def format_top(vk, rows, medals: tuple[str, ...]) -> str:
    names = get_user_names(vk, [uid for uid, _ in rows])
    lines = []
    for index, (user_id, messages) in enumerate(rows):
        prefix = medals[index] if index < len(medals) else f"{index + 1}."
        lines.append(
            f"{prefix} {mention(user_id, names.get(user_id))} — {messages}"
        )
    return "\n".join(lines) if lines else "Пока нет сообщений."


def publish_daily_stats(vk, conn, stat_date: date) -> bool:
    stats = db.get_daily_stats(conn, stat_date)
    top3 = stats["top_users"][:3]
    month = config.MONTH_NAMES[stat_date.month - 1]

    report = (
        f"📊 СТАТИСТИКА ЧАТА\n"
        f"#{stat_date.day}{month}\n\n"
        f"💬 Сообщений за день: {stats['messages']}\n"
        f"📷 Фото: {stats['photos']}\n"
        f"🎬 Видео: {stats['videos']}\n"
        f"🎵 Музыка: {stats['music']}\n"
        f"🎙 Голосовые: {stats['voices']}\n\n"
        f"🏆 ТОП-3 АКТИВНЫХ\n"
        f"{format_top(vk, top3, ('🥇', '🥈', '🥉'))}"
    )
    return send_bot_message(vk, report) > 0


def publish_weekly_stats(vk, conn, start_date: date, end_date: date) -> bool:
    stats = db.get_weekly_stats(conn, start_date, end_date)
    month = config.MONTH_NAMES[start_date.month - 1]

    report = (
        f"📊 ИТОГИ НЕДЕЛИ\n"
        f"#{start_date.day}{month}\n\n"
        f"💬 Сообщений: {stats['messages']}\n"
        f"📷 Фото: {stats['photos']}\n"
        f"🎬 Видео: {stats['videos']}\n"
        f"🎵 Музыка: {stats['music']}\n"
        f"🎙 Голосовые: {stats['voices']}\n"
        f"🔇 Мутов: {stats['mutes']}\n\n"
        f"🏆 ТОП-5 АКТИВНЫХ\n"
        f"{format_top(vk, stats['top_users'], ('🥇', '🥈', '🥉', '4️⃣', '5️⃣'))}"
    )
    return send_bot_message(vk, report) > 0


def publish_king(vk, conn, stat_date: date, *, announce=True) -> bool:
    stats = db.get_daily_stats(conn, stat_date)
    if not stats["top_users"]:
        log.info("Герой не назначен: %s — нет сообщений", stat_date)
        return True

    user_id, messages = stats["top_users"][0]
    if db.get_king(conn, stat_date):
        return True

    if not announce:
        db.save_king(conn, stat_date, user_id, messages)
        return True

    names = get_user_names(vk, [user_id])
    text = (
        f"🦸 ГЕРОЙ ЧАТА\n\n"
        f"{mention(user_id, names.get(user_id))}\n"
        f"Сообщений за день: {messages}\n\n"
        f"Сегодня у Героя есть право выдать один мут через команду /мут @username."
    )
    sent = send_bot_message(vk, text)
    if sent <= 0:
        return False
    db.save_king(conn, stat_date, user_id, messages)
    log.info(
        "Герой чата: date=%s user=%s messages=%s",
        stat_date,
        user_id,
        messages,
    )
    return True


def current_week_key(local_date: date) -> str:
    return (local_date - timedelta(days=local_date.weekday())).isoformat()


def parse_target_user_id(text: str) -> int:
    match = re.search(r"(?<![\w@])(?:@id(\d+)(?![\w.])|@(\d+)(?![\w.])|\[id(\d+)\|[^\]]+\])", text)
    return int(next(group for group in match.groups() if group)) if match else 0

def resolve_target_user_id(vk, text: str) -> int:
    """Resolve a VK numeric mention or @screen_name to a user id."""
    target_id = parse_target_user_id(text)
    if target_id > 0:
        return target_id

    match = re.search(r"@([A-Za-z0-9_.]+)", text)
    if not match:
        return 0

    screen_name = match.group(1)
    try:
        profiles = vk.users.get(user_ids=screen_name)
    except ApiError as err:
        log.warning("Не удалось найти пользователя @%s: %s", screen_name, err)
        return 0

    if not profiles:
        return 0

    return int(profiles[0].get("id", 0) or 0)


def is_stat_allowed_user(vk, from_id: int) -> bool:
    """Check /stat access by resolving the configured VK screen names to IDs."""
    allowed_logins = tuple(
        login.strip().lstrip("@")
        for login in config.STAT_ALLOWED_LOGINS
        if login.strip()
    )
    if not allowed_logins:
        return False

    if config.STAT_ALLOWED_IDS:
        return from_id in config.STAT_ALLOWED_IDS
    numeric_ids = {int(login[2:]) for login in allowed_logins if re.fullmatch(r"id\d+", login)}
    if from_id in numeric_ids:
        return True
    cache_key = tuple(sorted(allowed_logins))
    if cache_key not in STAT_ACCESS_CACHE:
        # A lookup failure must leave the inbox item pending, not silently consume /stat.
        profiles = vk.users.get(user_ids=",".join(cache_key))
        allowed_ids = {int(profile.get("id", 0) or 0) for profile in profiles}
        if len(allowed_ids - {0}) < len(cache_key):
            raise RuntimeError("Не удалось разрешить все аккаунты /stat; задайте STAT_ALLOWED_IDS")
        STAT_ACCESS_CACHE[cache_key] = (allowed_ids | numeric_ids) - {0}
    return int(from_id) in STAT_ACCESS_CACHE[cache_key]


def handle_stat_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    # /stat — закрытая команда. Доступ только у трёх указанных аккаунтов.
    # Команда всегда показывает общую статистику чата 2000000002
    # за текущий день; статистика отдельных пользователей здесь не запрашивается.
    if not re.fullmatch(r"/stat", text.strip(), re.IGNORECASE):
        return False

    if not is_stat_allowed_user(vk, from_id):
        return True

    stat_date = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    publish_daily_stats(vk, conn, stat_date)
    return True

def handle_profile_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    match = re.match(r"^/profile(?:\s+(.+))?$", text.strip(), re.IGNORECASE)
    if not match:
        return False

    argument = match.group(1)
    target_id = resolve_target_user_id(vk, argument) if argument else from_id
    if target_id <= 0:
        send_bot_message(vk, "❗ Укажи существующего пользователя через @username или @id.", temporary=True)
        return True
    command_key = "profile"
    last_used = db.get_command_cooldown(conn, from_id, command_key)

    cooldown = config.PROFILE_COOLDOWN_SECONDS
    if last_used and now - last_used < cooldown:
        remaining = cooldown - (now - last_used)
        days = remaining // (24 * 60 * 60)
        hours = (remaining % (24 * 60 * 60) + 3599) // 3600
        if days > 0:
            remaining_text = f"{days} дн."
        else:
            remaining_text = f"{max(1, hours)} ч."
        send_bot_message(
            vk,
            f"⏳ Профиль можно посмотреть снова через {remaining_text}.",
            temporary=True,
        )
        return True

    db.set_command_cooldown(conn, from_id, command_key, 0, now)
    reputation = db.get_reputation(conn, target_id)
    names = get_user_names(vk, [target_id])
    profile = (
        f"👤 ПРОФИЛЬ\n\n"
        f"{mention(target_id, names.get(target_id))}\n"
        f"⭐ Репутация: {reputation:+d}"
    )
    send_bot_message(vk, profile)
    return True


def handle_reputation_command(
    vk,
    conn,
    from_id: int,
    text: str,
    now: int,
) -> bool:
    match = re.match(r"^/(?:rep|реп)\s+(?:([+-])\s+)?(.+)$", text.strip(), re.IGNORECASE)
    if not match:
        return False

    target_id = resolve_target_user_id(vk, match.group(2))
    if target_id <= 0:
        send_bot_message(vk, "❗ Укажи пользователя через @username или @id.", temporary=True)
        return True

    value = -1 if match.group(1) == "-" else 1
    week_key = current_week_key(
        datetime.fromtimestamp(now, config.CHAT_TZ).date()
    )
    # Повторная репутация в течение недели полностью игнорируется.
    if db.has_reputation_vote(conn, from_id, week_key):
        return True

    if not db.add_reputation_vote(
        conn,
        from_id,
        target_id,
        value,
        week_key,
        now,
    ):
        # Даже при гонке двух одинаковых событий команда остаётся тихой.
        return True

    sign = "+1" if value > 0 else "-1"
    action = "повысил" if value > 0 else "понизил"
    giver_names = get_user_names(vk, [from_id])
    target_names = get_user_names(vk, [target_id])
    send_bot_message(
        vk,
        f"⭐ {mention(from_id, giver_names.get(from_id))} {action} репутацию "
        f"{mention(target_id, target_names.get(target_id))} ({sign}).",
    )
    return True


def handle_king_mute_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    if not re.match(r"^/мут(?:\s+.+)?$", text.strip(), re.IGNORECASE):
        return False

    local_date = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    yesterday = local_date - timedelta(days=1)
    king = db.get_king(conn, yesterday)

    if not king or king[0] != from_id:
        send_bot_message(
            vk,
            "🦸 Эта команда доступна только Герою чата.",
            temporary=True,
        )
        return True

    target_id = resolve_target_user_id(vk, text)
    if target_id <= 0:
        send_bot_message(
            vk,
            "❗ Укажи пользователя через @username или @id.",
            temporary=True,
        )
        return True

    if target_id == from_id:
        send_bot_message(
            vk,
            "🦸 Себя мутить нельзя.",
            temporary=True,
        )
        return True

    if target_id in config.ADMIN_IDS:
        send_bot_message(
            vk,
            "🛡 Администратора Король замутить не может.",
            temporary=True,
        )
        return True

    if pending_king_mute(conn, from_id, local_date):
        send_bot_message(vk, "⏳ Твой мут уже ожидает подтверждения VK.", temporary=True)
        return True

    # Один успешный королевский мут в сутки. Используем уже существующую
    # таблицу command_cooldowns — схема БД не меняется.
    last_mute = db.get_command_cooldown(conn, from_id, "king_mute")
    if last_mute:
        last_date = datetime.fromtimestamp(last_mute, config.CHAT_TZ).date()
        if last_date == local_date:
            send_bot_message(
                vk,
                "🦸 Ты уже использовал свой мут сегодня. Следующий будет доступен завтра.",
                temporary=True,
            )
            return True

    if not apply_mute(vk, config.CHAT_PEER_ID, target_id, king_id=from_id, now=now):
        send_bot_message(vk, "🔇 Пользователь уже под ограничением. Твой мут не потрачен.", temporary=True)

    return True


def handle_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False

    if handle_stat_command(vk, conn, from_id, stripped, now):
        return True
    if handle_profile_command(vk, conn, from_id, stripped, now):
        return True
    if handle_reputation_command(vk, conn, from_id, stripped, now):
        return True
    if handle_king_mute_command(vk, conn, from_id, stripped, now):
        return True
    return False


def bot_message_cleanup_watchdog(vk, conn) -> None:
    while True:
        try:
            cleanup_tick(conn)
        except Exception:
            log.error("Ошибка автоудаления сообщений бота:\n%s", traceback.format_exc())
        time.sleep(5)


def cleanup_tick(conn, now=None):
    with db.atomic(conn):
        for message_id, peer_id in db.get_due_bot_messages(conn, now):
            db.enqueue(conn, "delete", {"params": {
                "message_ids": str(message_id), "delete_for_all": 1,
            }})
            db.remove_bot_message(conn, message_id)


def scheduler_watchdog(vk, conn) -> None:
    while True:
        try:
            scheduler_tick(vk, conn)
        except Exception:
            log.exception("Ошибка scheduler")
        time.sleep(config.SCHEDULER_INTERVAL_SECONDS)

def process_message(vk, conn, message) -> None:
    from_id = message_field(message, "from_id")
    peer_id = message_field(message, "peer_id")
    text = message_text(message)

    if from_id <= 0 or not is_target_chat(peer_id):
        return

    now = int(time.time())

    if event_field(message, "action") or message_field(message, "out"):
        return

    # Administrators retain moderation exemptions, but can use every feature command.
    if from_id in config.ADMIN_IDS:
        if is_bot_feature_chat(peer_id):
            if handle_command(vk, conn, from_id, text, now) or text.strip().startswith("/"):
                return
            db.record_message(conn, from_id, message_field(message, "date", now), classify_media(message))
        return

    if is_bot_feature_chat(peer_id) and re.search(r"(?<!\w)@all(?!\w)", text, re.IGNORECASE):
        deleted = delete_message(vk, message)
        muted = apply_mute(vk, peer_id, from_id, reason=config.ALL_COMMAND_MUTE_REASON, deletion_id=deleted)
        log.warning(
            "Запрещённая @all-команда: user=%s deleted=%s muted=%s",
            from_id,
            deleted,
            muted,
        )
        return

    if handle_content_violation(vk, conn, message, from_id, peer_id):
        return

    if is_bot_feature_chat(peer_id):
        if handle_command(vk, conn, from_id, text, now):
            return
        if text.strip().startswith("/"):
            return  # Unknown commands do not compete for the activity title.
        db.record_message(conn, from_id, message_field(message, "date", now), classify_media(message))
        return

    if db.mute_deadline(conn, peer_id, from_id) > now:
        delete_message(vk, message)
        return  # Never restart an existing one-hour restriction.

    result = db.register_message(conn, from_id, peer_id, now, defer_mute=True)

    if result["already_muted"]:
        delete_message(vk, message)
        return

    if not result["should_mute"]:
        return

    deleted = delete_message(vk, message)
    muted = apply_mute(vk, peer_id, from_id, reason=config.LIMIT_MUTE_REASON, deletion_id=deleted)

    log.warning(
        "Лимит: user=%s peer=%s deleted=%s muted=%s",
        from_id,
        peer_id,
        deleted,
        muted,
    )



def pending_king_mute(conn, user_id, local_date):
    with db._lock:
        for row in conn.execute("SELECT payload FROM outbox WHERE kind = 'mute' AND done_at = 0"):
            payload = json.loads(row["payload"])
            request_date = datetime.fromtimestamp(payload["requested_at"], config.CHAT_TZ).date()
            if payload.get("king_id") == user_id and request_date == local_date:
                return True
    return False


def handle_new_message(vk, conn, message) -> None:
    global ACTIVE_CONN
    ACTIVE_CONN = conn
    peer_id = message_field(message, "peer_id")
    if not is_target_chat(peer_id) or message_field(message, "from_id") <= 0:
        return
    message = dict(message)
    message_id = message_field(message, "id")
    cmid = message_field(message, "conversation_message_id")
    if message_id:
        identity = f"id:{message_id}"
    elif cmid:
        identity = f"cmid:{cmid}"
    else:
        # Missing IDs are anomalous: keep the event, but cannot guarantee deduplication.
        identity = f"missing:{uuid.uuid4().hex}"
        log.error("Сообщение без id/cmid: peer=%s", peer_id)
    key = f"message:{peer_id}:{identity}"
    db.store_inbox(conn, key, message)
    process_inbox(vk, conn)


def process_inbox(vk, conn):
    for row in db.due_inbox(conn):
        try:
            with db.atomic(conn):
                current = conn.execute("SELECT done_at FROM inbox WHERE event_key = ?", (row["event_key"],)).fetchone()
                if current["done_at"]:
                    continue
                message = json.loads(row["payload"])
                message_id = message_field(message, "id")
                peer_id = message_field(message, "peer_id")
                # Keep compatibility with pre-upgrade processed message records.
                if not message_id or db.claim_processed_message(conn, peer_id, message_id):
                    process_message(vk, conn, message)
                db.finish_inbox(conn, row["event_key"])
        except Exception:
            log.exception("Входящее событие остаётся в очереди: %s", row["event_key"])
            db.retry_inbox(conn, row["event_key"])


def execute_operation(vk, conn, row, now):
    payload = json.loads(row["payload"])
    if row["kind"] == "send":
        if payload.get("depends_on"):
            with db._lock:
                dependency = conn.execute("SELECT done_at FROM outbox WHERE id = ?", (payload["depends_on"],)).fetchone()
            if dependency and not dependency["done_at"]:
                return
        # Positive SQLite IDs are unique and persistent across restarts.
        message_id = int(vk.messages.send(peer_id=payload["peer_id"],
            random_id=row["id"], message=payload["message"]) or 0)
        if not message_id:
            raise RuntimeError("VK не вернул ID отправленного сообщения")
        with db.atomic(conn):
            if payload["temporary"]:
                db.queue_bot_message(conn, message_id, payload["peer_id"], now + payload["delay"])
            db.finish_operation(conn, row["id"], now)
    elif row["kind"] == "delete":
        result = vk.messages.delete(**payload["params"])
        if not result or (isinstance(result, dict) and any(not value for value in result.values())):
            raise RuntimeError("VK не подтвердил удаление сообщения")
        db.finish_operation(conn, row["id"], now)
    elif row["kind"] == "mute":
        remaining = payload["until"] - now
        requested_date = datetime.fromtimestamp(payload["requested_at"], config.CHAT_TZ).date()
        today = datetime.fromtimestamp(now, config.CHAT_TZ).date()
        if remaining <= 0 or (payload.get("king_id") and requested_date != today):
            with db.atomic(conn):
                log.error("Мут не доставлен до истечения срока: operation=%s", row["id"])
                if payload.get("king_id"):
                    send_bot_message(vk, "❗ VK не подтвердил мут. Право не было потрачено.", temporary=True)
                db.finish_operation(conn, row["id"], now)
            return
        result = vk.messages.changeConversationMemberRestrictions(peer_id=payload["peer_id"],
            member_ids=str(payload["user_id"]), action="ro", **{"for": remaining})
        if not result:
            raise RuntimeError("VK не подтвердил ограничение")
        with db.atomic(conn):
            db.confirm_mute(conn, payload["peer_id"], payload["user_id"], payload["until"])
            record_successful_mute(conn, payload["user_id"], payload["peer_id"], now)
            if payload.get("king_id"):
                db.set_command_cooldown(conn, payload["king_id"], "king_mute", 0, now)
                names = get_user_names(vk, [payload["user_id"]])
                send_bot_message(vk, config.KING_MUTE_MESSAGE.format(
                    mention=mention(payload["user_id"], names.get(payload["user_id"]))))
            elif payload.get("reason"):
                send_mute_reason(vk, payload["user_id"], payload["reason"], payload["peer_id"], payload.get("deletion_id"))
            db.finish_operation(conn, row["id"], now)
    else:
        raise RuntimeError(f"Неизвестная операция {row['kind']}")


DELIVERY_LOCK = threading.Lock()


def deliver_outbox(vk, conn, now=None):
    global ACTIVE_CONN
    ACTIVE_CONN = conn
    with DELIVERY_LOCK:
        for row in db.due_operations(conn, now):
            try:
                execute_operation(vk, conn, row, int(time.time()) if now is None else now)
            except Exception as error:
                label = f"{type(error).__name__}: code={getattr(error, 'code', 'n/a')}"
                log.warning("Действие VK будет повторено: operation=%s kind=%s error=%s",
                            row["id"], row["kind"], label)
                db.retry_operation(conn, row["id"], label, now)


def delivery_watchdog(vk, conn):
    last_prune = 0
    while True:
        try:
            process_inbox(vk, conn)
            deliver_outbox(vk, conn)
            now = int(time.time())
            if now - last_prune >= 3600:
                db.prune_operational_history(conn, now)
                last_prune = now
        except Exception:
            log.exception("Ошибка delivery watchdog")
        time.sleep(5)


def scheduler_tick(vk, conn, now=None):
    global ACTIVE_CONN
    ACTIVE_CONN = conn
    now = datetime.now(config.CHAT_TZ) if now is None else now.astimezone(config.CHAT_TZ)
    with db.atomic(conn):
        checkpoint = conn.execute("SELECT created_at FROM scheduler_state WHERE event_key = 'checkpoint'").fetchone()
        first_pass = checkpoint is None
        lower = (datetime.fromtimestamp(checkpoint["created_at"], config.CHAT_TZ) if checkpoint
                 else now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1))
        lower = max(lower, now - timedelta(days=config.SCHEDULER_CATCHUP_DAYS))
        cursor = lower.date()
        while cursor <= now.date():
            midnight = datetime.combine(cursor, datetime.min.time(), config.CHAT_TZ)
            if lower < midnight <= now:
                yesterday = cursor - timedelta(days=1)
                for key, publisher in ((f"king:{yesterday.isoformat()}", publish_king),
                                       (f"daily:{yesterday.isoformat()}", publish_daily_stats)):
                    if not db.scheduler_event_claimed(conn, key):
                        queued = (publisher(vk, conn, yesterday, announce=(cursor == now.date()))
                                  if publisher is publish_king else publisher(vk, conn, yesterday))
                        if queued:
                            db.claim_scheduler_event(conn, key, int(now.timestamp()))
            hours = [hour for hour in (6, 12, 18) if lower < midnight.replace(hour=hour) <= now]
            # A missed intraday snapshot cannot be reconstructed from daily totals.
            # Publish only the latest due snapshot for today's date.
            if cursor == now.date() and hours:
                if first_pass:
                    hours = hours[-1:]
                for hour in hours:
                    key = f"daily:{cursor.isoformat()}-{hour:02d}"
                    if not db.scheduler_event_claimed(conn, key):
                        if publish_daily_stats(vk, conn, cursor):
                            db.claim_scheduler_event(conn, key, int(now.timestamp()))
            monday_noon = midnight.replace(hour=12)
            if cursor.weekday() == 0 and lower < monday_noon <= now:
                start_date = cursor - timedelta(days=7)
                key = f"weekly:{start_date.isoformat()}"
                if not db.scheduler_event_claimed(conn, key):
                    if publish_weekly_stats(vk, conn, start_date, cursor):
                        db.claim_scheduler_event(conn, key, int(now.timestamp()))
            cursor += timedelta(days=1)
        conn.execute("INSERT INTO scheduler_state(event_key, created_at) VALUES ('checkpoint', ?) "
                     "ON CONFLICT(event_key) DO UPDATE SET created_at = excluded.created_at", (int(now.timestamp()),))


def acquire_instance_lock():
    """OS releases this lock automatically on crash; prevents two local bot processes."""
    handle = open(config.BASE_DIR / ".bot.lock", "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("Другой экземпляр бота уже запущен в этой папке.")
    return handle

def run_forever() -> None:
    global ACTIVE_CONN
    instance_lock = acquire_instance_lock()
    conn = db.connect()
    ACTIVE_CONN = conn

    log.info("База готова: %s", config.DB_PATH)
    log.info(
        "Бот запущен: feature_chat=%s, moderation_chat=%s, timezone=%s",
        config.CHAT_PEER_ID,
        config.MODERATION_CHAT_PEER_ID,
        config.CHAT_TIMEZONE,
    )

    session = vk_api.VkApi(
        token=config.VK_TOKEN,
        api_version=config.API_VERSION,
        session=TimeoutSession(),
    )
    vk = session.get_api()

    if (
        config.BLOCKED_USER_IDS
        and config.BLOCKLIST_CHAT_PEER_ID > 0
        and config.BLOCKLIST_CHAT_PEER_ID != config.CHAT_PEER_ID
    ):
        threading.Thread(
            target=blocklist_watchdog,
            args=(vk,),
            name="blocklist-watchdog",
            daemon=True,
        ).start()

    threading.Thread(
        target=bot_message_cleanup_watchdog,
        args=(vk, conn),
        name="bot-message-cleanup",
        daemon=True,
    ).start()

    threading.Thread(
        target=scheduler_watchdog,
        args=(vk, conn),
        name="scheduler-watchdog",
        daemon=True,
    ).start()

    threading.Thread(target=delivery_watchdog, args=(vk, conn), name="delivery-watchdog", daemon=True).start()

    longpoll = None
    while True:
        try:
            if longpoll is None:
                longpoll = VkBotLongPoll(session, config.GROUP_ID)

            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    obj = event_object(event)
                    nested = (
                        event_field(obj, "message")
                        if obj is not None
                        else None
                    )
                    message = nested if nested is not None else obj

                    if message is not None:
                        handle_new_message(vk, conn, message)

                elif str(getattr(event, "type", "")) == "chat_update":
                    handle_chat_update(vk, event)

        except (KeyboardInterrupt, SystemExit):
            raise
        except (VkApiError, OSError, ConnectionError) as err:
            log.error(
                "Сбой Long Poll/API: %s. Повтор через %s сек.",
                err,
                config.RECONNECT_DELAY,
            )
            time.sleep(config.RECONNECT_DELAY)
        except Exception:
            log.error("Неожиданная ошибка:\n%s", traceback.format_exc())
            time.sleep(config.RECONNECT_DELAY)


ACTIVE_CONN = None
STAT_ACCESS_CACHE = {}


if __name__ == "__main__":
    run_forever()
