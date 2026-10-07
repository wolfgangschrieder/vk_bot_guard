import logging
import re
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.exceptions import ApiError, VkApiError

import config
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("vk_bot_guard")

CHAT_PEER_START = 2_000_000_000


def is_target_chat(peer_id: int) -> bool:
    return peer_id in {
        config.CHAT_PEER_ID,
        config.MODERATION_CHAT_PEER_ID,
    }


def is_bot_feature_chat(peer_id: int) -> bool:
    return peer_id == config.CHAT_PEER_ID


def is_moderation_chat(peer_id: int) -> bool:
    return peer_id == config.MODERATION_CHAT_PEER_ID


def delete_message(vk, message) -> bool:
    """Delete a message from the conversation for everyone."""
    message_id = message_field(message, "id")
    conversation_message_id = message_field(message, "conversation_message_id")
    peer_id = message_field(message, "peer_id")

    try:
        if message_id > 0:
            vk.messages.delete(
                message_ids=str(message_id),
                delete_for_all=1,
            )
            log.info(
                "Сообщение удалено: message_id=%s peer_id=%s",
                message_id,
                peer_id,
            )
            return True

        if conversation_message_id > 0 and peer_id > 0:
            vk.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=str(conversation_message_id),
                delete_for_all=1,
            )
            log.info(
                "Сообщение удалено: conversation_message_id=%s peer_id=%s",
                conversation_message_id,
                peer_id,
            )
            return True

        return False
    except ApiError as err:
        log.error(
            "Не удалось удалить сообщение id=%s conversation_message_id=%s peer_id=%s: %s",
            message_id,
            conversation_message_id,
            peer_id,
            err,
        )
        return False


def delete_bot_message(vk, message_id: int, peer_id: int) -> bool:
    try:
        vk.messages.delete(
            message_ids=str(message_id),
            delete_for_all=1,
        )
        log.info(
            "Сообщение бота удалено: message_id=%s peer_id=%s",
            message_id,
            peer_id,
        )
        return True
    except ApiError as err:
        log.warning(
            "Не удалось удалить сообщение бота message_id=%s peer_id=%s: %s",
            message_id,
            peer_id,
            err,
        )
        return False


def apply_mute(vk, peer_id: int, user_id: int) -> bool:
    try:
        vk.messages.changeConversationMemberRestrictions(
            peer_id=peer_id,
            member_ids=str(user_id),
            action="ro",
            **{"for": config.MUTE_SECONDS},
        )
        return True
    except ApiError as err:
        log.error(
            "Не удалось выдать мут user=%s peer=%s: %s",
            user_id,
            peer_id,
            err,
        )
        return False


def lift_mute(vk, peer_id: int, user_id: int) -> None:
    try:
        vk.messages.changeConversationMemberRestrictions(
            peer_id=peer_id,
            member_ids=str(user_id),
            action="rw",
        )
        log.info("Мут снят: user=%s peer=%s", user_id, peer_id)
    except ApiError as err:
        log.warning(
            "Не удалось снять мут user=%s peer=%s: %s",
            user_id,
            peer_id,
            err,
        )


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
    return any(term in normalized for term in config.PROHIBITED_LEXICON) or any(
        re.search(pattern, normalized) for pattern in config.PRICE_PATTERNS
    )


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
    vk,
    message: str,
    *,
    temporary: bool = False,
    peer_id: int | None = None,
    delete_after_seconds: int | None = None,
) -> int:
    target_peer = peer_id or config.CHAT_PEER_ID
    try:
        message_id = int(
            vk.messages.send(
                peer_id=target_peer,
                random_id=0,
                message=message,
            )
            or 0
        )
        if temporary and message_id > 0:
            delay = (
                config.BOT_REASON_DELETE_SECONDS
                if delete_after_seconds is None
                else max(1, int(delete_after_seconds))
            )
            db.queue_bot_message(
                ACTIVE_CONN,
                message_id,
                target_peer,
                int(time.time()) + delay,
            )
        return message_id
    except ApiError as err:
        log.warning("Не удалось отправить сообщение бота: %s", err)
        return 0


def send_mute_reason(vk, user_id: int, reason: str, peer_id: int) -> None:
    send_bot_message(
        vk,
        f"[id{user_id}|Пользователь], {reason}",
        temporary=True,
        peer_id=peer_id,
    )


def record_successful_mute(conn, user_id: int) -> None:
    db.record_mute(conn, user_id)


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
    muted = apply_mute(vk, peer_id, from_id)
    if muted:
        record_successful_mute(conn, from_id)
        send_mute_reason(vk, from_id, reason, peer_id)

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


def publish_daily_stats(vk, conn, stat_date: date) -> None:
    stats = db.get_daily_stats(conn, stat_date)
    top3 = stats["top_users"][:3]
    month = config.MONTH_NAMES[stat_date.month - 1]

    report = (
        f"📊 СТАТИСТИКА ЧАТА\n"
        f"#{stat_date.day}{month}\n\n"
        f"💬 Сообщений сегодня: {stats['messages']}\n"
        f"📷 Фото: {stats['photos']}\n"
        f"🎬 Видео: {stats['videos']}\n"
        f"🎵 Музыка: {stats['music']}\n"
        f"🎙 Голосовые: {stats['voices']}\n\n"
        f"🏆 ТОП-3 АКТИВНЫХ\n"
        f"{format_top(vk, top3, ('🥇', '🥈', '🥉'))}"
    )
    send_bot_message(vk, report)


def publish_weekly_stats(vk, conn, start_date: date, end_date: date) -> None:
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
    send_bot_message(vk, report)


def publish_king(vk, conn, stat_date: date) -> None:
    stats = db.get_daily_stats(conn, stat_date)
    if not stats["top_users"]:
        log.info("Герой не назначен: %s — нет сообщений", stat_date)
        return

    user_id, messages = stats["top_users"][0]
    if not db.save_king(conn, stat_date, user_id, messages):
        return

    names = get_user_names(vk, [user_id])
    text = (
        f"🦸 ГЕРОЙ ЧАТА\n\n"
        f"{mention(user_id, names.get(user_id))}\n"
        f"Сообщений за день: {messages}\n\n"
        f"Сегодня у Героя есть право выдать один мут через команду /мут @username."
    )
    send_bot_message(vk, text)
    log.info(
        "Герой чата: date=%s user=%s messages=%s",
        stat_date,
        user_id,
        messages,
    )


def current_week_key(local_date: date) -> str:
    return (local_date - timedelta(days=local_date.weekday())).isoformat()


def parse_target_user_id(text: str) -> int:
    match = re.search(r"(?:@id|@|\[id)(\d+)", text)
    return int(match.group(1)) if match else 0


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


def handle_profile_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    match = re.match(r"^/profile(?:\s+(.+))?$", text.strip(), re.IGNORECASE)
    if not match:
        return False

    target_id = parse_target_user_id(match.group(1) or "") or from_id
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
    match = re.match(r"^/(?:rep|реп)\s+([+-])\s+(.+)$", text.strip(), re.IGNORECASE)
    if not match:
        return False

    target_id = resolve_target_user_id(vk, match.group(2))
    if target_id <= 0:
        send_bot_message(vk, "❗ Укажи пользователя через @username или @id.", temporary=True)
        return True

    value = 1 if match.group(1) == "+" else -1
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

    if apply_mute(vk, config.CHAT_PEER_ID, target_id):
        record_successful_mute(conn, target_id)
        db.set_command_cooldown(conn, from_id, "king_mute", 0, now)
        names = get_user_names(vk, [target_id])
        send_bot_message(
            vk,
            config.KING_MUTE_MESSAGE.format(
                mention=mention(target_id, names.get(target_id))
            ),
        )
    return True


def handle_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False

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
            due = db.get_due_bot_messages(conn)
            for message_id, peer_id in due:
                delete_bot_message(vk, message_id, peer_id)
                db.remove_bot_message(conn, message_id)
        except Exception:
            log.error("Ошибка автоудаления сообщений бота:\n%s", traceback.format_exc())
        time.sleep(20)


def scheduler_watchdog(vk, conn) -> None:
    """Publish daily/weekly reports and elect the daily king at fixed local times."""
    while True:
        try:
            now = datetime.now(config.CHAT_TZ)
            local_date = now.date()

            # 00:00: finish yesterday, publish its stats, and appoint today's king.
            if now.minute == 0 and now.hour == 0:
                stat_date = local_date - timedelta(days=1)
                king_key = f"king:{stat_date.isoformat()}"
                if db.claim_scheduler_event(conn, king_key):
                    publish_king(vk, conn, stat_date)

                daily_key = f"daily:{stat_date.isoformat()}"
                if db.claim_scheduler_event(conn, daily_key):
                    publish_daily_stats(vk, conn, stat_date)

            if now.minute == 0 and now.hour in {6, 12, 18}:
                key = f"daily:{now.strftime('%Y-%m-%d-%H')}"
                if db.claim_scheduler_event(conn, key):
                    publish_daily_stats(vk, conn, local_date)

            # Monday 12:00 — previous Monday-Sunday.
            if now.weekday() == 0 and now.hour == 12 and now.minute == 0:
                end_date = local_date
                start_date = end_date - timedelta(days=7)
                key = f"weekly:{start_date.isoformat()}"
                if db.claim_scheduler_event(conn, key):
                    publish_weekly_stats(vk, conn, start_date, end_date)

        except Exception:
            log.error("Ошибка scheduler:\n%s", traceback.format_exc())

        time.sleep(config.SCHEDULER_INTERVAL_SECONDS)


def handle_new_message(vk, conn, message) -> None:
    from_id = message_field(message, "from_id")
    peer_id = message_field(message, "peer_id")
    text = message_text(message)

    if from_id <= 0 or not is_target_chat(peer_id):
        return

    message_id = message_field(message, "id")
    if message_id > 0 and not db.claim_processed_message(conn, peer_id, message_id):
        log.warning(
            "Повторная доставка сообщения проигнорирована: peer_id=%s message_id=%s",
            peer_id,
            message_id,
        )
        return

    now = int(time.time())

    # Statistics and bot commands belong only to the feature chat (2000000002).
    if is_bot_feature_chat(peer_id):
        db.record_message(
            conn,
            from_id,
            message_field(message, "date", now) or now,
            classify_media(message),
        )

    if from_id in config.ADMIN_IDS:
        return

    if is_bot_feature_chat(peer_id) and handle_command(vk, conn, from_id, text, now):
        return

    if is_bot_feature_chat(peer_id) and re.search(r"(?<!\w)@all(?!\w)", text, re.IGNORECASE):
        deleted = delete_message(vk, message)
        muted = apply_mute(vk, peer_id, from_id)
        if muted:
            record_successful_mute(conn, from_id)
            send_bot_message(
                vk,
                config.ALL_COMMAND_MUTE_REASON,
                temporary=True,
                peer_id=peer_id,
                delete_after_seconds=config.ALL_COMMAND_DELETE_SECONDS,
            )
        log.warning(
            "Запрещённая @all-команда: user=%s deleted=%s muted=%s",
            from_id,
            deleted,
            muted,
        )
        return

    if handle_content_violation(vk, conn, message, from_id, peer_id):
        return

    # The one-message-per-hour rule exists only in the strict moderation chat.
    if not is_moderation_chat(peer_id):
        return

    for expired_user, expired_peer in db.clear_expired_mutes(conn, now):
        lift_mute(vk, expired_peer, expired_user)

    result = db.register_message(conn, from_id, peer_id, now)

    if result["already_muted"]:
        delete_message(vk, message)
        apply_mute(vk, peer_id, from_id)
        return

    if not result["should_mute"]:
        return

    deleted = delete_message(vk, message)
    muted = apply_mute(vk, peer_id, from_id)

    if muted:
        record_successful_mute(conn, from_id)
        send_mute_reason(vk, from_id, config.LIMIT_MUTE_REASON, peer_id)

    log.warning(
        "Лимит: user=%s peer=%s deleted=%s muted=%s",
        from_id,
        peer_id,
        deleted,
        muted,
    )


def run_forever() -> None:
    global ACTIVE_CONN
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

    while True:
        try:
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


if __name__ == "__main__":
    run_forever()
