import logging
import json
import hashlib
import random
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
import morning_content
import chat_stats_export

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
CHAT_NOMINATION_TITLE = "👑 Народная номинация"
DUEL_TITLE = "🥴 Неудачник"
USER_GUIDE_URL = "https://vk.ru/topic-234090559_68552562?offset=0"
COMMANDS_URL = "https://vk.ru/topic-234090559_68552562"
DONATE_GOAL_URL = (
    "https://vk.ru/vlgchat?w=app51528700_38291769%3FisCloseForwardPush%3Dtrue"
    "%23%3Fowner_id%3D-234090559%26type%3Dgoal%26params%3D%7B%2522goal_id%2522"
    "%3A16677%7D%26in_modal%3Dtrue"
)
DONUT_SUBSCRIPTION_URL = (
    "https://vk.ru/vlgchat?analytics_screen=group&levelId=2482&"
    "source=donut_banner&w=donut_payment-234090559"
)
DONUT_EVENT_TYPES = {
    "donut_subscription_create",
    "donut_subscription_prolonged",
    "donut_subscription_cancelled",
    "donut_subscription_expired",
}
HERO_NAMES = {
    "day": "Герой дня",
    "week": "Герой недели",
    "month": "Герой месяца",
    "year": "Герой года",
}
HERO_EMOJIS = {"day": "🏆", "week": "👑", "month": "💎", "year": "🌟"}
HERO_MUTE_SECONDS = {"day": 3600, "week": 4 * 3600, "month": 8 * 3600, "year": 24 * 3600}
HERO_MUTE_LABELS = {"day": "1 час", "week": "4 часа", "month": "8 часов", "year": "24 часа"}
HERO_MUTE_PRIORITY = {"day": 1, "week": 2, "month": 3, "year": 4}


def menu_keyboard() -> dict:
    return {
        "inline": True,
        "buttons": [[
            {
                "action": {"type": "text", "label": "👤 Мой профиль"},
                "color": "primary",
            },
            {
                "action": {
                    "type": "open_link",
                    "link": USER_GUIDE_URL,
                    "label": "📖 Руководство",
                },
            },
        ]],
    }


def donut_subscription_keyboard() -> dict:
    return {
        "inline": True,
        "buttons": [[{
            "action": {
                "type": "open_link",
                "link": DONUT_SUBSCRIPTION_URL,
                "label": "💎 Оформить Don-подписку",
            },
        }]],
    }


def chat_donation_keyboard() -> dict:
    return {
        "inline": True,
        "buttons": [[{
            "action": {
                "type": "open_link",
                "link": DONATE_GOAL_URL,
                "label": "💛 Поддержать чат донатом",
            },
        }]],
    }


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        # vk_api may pass timeout=None explicitly. setdefault() would leave
        # that unbounded, allowing a stalled API call to hold an inbox event
        # open forever.
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 10
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


def handle_donut_event(conn, event) -> bool:
    """Process a Donut update received through the community Long Poll."""
    raw = getattr(event, "raw", event)
    if not isinstance(raw, dict) or raw.get("type") not in DONUT_EVENT_TYPES:
        return False
    try:
        group_id = int(raw.get("group_id", 0))
    except (TypeError, ValueError):
        group_id = 0
    if group_id != config.GROUP_ID:
        log.error("Пропущено Donut-событие от неизвестного сообщества: group_id=%s", group_id)
        return True

    result = db.process_donut_event(conn, raw, config.PREMIUM_DONUT_MIN_AMOUNT_RUB)
    user_id = (raw.get("object") or {}).get("user_id")
    if result in {"invalid", "below_minimum"}:
        log.warning("Donut-событие не выдало премиум: event=%s result=%s",
                    raw.get("event_id", ""), result)
    elif result == "activated":
        log.info("Donut-премиум активирован: user=%s", user_id)
    elif result == "expired":
        log.info("Donut-премиум завершён: user=%s", user_id)
    elif result == "cancelled":
        log.info("Donut-подписка отменена; премиум сохранён до окончания срока: user=%s", user_id)
    return True


def delete_message(vk, message) -> int:
    message_id = message_field(message, "id")
    cmid = message_field(message, "conversation_message_id")
    peer_id = message_field(message, "peer_id")
    # In group conversations, prefer the conversation-scoped ID and include
    # the peer. A global message ID without its destination is ambiguous for
    # community tokens and may be rejected by VK for messages authored by
    # other participants.
    if cmid > 0 and peer_id >= CHAT_PEER_START:
        params = {"peer_id": peer_id, "cmids": str(cmid), "delete_for_all": 1}
    elif message_id > 0:
        params = {"peer_id": peer_id, "message_ids": str(message_id), "delete_for_all": 1}
    else:
        log.error("Нельзя удалить сообщение без id/cmid: peer=%s", peer_id)
        return 0
    return db.enqueue(ACTIVE_CONN, "delete", {"params": params, "peer_id": peer_id})

def apply_mute(
    vk,
    peer_id: int,
    user_id: int,
    *,
    reason=None,
    king_id=None,
    now=None,
    deletion_id=None,
    duration_seconds=None,
    hero_period_type=None,
    hero_period_key=None,
    hero_cooldown_key=None,
    hero_notice=None,
    hourly_segments=1,
    rank_user_id=None,
    rank_title=None,
    rank_cooldown_key=None,
    allow_extend=False,
) -> bool:
    now = int(time.time()) if now is None else int(now)
    if db.mute_deadline(ACTIVE_CONN, peer_id, user_id) > now and not allow_extend:
        return False
    segments = max(1, int(hourly_segments))
    duration = max(1, int(duration_seconds or config.MUTE_SECONDS))
    previous_id = None
    for index in range(segments):
        starts_at = now + index * 60 * 60 if segments > 1 else now
        ends_at = starts_at + 60 * 60 if segments > 1 else now + duration
        payload = {
            "peer_id": peer_id,
            "user_id": user_id,
            "until": ends_at,
            "reason": reason,
            "king_id": king_id,
            "requested_at": starts_at,
            "deletion_id": deletion_id,
        }
        if hero_period_type:
            payload.update({
                "hero_period_type": hero_period_type,
                "hero_period_key": hero_period_key,
                "hero_cooldown_key": hero_cooldown_key,
                "hero_notice": (index == 0) if hero_notice is None else bool(hero_notice and index == 0),
                "segment_index": index,
                "previous_segment_id": previous_id,
            })
        if rank_user_id is not None:
            payload.update({
                "rank_user_id": int(rank_user_id),
                "rank_title": rank_title,
                "rank_cooldown_key": rank_cooldown_key,
                "rank_notice": index == 0,
                "segment_index": index,
                "previous_segment_id": previous_id,
            })
        operation_id = db.enqueue(ACTIVE_CONN, "mute", payload, next_attempt=starts_at)
        previous_id = operation_id
    return True

def remove_user_from_chat(vk, peer_id: int, user_id: int) -> bool:
    chat_id = peer_id - CHAT_PEER_START
    if chat_id <= 0:
        return False

    try:
        response = vk.messages.removeChatUser(chat_id=chat_id, user_id=user_id)
        if response not in (1, True):
            log.error(
                "VK не подтвердил удаление пользователя user=%s peer=%s response=%r",
                user_id,
                peer_id,
                response,
            )
            return False
        log.warning(
            "Заблокированный пользователь удалён из беседы: user=%s peer=%s",
            user_id,
            peer_id,
        )
        return True
    except Exception as err:
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


def handle_chat_update(vk, event, conn=None) -> None:
    """Enforce the social-chat blacklist and the optional legacy blocklist chat."""
    obj = event_object(event)
    if not obj:
        return

    peer_id = message_field(obj, "peer_id")
    if peer_id <= 0:
        chat_id = message_field(obj, "chat_id")
        peer_id = CHAT_PEER_START + chat_id if chat_id > 0 else 0

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

    if peer_id == config.CHAT_PEER_ID:
        if conn is not None and db.is_chat_blacklisted(conn, member_id):
            remove_user_from_chat(vk, peer_id, member_id)
        return

    if peer_id == config.BLOCKLIST_CHAT_PEER_ID and member_id in config.BLOCKED_USER_IDS:
        remove_user_from_chat(vk, peer_id, member_id)


def scan_blocklist_chat(vk) -> None:
    if not config.BLOCKED_USER_IDS:
        return

    try:
        present_ids = set()
        blocked_ids = sorted(config.BLOCKED_USER_IDS)
        for start in range(0, len(blocked_ids), 1000):
            batch = blocked_ids[start:start + 1000]
            response = vk.messages.getConversationMembers(
                peer_id=config.BLOCKLIST_CHAT_PEER_ID,
                member_ids=",".join(map(str, batch)),
                count=len(batch),
            )
            members = response.get("items", []) if isinstance(response, dict) else []
            present_ids.update(
                int(member.get("member_id", 0))
                for member in members
                if isinstance(member, dict) and int(member.get("member_id", 0) or 0) > 0
            )
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


def scan_chat_blacklist(vk, conn) -> None:
    blocked_ids = set(db.get_chat_blacklist(conn))
    if not blocked_ids:
        return

    try:
        present_ids = set()
        blocked_list = sorted(blocked_ids)
        for start in range(0, len(blocked_list), 1000):
            batch = blocked_list[start:start + 1000]
            response = vk.messages.getConversationMembers(
                peer_id=config.CHAT_PEER_ID,
                member_ids=",".join(map(str, batch)),
                count=len(batch),
            )
            members = response.get("items", []) if isinstance(response, dict) else []
            present_ids.update({
                int(member.get("member_id", 0))
                for member in members
                if isinstance(member, dict) and int(member.get("member_id", 0) or 0) > 0
            })
        for user_id in present_ids & blocked_ids:
            remove_user_from_chat(vk, config.CHAT_PEER_ID, user_id)
    except Exception:
        log.error("Ошибка проверки чёрного списка социальной беседы:\n%s", traceback.format_exc())


def chat_blacklist_watchdog(vk, conn) -> None:
    while True:
        try:
            scan_chat_blacklist(vk, conn)
        except Exception:
            log.exception("Ошибка проверки чёрного списка")
        time.sleep(60)


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

def has_audio_recording(message) -> bool:
    """Return true for VK music/audio-track attachments, excluding voice messages."""
    for attachment in message_attachments(message):
        attachment_type = str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower()
        if attachment_type == "audio":
            return True
    return False


def has_non_text_content(message) -> bool:
    """Return true for attachments or geolocation in a message or its forwards."""
    for item in moderated_contents(message):
        if message_attachments(item) or event_field(item, "geo"):
            return True
    return False


def is_night_mode_active(now=None) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    local = datetime.fromtimestamp(timestamp, config.CHAT_TZ)
    return 0 <= local.hour < config.MORNING_POST_HOUR


def has_sticker_attachment(message) -> bool:
    return any(
        str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower() == "sticker"
        for attachment in message_attachments(message)
    )


def sticker_fingerprint(message) -> str | None:
    stickers = []
    for attachment in message_attachments(message):
        attachment_type = str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower()
        if attachment_type != "sticker":
            continue
        sticker = (
            attachment.get("sticker", {})
            if isinstance(attachment, dict)
            else getattr(attachment, "sticker", {})
        )
        if not sticker:
            continue
        if isinstance(sticker, dict):
            sticker_id = sticker.get("sticker_id", sticker.get("id"))
            product_id = sticker.get("product_id", "")
            if sticker_id is not None:
                stickers.append(f"{product_id}:{sticker_id}")
                continue
            stickers.append(json.dumps(sticker, ensure_ascii=False, sort_keys=True, default=str))
        else:
            stickers.append(str(sticker))
    return json.dumps(sorted(stickers), ensure_ascii=False) if stickers else None


def repeated_content_fingerprint(message):
    sticker_key = sticker_fingerprint(message)
    if sticker_key:
        return f"sticker:{sticker_key}"
    text = normalized_text(message_text(message))
    return f"text:{text}" if text else None


def is_spam_message(conn, message, user_id: int, peer_id: int, now: int) -> bool:
    return db.track_spam_streak(
        conn, peer_id, user_id, repeated_content_fingerprint(message), now
    ) >= 4


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def contains_any_prohibited_term(text: str) -> bool:
    normalized = normalized_text(text)
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized)
        for term in config.PROHIBITED_LEXICON
    ) or any(re.search(pattern, normalized) for pattern in config.PRICE_PATTERNS)


def has_link(message) -> bool:
    text = message_text(message)
    # Accept explicit URLs, www links, and bare domain names. Avoid matching
    # email domains by requiring the match not to start after an @.
    if re.search(
        r"(?<![\w@])(?:https?://|www\.)[^\s<>]+|"
        r"(?<![\w@])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
        r"(?:/[^\s<>]*)?",
        text,
        re.IGNORECASE,
    ):
        return True

    for attachment in message_attachments(message):
        attachment_type = str(
            attachment.get("type", "")
            if isinstance(attachment, dict)
            else getattr(attachment, "type", "")
        ).lower()
        if attachment_type != "link":
            continue
        link = attachment.get("link", {}) if isinstance(attachment, dict) else getattr(attachment, "link", {})
        url = link.get("url", "") if isinstance(link, dict) else getattr(link, "url", "")
        if url:
            return True
    return False

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
    keyboard: dict | None = None,
) -> int:
    delay = (config.BOT_REASON_DELETE_SECONDS if delete_after_seconds is None
             else max(1, int(delete_after_seconds)))
    # This returns a durable delivery ID, not VK's message ID. VK delivery is retried.
    return db.enqueue(ACTIVE_CONN, "send", {
        "peer_id": peer_id or config.CHAT_PEER_ID, "message": message,
        "temporary": temporary, "delay": delay,
        "depends_on": depends_on,
        "keyboard": keyboard,
    })


def publish_commands_link(vk, peer_id: int | None = None) -> int:
    return send_bot_message(
        vk,
        f"📖 Список команд бота: [{COMMANDS_URL}|команды]",
        peer_id=peer_id,
    )


def publish_donut_promo(vk, peer_id: int | None = None) -> int:
    message = (
        "💎 Хочешь больше возможностей в @vlgchat? Поддержи чат подпиской Don-премиум!\\n\\n"
        "✨ Тебе откроются расширенные возможности:\\n"
        "• 👤 Профиль — 5 просмотров своего и 5 чужих профилей в день.\\n"
        "• 🎭 Характеристики — до 5 разных участников в день.\\n"
        "• ⭐ Репутация — до 3 разных участников в день.\\n"
        "• 💍 Свадьба — можешь предложить её любому участнику; принять приглашение может каждый.\\n"
        "• 💔 Развод — расторгнуть свой брак без согласия партнёра.\\n\\n"
        "Подписка — 100 ₽ в месяц. Твоя поддержка помогает развивать чат и добавлять новые функции. "
        "Нажми кнопку ниже, чтобы оформить Don-подписку. Спасибо, что ты с нами!"
    )
    return send_bot_message(vk, message, peer_id=peer_id, keyboard=donut_subscription_keyboard())


def publish_chat_ad(vk, peer_id: int | None = None) -> int:
    message = (
        "🌟 Добро пожаловать в @vlgchat!\\n\\n"
        "Здесь знакомятся, общаются и вместе создают истории чата. Профили, рейтинги, "
        "характеристики, Герои дня и другие механики помогают сделать общение живее.\\n\\n"
        "Мы развиваем проект для всего сообщества. Если тебе нравится чат и хочется помочь ему расти — "
        "поддержи @vlgchat донатом 💛\\n\\n"
        "Спасибо каждому, кто делает это место живым!"
    )
    return send_bot_message(vk, message, peer_id=peer_id, keyboard=chat_donation_keyboard())


def handle_donut_promo_command(vk, from_id: int, text: str,
                               command_message=None) -> bool:
    if not re.fullmatch(r"/реклама", text.strip(), re.IGNORECASE):
        return False
    if from_id not in config.ADMIN_IDS:
        if command_message is not None:
            delete_message(vk, command_message)
        return True
    peer_id = message_field(command_message, "peer_id", config.CHAT_PEER_ID)
    publish_chat_ad(vk, peer_id=peer_id)
    return True


def publish_project_news(vk, news_text: str) -> int:
    return send_bot_message(vk, f"📰 Новости проекта\n\n{news_text}")


def handle_commands_link_command(vk, conn, from_id: int, text: str, now: int,
                                 command_message=None) -> bool:
    if not re.fullmatch(r"/команды", text.strip(), re.IGNORECASE):
        return False
    if from_id not in config.ADMIN_IDS:
        is_premium = db.is_premium_user(conn, from_id, now)
        day_key = datetime.fromtimestamp(int(now), config.CHAT_TZ).date().isoformat()
        allowed = is_premium and db.try_consume_daily_command(
            conn, from_id, "commands_link", day_key, 3
        )
        if not allowed:
            if command_message is not None:
                delete_message(vk, command_message)
            return True
    peer_id = message_field(command_message, "peer_id", config.CHAT_PEER_ID)
    publish_commands_link(vk, peer_id=peer_id)
    return True

def send_mute_reason(vk, user_id: int, reason: str, peer_id: int, deletion_id=None) -> None:
    delete_after = (
        config.MODERATION_REASON_DELETE_SECONDS
        if is_moderation_chat(peer_id)
        else config.SOCIAL_MUTE_NOTICE_DELETE_SECONDS
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
    contents = list(moderated_contents(message))
    texts = [message_text(item) for item in contents]

    if is_moderation_chat(peer_id):
        if any(contains_porn_sale_term(text) for text in texts):
            reason = config.PORN_SALE_MUTE_REASON
        elif any(has_sticker_attachment(item) for item in contents):
            reason = config.STICKER_MUTE_REASON
        elif any(has_media_attachment(item) for item in contents):
            reason = config.MEDIA_MUTE_REASON
        elif any(has_link(item) for item in contents):
            reason = config.LINK_MUTE_REASON
        elif any(contains_any_prohibited_term(text) for text in texts):
            reason = config.PROHIBITED_WORDS_MUTE_REASON
        else:
            return False
    elif is_bot_feature_chat(peer_id):
        # В 2000000002 слова из запрещённой лексики не являются
        # основанием для мута. Здесь модерируем только карты/телефоны.
        if any(contains_card_or_phone(text) for text in texts):
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


def moderated_contents(message):
    """Inspect explicit forwards; replying to a message does not republish it."""
    pending = [message]
    while pending:
        item = pending.pop()
        yield item
        forwards = event_field(item, 'fwd_messages', []) or []
        pending.extend(child for child in forwards if isinstance(child, dict))


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


def publish_daily_stats(vk, conn, stat_date: date, *, include_records=False,
                        include_night_mode_notice=False) -> bool:
    stats = db.get_daily_stats(conn, stat_date)
    top3 = stats["top_users"][:3]
    month = config.MONTH_NAMES[stat_date.month - 1]
    activity = (
        f"{stats['activity_percent']}% от среднего за 7 дней"
        if stats["activity_percent"] is not None
        else "нет базы для сравнения за 7 дней"
    )

    report = (
        f"📊 СТАТИСТИКА ЧАТА\n"
        f"#{stat_date.day}{month}\n\n"
        f"💬 Сообщений за день: {stats['messages']}\n"
        f"👥 Активных участников: {stats['active_users']}\n"
        f"⚡ Активность чата: {activity}\n"
        f"📷 Фото: {stats['photos']}\n"
        f"🎬 Видео: {stats['videos']}\n"
        f"🎵 Музыка: {stats['music']}\n"
        f"🎙 Голосовые: {stats['voices']}\n\n"
        f"🏆 ТОП-3 АКТИВНЫХ\n"
        f"{format_top(vk, top3, ('🥇', '🥈', '🥉'))}"
    )
    if include_records:
        records = db.get_chat_stat_records(conn)
        record_lines = []
        for metric, label, suffix in (
            ("active_users", "👥 Рекорд активных участников", ""),
            ("daily_messages", "⚡ Рекорд активности чата", " сообщений за день"),
        ):
            record = records.get(metric)
            if record:
                record_day = date.fromisoformat(record["stat_date"]).strftime("%d.%m.%Y")
                record_lines.append(f"{label}: {record['value']}{suffix} ({record_day})")
            else:
                record_lines.append(f"{label}: пока нет данных")
        report += "\n\n📈 РЕКОРДЫ ЧАТА\n" + "\n".join(record_lines)
    if include_night_mode_notice:
        report += (
            "\n\n🌙 Ночной режим чата включён!\n"
            "С 00:00 до 08:00 обычным участникам доступны только текстовые сообщения. "
            "Don-премиум может отправлять сообщения любого типа."
        )
    return send_bot_message(vk, report) > 0


def _morning_content_key(item: dict) -> str:
    stable_id = str(item.get("id") or item.get("text", "")).strip().casefold()
    return hashlib.sha256(stable_id.encode("utf-8")).hexdigest()


def choose_morning_content(conn, items: list[dict], content_type: str,
                           stat_date: date, *, rng=None):
    if not items:
        return None
    rng = rng or random.SystemRandom()
    unique = {}
    for item in items:
        if not str(item.get("text", "")).strip():
            continue
        key = _morning_content_key(item)
        unique.setdefault(key, (item, key))
    if not unique:
        return None

    history = db.get_morning_content_history(conn, content_type)
    cutoff = (stat_date - timedelta(days=config.MORNING_CONTENT_HISTORY_DAYS - 1)).isoformat()
    candidates = [pair for key, pair in unique.items()
                  if key not in history or history[key] < cutoff]
    if candidates:
        item, key = rng.choice(candidates)
    else:
        oldest = min(history.get(key, "0000-00-00") for key in unique)
        candidates = [pair for key, pair in unique.items()
                      if history.get(key, "0000-00-00") == oldest]
        item, key = rng.choice(candidates)
    return {**item, "content_type": content_type, "content_key": key}


def build_morning_message(stat_date: date, weather_line: str | None = None,
                          advice=None, question=None) -> str:
    weekdays = (
        "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
    )
    day_label = (
        f"{weekdays[stat_date.weekday()]}, {stat_date.day} "
        f"{config.MONTH_NAMES[stat_date.month - 1]}"
    )
    lines = ["☀️ Доброе утро, чат!", f"📅 Сегодня: {day_label}"]
    if weather_line:
        lines.append(weather_line)
    if advice:
        lines.append(f"🧥 Совет дня: {advice['text']}")
    if question:
        lines.append(f"💬 Вопрос дня: {question['text']}")
    lines.extend((
        "",
        "🌅 Ночной режим чата отключён. Всем участникам снова доступны сообщения любого типа.",
    ))
    return "\n".join(lines)


def publish_morning_post(vk, conn, stat_date: date, *, content=None,
                         weather_line=None, fetch_weather=True, rng=None) -> bool:
    content = morning_content.load_content(stat_date) if content is None else content
    if fetch_weather and weather_line is None:
        weather_line = morning_content.fetch_weather()
    advice = choose_morning_content(conn, content.get("advice", []), "advice", stat_date, rng=rng)
    question = choose_morning_content(conn, content.get("question", []), "question", stat_date, rng=rng)
    message = build_morning_message(stat_date, weather_line, advice, question)
    if send_bot_message(vk, message) <= 0:
        return False
    for item in (advice, question):
        if item:
            db.record_morning_content(conn, item["content_type"], item["content_key"], stat_date)
    return True


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
    db.record_daily_chat_stats(conn, stat_date)
    if not stats["top_users"]:
        log.info("Герой не назначен: %s — нет сообщений", stat_date)
        return True

    user_id, messages = stats["top_users"][0]
    status_date = stat_date + timedelta(days=1)
    status_key = status_date.isoformat()
    if db.get_hero_title(conn, "day", status_key):
        return True
    if db.get_king(conn, stat_date):
        db.save_hero_title(conn, "day", status_key, user_id, messages)
        return True

    if not announce:
        db.save_king(conn, stat_date, user_id, messages)
        db.save_hero_title(conn, "day", status_key, user_id, messages)
        return True

    names = get_user_names(vk, [user_id])
    hero_record = db.get_hero_message_record(
        conn, (stat_date.isoformat(), user_id, messages)
    )
    text = (
        f"🏆 ГЕРОЙ ДНЯ\n\n"
        f"{mention(user_id, names.get(user_id))}\n"
        f"Сообщений за день: {messages}\n\n"
        f"Сегодня у Героя дня есть право выдать один мут через команду /мут + @username."
    )
    if hero_record:
        record_date, record_user_id, record_messages = hero_record
        record_name = names.get(record_user_id)
        if record_user_id not in names:
            record_name = get_user_names(vk, [record_user_id]).get(record_user_id)
        record_day = date.fromisoformat(record_date).strftime("%d.%m.%Y")
        is_new = record_date == stat_date.isoformat()
        new_label = " — новый рекорд" if is_new else ""
        hero_record_line = (
            f"🏆 Рекорд Героя дня: {mention(record_user_id, record_name)} — "
            f"{record_messages} сообщ. за день ({record_day}){new_label}"
        )
    else:
        hero_record_line = "🏆 Рекорд Героя дня: пока нет данных"
    text += "\n\n" + hero_record_line
    sent = send_bot_message(vk, text)
    if sent <= 0:
        return False
    db.save_king(conn, stat_date, user_id, messages)
    db.save_hero_title(conn, "day", status_key, user_id, messages)
    log.info(
        "Герой дня: status_date=%s user=%s messages=%s",
        status_date,
        user_id,
        messages,
    )
    return True


def publish_period_hero(
    vk, conn, period_type: str, period_start: date, *, announce=True, window_end: date | None = None
) -> bool:
    if period_type not in {"week", "month", "year"}:
        raise ValueError(f"Unsupported non-daily hero period: {period_type}")
    period_key = (period_start.strftime("%Y-%m") if period_type == "month" else
                  period_start.strftime("%Y") if period_type == "year" else
                  period_start.isoformat())
    if db.get_hero_title(conn, period_type, period_key):
        return True
    stats_end = window_end or period_start
    if period_type == "week":
        # The title starts on Monday; count the previous complete Monday–Sunday.
        stats_start = stats_end - timedelta(days=7)
    elif period_type == "month":
        # At the first of the month, count the previous calendar month in full.
        previous_month_last_day = stats_end - timedelta(days=1)
        stats_start = previous_month_last_day.replace(day=1)
    else:
        # The year title follows the existing 365-day lookback rule.
        stats_start = stats_end - timedelta(days=365)
    top = db.get_top_user_messages(conn, stats_start, stats_end, limit=1)
    if not top:
        log.info("Герой %s не назначен: %s — нет сообщений за период", period_type, period_key)
        return True
    user_id, messages = top[0]
    if not announce:
        db.save_hero_title(conn, period_type, period_key, user_id, messages)
        return True
    names = get_user_names(vk, [user_id])
    title = HERO_NAMES[period_type].upper()
    text = (
        f"{HERO_EMOJIS[period_type]} {title}\n\n"
        f"{mention(user_id, names.get(user_id))}\n"
        f"Сообщений за период: {messages}\n\n"
        f"Право Героя — один мут через /мут + @username."
    )
    if send_bot_message(vk, text) <= 0:
        return False
    db.save_hero_title(conn, period_type, period_key, user_id, messages)
    log.info("%s: period=%s user=%s messages=%s", title, period_key, user_id, messages)
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


def handle_period_top_command(vk, conn, from_id: int, text: str, now: int,
                              command_message=None) -> bool:
    command = text.strip().casefold()
    periods = {
        "/неделя": ("НЕДЕЛЯ", 7),
        "/месяц": ("МЕСЯЦ", 30),
        "/год": ("ГОД", 365),
    }
    if command not in periods:
        return False
    if from_id not in config.ADMIN_IDS:
        if command_message is not None:
            delete_message(vk, command_message)
        return True
    label, days = periods[command]
    today = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    start_date = today - timedelta(days=days - 1)
    end_date = today + timedelta(days=1)
    top = db.get_top_user_messages(conn, start_date, end_date, limit=5)
    period_label = f"{start_date:%d.%m.%Y} — {today:%d.%m.%Y}"
    report = (
        f"📊 ТОП-5 ЗА {label}\n"
        f"{period_label}\n\n"
        f"💬 Сообщения участников\n"
        f"{format_top(vk, top, ('🥇', '🥈', '🥉', '4️⃣', '5️⃣'))}"
    )
    send_bot_message(vk, report)
    return True


def _get_social_chat_member_ids(vk, user_ids: tuple[int, ...]) -> set[int] | None:
    """Check only the requested IDs instead of paging through the whole chat."""
    requested_ids = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
    if not requested_ids:
        return set()
    try:
        log.info(
            "Проверка участников для свадьбы: peer=%s requested=%s",
            config.CHAT_PEER_ID, len(requested_ids),
        )
        response = vk.messages.getConversationMembers(
            peer_id=config.CHAT_PEER_ID,
            member_ids=",".join(map(str, requested_ids)),
            count=len(requested_ids),
        )
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            return None
        member_ids = set()
        for member in response["items"]:
            try:
                member_id = int(member.get("member_id", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if member_id > 0:
                member_ids.add(member_id)
        log.info(
            "VK подтвердил участников для свадьбы: peer=%s found=%s",
            config.CHAT_PEER_ID, len(member_ids),
        )
        return member_ids
    except Exception:
        log.exception("Не удалось проверить участников перед командой свадьбы.")
        return None


def _wedding_pair_is_eligible(vk, conn, user_ids: tuple[int, int]) -> bool | None:
    if any(db.is_chat_blacklisted(conn, user_id) for user_id in user_ids):
        return False
    members = _get_social_chat_member_ids(vk, user_ids)
    if members is None:
        return None
    return all(user_id in members for user_id in user_ids)


def _wedding_member_notice(vk, eligible: bool | None) -> None:
    if eligible is None:
        send_bot_message(
            vk, "⚠️ Сейчас не удалось проверить участников беседы. Попробуйте ещё раз позже.",
            temporary=True,
        )
    elif not eligible:
        send_bot_message(
            vk, "❗ Оба участника должны быть в чате и не находиться в его чёрном списке.",
            temporary=True,
        )


def _wedding_don_notice(vk) -> None:
    send_bot_message(
        vk, "💎 Предлагать свадьбу могут премиум-участники и администраторы. "
            "Принять приглашение может любой участник чата.",
        temporary=True,
    )


def handle_marriage_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    stripped = text.strip()
    command = stripped.casefold()

    marriage_match = re.fullmatch(r"/свадьба(?:\s+(.+))?", stripped, re.IGNORECASE)
    if marriage_match:
        target_text = marriage_match.group(1)
        if not target_text:
            send_bot_message(vk, "💍 Использование: /свадьба + @username", temporary=True)
            return True
        target_id = resolve_target_user_id(vk, target_text)
        if target_id <= 0:
            send_bot_message(vk, "❗ Не нашёл пользователя. Укажи @username или @id.", temporary=True)
            return True
        if target_id == from_id:
            send_bot_message(vk, "💍 Предложить свадьбу можно другому участнику, не себе.", temporary=True)
            return True
        if from_id not in config.ADMIN_IDS and not db.is_premium_user(conn, from_id, now):
            _wedding_don_notice(vk)
            return True
        eligible = _wedding_pair_is_eligible(vk, conn, (from_id, target_id))
        if eligible is not True:
            _wedding_member_notice(vk, eligible)
            return True
        result = db.create_marriage_proposal(
            conn, from_id, target_id, now, config.MARRIAGE_PROPOSAL_SECONDS,
        )
        if result == "married":
            send_bot_message(vk, "💍 Один из вас уже состоит в паре.", temporary=True)
        elif result == "pending":
            send_bot_message(
                vk, "💌 У одного из вас уже есть активное предложение. Дождитесь ответа или отмените своё командой /отменить свадьбу.",
                temporary=True,
            )
        else:
            names = get_user_names(vk, [from_id, target_id])
            send_bot_message(
                vk,
                f"💍 {mention(from_id, names.get(from_id))} предлагает {mention(target_id, names.get(target_id))} стать парой!\n"
                f"{mention(target_id, names.get(target_id))}, чтобы согласиться, отправь: /да @id{from_id}\n"
                "Предложение действует 24 часа.",
            )
        return True

    yes_match = re.fullmatch(r"/да(?:\s+\+?\s*(.+))?", stripped, re.IGNORECASE)
    if yes_match:
        proposer_text = yes_match.group(1)
        if not proposer_text:
            send_bot_message(vk, "💌 Использование: /да @username того, кто сделал предложение", temporary=True)
            return True
        proposer_id = resolve_target_user_id(vk, proposer_text)
        if proposer_id <= 0 or proposer_id == from_id:
            send_bot_message(vk, "❗ Укажи автора действующего предложения: /да @username", temporary=True)
            return True
        eligible = _wedding_pair_is_eligible(vk, conn, (proposer_id, from_id))
        if eligible is not True:
            _wedding_member_notice(vk, eligible)
            return True
        result = db.accept_marriage_proposal(conn, proposer_id, from_id, now)
        if result == "not_found":
            send_bot_message(vk, "💌 Для вас нет действующего предложения. Возможно, оно уже истекло.", temporary=True)
        elif result == "married":
            send_bot_message(vk, "💍 Один из вас уже состоит в паре. Предложение закрыто.", temporary=True)
        else:
            names = get_user_names(vk, [proposer_id, from_id])
            send_bot_message(
                vk,
                f"🎉 Согласие получено! {mention(proposer_id, names.get(proposer_id))} и "
                f"{mention(from_id, names.get(from_id))} теперь пара! 💍",
            )
        return True

    if command == "/отменить свадьбу":
        target_id = db.cancel_marriage_proposal(conn, from_id)
        if target_id is None:
            send_bot_message(vk, "💌 У тебя нет исходящего предложения, которое можно отменить.", temporary=True)
        else:
            names = get_user_names(vk, [from_id, target_id])
            send_bot_message(
                vk, f"💌 {mention(from_id, names.get(from_id))} отменил(а) предложение "
                f"{mention(target_id, names.get(target_id))}.",
            )
        return True

    if command == "/супруг":
        marriage = db.get_marriage(conn, from_id)
        if not marriage:
            send_bot_message(vk, "💔 Ты пока не состоишь в паре.")
            return True
        partner_id, married_at = marriage
        names = get_user_names(vk, [partner_id])
        married_on = datetime.fromtimestamp(married_at, config.CHAT_TZ).strftime("%d.%m.%Y")
        send_bot_message(vk, f"💍 Твоя пара: {mention(partner_id, names.get(partner_id))}\nСвадьба: {married_on}")
        return True

    pairs_match = re.fullmatch(r"/пары(?:\s+(\d+))?", stripped, re.IGNORECASE)
    if pairs_match:
        page = int(pairs_match.group(1) or "1")
        if page < 1:
            send_bot_message(vk, "❗ Номер страницы должен быть больше нуля.", temporary=True)
            return True
        page_size = config.MARRIAGE_PAIRS_PAGE_SIZE
        total = db.count_marriage_pairs(conn)
        if total == 0:
            send_bot_message(vk, "💔 В чате пока нет зарегистрированных пар.")
            return True
        pages = (total + page_size - 1) // page_size
        if page > pages:
            send_bot_message(vk, f"❗ Такой страницы нет. Всего страниц: {pages}.", temporary=True)
            return True
        pairs = db.get_marriage_pairs(conn, page_size, (page - 1) * page_size)
        names = get_user_names(vk, [user_id for left, right, _ in pairs for user_id in (left, right)])
        lines = [f"💍 ПАРЫ ЧАТА — страница {page}/{pages}"]
        for index, (left_id, right_id, married_at) in enumerate(pairs, start=(page - 1) * page_size + 1):
            married_on = datetime.fromtimestamp(married_at, config.CHAT_TZ).strftime("%d.%m.%Y")
            lines.append(
                f"{index}. {mention(left_id, names.get(left_id))} 💞 "
                f"{mention(right_id, names.get(right_id))} — с {married_on}"
            )
        lines.append(f"Всего пар: {total}. Следующая страница: /пары {page + 1}" if page < pages else f"Всего пар: {total}.")
        send_bot_message(vk, "\n".join(lines))
        return True

    divorce_match = re.fullmatch(r"/развод(?:\s+\+?\s*(.+))?", stripped, re.IGNORECASE)
    if divorce_match:
        target_text = divorce_match.group(1)
        if not target_text:
            send_bot_message(vk, "💔 Использование: /развод + @username супруга (админ может указать любого супруга)", temporary=True)
            return True
        target_id = resolve_target_user_id(vk, target_text)
        if target_id <= 0:
            send_bot_message(vk, "❗ Не нашёл пользователя. Укажи @username или @id.", temporary=True)
            return True
        is_admin = from_id in config.ADMIN_IDS
        result = db.divorce_marriage(conn, from_id, target_id, allow_any=is_admin)
        if result is None:
            message = ("💔 У этого участника нет зарегистрированного супруга."
                       if is_admin else "💔 Развестись можно только со своим супругом.")
            send_bot_message(vk, message, temporary=True)
            return True
        partner_id, married_at = result
        names = get_user_names(vk, [target_id, partner_id])
        initiator_name = "администратора" if is_admin else "одного из супругов"
        send_bot_message(
            vk,
            f"💔 {mention(target_id, names.get(target_id))} и {mention(partner_id, names.get(partner_id))} "
            f"больше не пара. Брак расторгнут по команде {initiator_name}.",
        )
        return True

    return False


def handle_legends_command(vk, conn, text: str) -> bool:
    if text.strip().casefold() != "/легенды":
        return False
    legends = db.get_chat_legends(conn)
    if not legends:
        send_bot_message(
            vk,
            "Пока в чате нет ни одной Легенды. Станьте первым — отправьте 100 000 сообщений!",
        )
        return True

    names = {}
    for start in range(0, len(legends), 500):
        names.update(get_user_names(vk, [user_id for user_id, _ in legends[start:start + 500]]))

    messages = []
    current = "🌟 ЛЕГЕНДЫ ЧАТА 🌟"
    for index, (user_id, total) in enumerate(legends, start=1):
        count = f"{total:,}".replace(",", " ")
        line = f"{index}. {mention(user_id, names.get(user_id))} — {count} сообщений"
        if len(current) + len(line) + 1 > 3500:
            messages.append(current)
            current = line
        else:
            current += "\n" + line

    summary = f"Всего легенд: {len(legends)}"
    if len(current) + len(summary) + 1 <= 3500:
        current += "\n" + summary
    else:
        messages.append(current)
        current = summary
    messages.append(current)
    for message in messages:
        send_bot_message(vk, message)
    return True


def handle_reputation_top_command(vk, conn, text: str) -> bool:
    if text.strip().casefold() != "/топ":
        return False
    top = db.get_top_reputation(conn, limit=5)
    if not top:
        send_bot_message(vk, "🏆 Пока в чате нет участников с положительной репутацией.")
        return True
    names = get_user_names(vk, [user_id for user_id, _ in top])
    medals = ("🥇", "🥈", "🥉")
    lines = ["🏆 ТОП-5 УЧАСТНИКОВ ПО РЕПУТАЦИИ"]
    for index, (user_id, score) in enumerate(top, start=1):
        rank = medals[index - 1] if index <= len(medals) else f"{index}."
        lines.append(f"{rank} {mention(user_id, names.get(user_id))} — ⭐ {score:+d}")
    send_bot_message(vk, "\n".join(lines))
    return True



def format_court_voters(user_ids: list[int], names: dict[int, str]) -> str:
    if not user_ids:
        return "—"
    return ", ".join(mention(user_id, names.get(user_id)) for user_id in user_ids)


def handle_court_command(vk, conn, from_id: int, text: str, now: int,
                         command_message=None) -> bool:
    stripped = text.strip()
    peer_id = message_field(command_message, "peer_id", config.CHAT_PEER_ID)

    start_match = re.fullmatch(r"/суд\s*\+\s*(.+)", stripped, re.IGNORECASE)
    if start_match:
        if from_id not in config.ADMIN_IDS:
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        target_id = resolve_target_user_id(vk, start_match.group(1))
        if target_id <= 0 or target_id == from_id:
            return True
        if db.get_active_chat_nomination(conn, peer_id):
            return True
        started_at = int(now)
        expires_at = started_at + 10 * 60
        trial_id = db.create_chat_trial(
            conn, peer_id, target_id, from_id, started_at, expires_at,
        )
        if trial_id is None:
            return True
        name = get_user_names(vk, [target_id]).get(target_id)
        deadline = datetime.fromtimestamp(expires_at, config.CHAT_TZ).strftime("%H:%M")
        message = (
            "⚖️ СУД ЧАТА ОТКРЫТ\n\n"
            f"На скамье подсудимых — {mention(target_id, name)}\n"
            "Обвиняется во всех грехах мира!\n\n"
            "🗳 У вас есть 10 минут, чтобы вынести вердикт:\n\n"
            "/виновен — поддержать обвинение\n"
            "/оправдан — поддержать защиту\n\n"
            "Голосуйте внимательно! В случае доказательства вины — мут на 1 час!\n"
            f"Заседание завершится в {deadline}."
        )
        send_bot_message(vk, message, peer_id=peer_id)
        return True

    vote_match = re.fullmatch(r"/(виновен|оправдан)", stripped, re.IGNORECASE)
    if vote_match:
        vote = 1 if vote_match.group(1).casefold() == "виновен" else -1
        db.cast_chat_trial_vote(conn, peer_id, from_id, vote, int(now))
        return True
    return False


def handle_duel_command(vk, conn, from_id: int, text: str, now: int,
                        command_message=None) -> bool:
    stripped = text.strip()
    peer_id = message_field(command_message, "peer_id", config.CHAT_PEER_ID)
    if re.fullmatch(r"/дуэли", stripped, re.IGNORECASE):
        send_bot_message(
            vk,
            "⚔️ КАК ПРОХОДИТ ДУЭЛЬ\n\n"
            "1. Вызови участника: /дуэль + @username\n"
            "2. Он принимает вызов командой /принять в течение 5 минут.\n"
            "3. После старта оба отправляют своё число: /число 42. Можно выбрать любое "
            "число от 0 до 100, но числа соперников должны отличаться. На выбор — 5 минут.\n"
            "4. Бот случайно выбирает тайное число. Побеждает тот, чьё число ближе; "
            "при равной дистанции — ничья без изменения репутации. Команды с числами удаляются.\n\n"
            "🏆 Победитель получает +1 ⭐ репутации (+2 ⭐ за победу над админом).\n"
            "🥴 Проигравший теряет 1 ⭐. Обычный участник получает звание «Неудачник» "
            "и мут на 1 час. Админ теряет репутацию, но не получает мут и звание.\n\n"
            "📅 Обычный участник может отправить до 3 вызовов и сыграть один раз "
            "за московские сутки. Админы не ограничены. Четвёртый вызов удаляется молча.",
            peer_id=peer_id,
        )
        return True

    challenge = re.fullmatch(r"/дуэль\s*\+\s*(.+)", stripped, re.IGNORECASE)
    if challenge:
        target_id = resolve_target_user_id(vk, challenge.group(1))
        if target_id <= 0 or target_id == int(from_id):
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        if db.is_chat_blacklisted(conn, target_id) or db.is_chat_blacklisted(conn, from_id):
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        members = _get_social_chat_member_ids(vk, (from_id, target_id))
        if members is None:
            send_bot_message(vk, "⚠️ Не удалось проверить участников чата. Попробуйте позже.",
                             temporary=True, peer_id=peer_id)
            return True
        if from_id not in members or target_id not in members:
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        now_local = datetime.fromtimestamp(int(now), config.CHAT_TZ)
        status, duel_id = db.create_chat_duel(
            conn, peer_id, from_id, target_id, int(now), int(now) + 5 * 60,
            now_local.date().isoformat(), is_admin=from_id in config.ADMIN_IDS,
        )
        if status == "limit":
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        if status == "participated":
            participant_id = int(duel_id)
            names = get_user_names(vk, [participant_id])
            participant = mention(participant_id, names.get(participant_id))
            send_bot_message(
                vk,
                f"⚔️ У {participant} уже была дуэль сегодня. "
                "Повторно участвовать можно после 00:00 по Москве.",
                peer_id=peer_id,
            )
            return True
        if status == "busy":
            send_bot_message(vk, "⚔️ Один из участников уже участвует в незавершённой дуэли.",
                             temporary=True, peer_id=peer_id)
            return True
        names = get_user_names(vk, [from_id, target_id])
        deadline = datetime.fromtimestamp(int(now) + 5 * 60, config.CHAT_TZ).strftime("%H:%M")
        send_bot_message(
            vk,
            "⚔️ ВЫЗОВ НА ДУЭЛЬ\n\n"
            f"{mention(from_id, names.get(from_id))} вызывает на дуэль "
            f"{mention(target_id, names.get(target_id))}!\n\n"
            f"Чтобы принять вызов, ответь командой /принять до {deadline}.\n"
            "После принятия оба участника отправляют число от 0 до 100 командой /число 50.\n"
            "Бот выберет случайное число. Победит тот, чьё число окажется ближе.\n"
            "Можно отправить до 3 вызовов участникам чата за московские сутки; сыграть — один раз в сутки.\n"
            "У администраторов ограничений нет.",
            peer_id=peer_id,
        )
        log.info("Дуэль вызвана: duel=%s peer=%s challenger=%s opponent=%s",
                 duel_id, peer_id, from_id, target_id)
        return True

    if re.fullmatch(r"/принять", stripped, re.IGNORECASE):
        now_local = datetime.fromtimestamp(int(now), config.CHAT_TZ)
        status, duel_id = db.accept_chat_duel(
            conn, peer_id, from_id, int(now), int(now) + 5 * 60,
            now_local.date().isoformat(), is_admin=from_id in config.ADMIN_IDS,
        )
        if status == "limit":
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        if status == "accepted":
            duel = db.get_chat_duel(conn, duel_id)
            names = get_user_names(vk, [duel["challenger_id"], duel["opponent_id"]])
            send_bot_message(
                vk,
                "⚔️ ДУЭЛЬ НАЧАЛАСЬ\n\n"
                f"Дуэль между {mention(duel['challenger_id'], names.get(duel['challenger_id']))} и "
                f"{mention(duel['opponent_id'], names.get(duel['opponent_id']))}!\n\n"
                "Отправьте команду /число N, где N — число от 0 до 100.\n"
                "На выбор числа есть 5 минут. Если число соперника уже занято, выберите другое.",
                peer_id=peer_id,
            )
        elif status == "busy":
            send_bot_message(vk, "⚔️ Ты уже участвуешь в другой дуэли.",
                             temporary=True, peer_id=peer_id)
        else:
            if command_message is not None:
                delete_message(vk, command_message)
        return True

    guess_match = re.fullmatch(r"/число\s+(\d{1,3})", stripped, re.IGNORECASE)
    if guess_match:
        if command_message is not None:
            delete_message(vk, command_message)
        guess = int(guess_match.group(1))
        if not 0 <= guess <= 100:
            send_bot_message(vk, "🎯 Число должно быть от 0 до 100.",
                             temporary=True, peer_id=peer_id)
            return True
        duel = db.submit_chat_duel_guess(conn, peer_id, from_id, guess, int(now))
        if not duel:
            return True
        if duel.get("guess_occupied"):
            send_bot_message(
                vk, f"🔢 Число {guess} уже занято. Выбери другое от 0 до 100.",
                temporary=True, peer_id=peer_id,
            )
            return True
        if duel["challenger_guess"] is None or duel["opponent_guess"] is None:
            return True
        target_number = random.randint(0, 100)
        challenger_guess = int(duel["challenger_guess"])
        opponent_guess = int(duel["opponent_guess"])
        challenger_distance = abs(challenger_guess - target_number)
        opponent_distance = abs(opponent_guess - target_number)
        loser_id = (
            int(duel["challenger_id"]) if challenger_distance > opponent_distance
            else int(duel["opponent_id"]) if opponent_distance > challenger_distance
            else None
        )
        winner_id = None if loser_id is None else (
            int(duel["opponent_id"]) if loser_id == int(duel["challenger_id"])
            else int(duel["challenger_id"])
        )
        winner_bonus = (
            2 if winner_id is not None and loser_id in config.ADMIN_IDS
            and winner_id not in config.ADMIN_IDS else 1
        )
        completed = db.finish_chat_duel(
            conn, duel["duel_id"], target_number, loser_id, int(now), winner_bonus,
        )
        if not completed:
            return True
        player_ids = [int(completed["challenger_id"]), int(completed["opponent_id"])]
        names = get_user_names(vk, player_ids)
        challenger = mention(player_ids[0], names.get(player_ids[0]))
        opponent = mention(player_ids[1], names.get(player_ids[1]))
        lines = [
            "⚔️ ДУЭЛЬ ЗАВЕРШЕНА",
            "",
            f"{challenger}: {challenger_guess} — {challenger_distance} от цели",
            f"{opponent}: {opponent_guess} — {opponent_distance} от цели",
            f"🎯 Загаданное ботом число: {target_number}",
            "",
        ]
        if loser_id is None:
            lines.append("🤝 Ничья! Репутация и ограничения не меняются.")
        else:
            loser = mention(loser_id, names.get(loser_id))
            winner = mention(winner_id, names.get(winner_id))
            lines.append(f"🏆 Победил {winner}!")
            lines.append(f"✨ Победителю начислено +{winner_bonus} ⭐ репутации.")
            if loser_id in config.ADMIN_IDS:
                lines.append(
                    f"{loser} теряет 1 ⭐ репутации. Админ проиграл — без мута и звания. "
                    "Сегодня даже тайная цель оказалась сильнее!"
                )
            else:
                mute_queued = apply_mute(
                    vk, peer_id, loser_id, reason="Поражение в дуэли",
                    duration_seconds=60 * 60, now=int(now), allow_extend=True,
                )
                lines.append(
                    f"🥴 {loser} — {DUEL_TITLE}: −1 репутация ⭐, мут на 1 час."
                    if mute_queued else
                    f"🥴 {loser} — {DUEL_TITLE}: −1 репутация ⭐. Участник уже под мутом."
                )
                status_message = (
                    f"⏰ С {loser} снято звание «{DUEL_TITLE}». Мут за дуэль завершён."
                )
                db.enqueue(conn, "send", {
                    "peer_id": peer_id, "message": status_message,
                    "temporary": True, "delay": 300, "depends_on": None,
                    "keyboard": None,
                }, next_attempt=int(now) + 60 * 60)
        send_bot_message(vk, "\n".join(lines), peer_id=peer_id)
        return True
    return False


def finalize_due_chat_trials(vk, conn, now: int | None = None) -> int:
    """Close expired trials and atomically queue their verdict and any mute."""
    global ACTIVE_CONN
    ACTIVE_CONN = conn
    timestamp = int(time.time()) if now is None else int(now)
    finalized = 0
    for trial in db.due_chat_trials(conn, timestamp):
        with db._lock:
            vote_rows = conn.execute(
                "SELECT user_id FROM chat_trial_votes WHERE trial_id = ? ORDER BY user_id",
                (trial["trial_id"],),
            ).fetchall()
        voter_ids = [int(row["user_id"]) for row in vote_rows]
        names = get_user_names(vk, [trial["target_id"], *voter_ids])

        with db.atomic(conn):
            changed = conn.execute(
                "UPDATE chat_trials SET status = 'closed', finalized_at = ? "
                "WHERE trial_id = ? AND status = 'active' AND expires_at <= ?",
                (timestamp, trial["trial_id"], timestamp),
            ).rowcount
            if not changed:
                continue
            votes = conn.execute(
                "SELECT user_id, vote FROM chat_trial_votes WHERE trial_id = ? "
                "ORDER BY user_id",
                (trial["trial_id"],),
            ).fetchall()
            guilty_ids = [int(row["user_id"]) for row in votes if int(row["vote"]) == 1]
            acquitted_ids = [int(row["user_id"]) for row in votes if int(row["vote"]) == -1]
            guilty = len(guilty_ids) > len(acquitted_ids)
            target_id = int(trial["target_id"])
            target_name = mention(target_id, names.get(target_id))
            result_lines = [
                "⚖️ СУД ЧАТА ЗАВЕРШЁН",
                "",
                f"Подсудимый: {target_name}",
                "",
                f"🔴 За обвинение: {len(guilty_ids)}",
                format_court_voters(guilty_ids, names),
                "",
                f"🟢 За оправдание: {len(acquitted_ids)}",
                format_court_voters(acquitted_ids, names),
                "",
                f"Вердикт: {'ВИНОВЕН' if guilty else 'ОПРАВДАН'}.",
            ]
            if guilty:
                mute_queued = apply_mute(
                    vk, int(trial["peer_id"]), target_id,
                    reason="Решение Суда чата", duration_seconds=3600, now=timestamp,
                )
                if mute_queued:
                    result_lines.append(
                        f"{target_name} отправляется на час подумать о своём поведении. Мут выдан."
                    )
                else:
                    result_lines.append(
                        f"{target_name} уже находится под мутом, срок не продлён."
                    )
            else:
                result_lines.append("Суд решил: состав преступления не доказан. Подсудимый свободен!")
            send_bot_message(vk, "\n".join(result_lines),
                             peer_id=int(trial["peer_id"]))
            finalized += 1
    return finalized


def handle_nomination_command(vk, conn, from_id: int, text: str, now: int,
                              command_message=None) -> bool:
    stripped = text.strip()
    peer_id = message_field(command_message, "peer_id", config.CHAT_PEER_ID)
    start_match = re.fullmatch(r"/номинация\s*\+\s*(.+)", stripped, re.IGNORECASE)
    if start_match:
        if from_id not in config.ADMIN_IDS:
            if command_message is not None:
                delete_message(vk, command_message)
            return True
        target_id = resolve_target_user_id(vk, start_match.group(1))
        if target_id <= 0 or target_id == from_id or db.get_active_chat_trial(conn, peer_id):
            return True
        started_at = int(now)
        expires_at = started_at + 10 * 60
        nomination_id = db.create_chat_nomination(
            conn, peer_id, target_id, from_id, CHAT_NOMINATION_TITLE,
            started_at, expires_at,
        )
        if nomination_id is None:
            return True
        name = get_user_names(vk, [target_id]).get(target_id)
        deadline = datetime.fromtimestamp(expires_at, config.CHAT_TZ).strftime("%H:%M")
        message = (
            "👑 Народная номинация\n\n"
            "Сейчас чат решает, достоин ли\n"
            f"{mention(target_id, name)} звания\n"
            "«👑 Народная номинация» за вклад в жизнь чата!\n\n"
            "🗳 Голосование открыто на 10 минут:\n\n"
            "/достоин — поддержать номинацию\n"
            "/мимо — пока не присуждать\n\n"
            "Голосуйте по совести — чат всё запомнит!\n"
            f"Итоги будут в {deadline}."
        )
        send_bot_message(vk, message, peer_id=peer_id)
        return True

    vote_match = re.fullmatch(r"/(достоин|мимо)", stripped, re.IGNORECASE)
    if vote_match:
        vote = 1 if vote_match.group(1).casefold() == "достоин" else -1
        db.cast_chat_nomination_vote(conn, peer_id, from_id, vote, int(now))
        return True
    return False


def finalize_due_chat_nominations(vk, conn, now: int | None = None) -> int:
    """Close expired nomination votes and store wins with the final post."""
    global ACTIVE_CONN
    ACTIVE_CONN = conn
    timestamp = int(time.time()) if now is None else int(now)
    finalized = 0
    for nomination in db.due_chat_nominations(conn, timestamp):
        with db._lock:
            vote_rows = conn.execute(
                "SELECT user_id FROM chat_nomination_votes WHERE nomination_id = ? ORDER BY user_id",
                (nomination["nomination_id"],),
            ).fetchall()
        voter_ids = [int(row["user_id"]) for row in vote_rows]
        names = get_user_names(vk, [nomination["target_id"], *voter_ids])

        with db.atomic(conn):
            changed = conn.execute(
                "UPDATE chat_nominations SET status = 'closed', finalized_at = ? "
                "WHERE nomination_id = ? AND status = 'active' AND expires_at <= ?",
                (timestamp, nomination["nomination_id"], timestamp),
            ).rowcount
            if not changed:
                continue
            votes = conn.execute(
                "SELECT user_id, vote FROM chat_nomination_votes WHERE nomination_id = ? "
                "ORDER BY user_id",
                (nomination["nomination_id"],),
            ).fetchall()
            worthy_ids = [int(row["user_id"]) for row in votes if int(row["vote"]) == 1]
            pass_ids = [int(row["user_id"]) for row in votes if int(row["vote"]) == -1]
            won = len(worthy_ids) > len(pass_ids)
            target_id = int(nomination["target_id"])
            target_name = mention(target_id, names.get(target_id))
            category = str(nomination["category"])
            if won:
                conn.execute(
                    "INSERT OR IGNORE INTO chat_nomination_awards "
                    "(nomination_id, user_id, category, awarded_at) VALUES (?, ?, ?, ?)",
                    (int(nomination["nomination_id"]), target_id, category, timestamp),
                )
                result_lines = [
                    "🏆 ПРИЗНАНИЕ ПРИСУЖДЕНО",
                    "",
                    f"{target_name} получает звание",
                    f"«{category}»",
                    "",
                    f"🔵 Достоин — {len(worthy_ids)}",
                    format_court_voters(worthy_ids, names),
                    "",
                    f"⚪ Мимо — {len(pass_ids)}",
                    format_court_voters(pass_ids, names),
                    "",
                    "Чат заметил твой вклад — и теперь это останется в профиле 💛",
                ]
            else:
                result_lines = [
                    "🏅 ИТОГИ ГОЛОСОВАНИЯ",
                    "",
                    f"За номинацию «{category}»:",
                    "",
                    f"🔵 Достоин — {len(worthy_ids)}",
                    format_court_voters(worthy_ids, names),
                    "",
                    f"⚪ Мимо — {len(pass_ids)}",
                    format_court_voters(pass_ids, names),
                    "",
                    "В этот раз признание не присуждено.",
                ]
            send_bot_message(vk, "\n".join(result_lines), peer_id=int(nomination["peer_id"]))
            finalized += 1
    return finalized



def handle_blacklist_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    match = re.fullmatch(r"/чс(?:\s+(.*))?", text.strip(), re.IGNORECASE)
    if not match:
        return False

    if from_id not in config.ADMIN_IDS:
        send_bot_message(
            vk,
            "🛡 Команда чёрного списка доступна только администраторам.",
            temporary=True,
            delete_after_seconds=120,
        )
        return True

    argument = (match.group(1) or "").strip()
    if not argument:
        send_bot_message(
            vk,
            "Команды чёрного списка:\n/чс @username — добавить в ЧС\n"
            "/чс снять @username — убрать из ЧС\n/чс список — показать список",
            temporary=True,
            delete_after_seconds=120,
        )
        return True

    if argument.casefold() == "список":
        blocked_ids = db.get_chat_blacklist(conn)
        text_out = (
            "📋 Чёрный список пуст."
            if not blocked_ids
            else "📋 Чёрный список социальной беседы:\n" + "\n".join(f"• @id{uid}" for uid in blocked_ids)
        )
        send_bot_message(vk, text_out, temporary=True, delete_after_seconds=120)
        return True

    remove = False
    target_text = argument
    remove_match = re.match(r"^снять\s+(.+)$", argument, re.IGNORECASE)
    if remove_match:
        remove = True
        target_text = remove_match.group(1).strip()

    target_id = resolve_target_user_id(vk, target_text)
    if target_id <= 0:
        send_bot_message(
            vk,
            "❗ Укажи пользователя как @username или @id123.",
            temporary=True,
            delete_after_seconds=120,
        )
        return True

    if remove:
        if db.remove_chat_blacklist(conn, target_id):
            response = f"✅ @id{target_id} удалён из чёрного списка."
        else:
            response = f"ℹ️ @id{target_id} не найден в чёрном списке."
        send_bot_message(vk, response, temporary=True, delete_after_seconds=120)
        return True

    if target_id in config.ADMIN_IDS:
        send_bot_message(
            vk,
            "🛡 Администраторов нельзя добавить в чёрный список.",
            temporary=True,
            delete_after_seconds=120,
        )
        return True

    added = db.add_chat_blacklist(conn, target_id, from_id, now)
    removed = remove_user_from_chat(vk, config.CHAT_PEER_ID, target_id)
    if added and removed:
        response = f"🚫 @id{target_id} добавлен в ЧС и удалён из беседы."
    elif added:
        response = f"🚫 @id{target_id} добавлен в ЧС. VK пока не подтвердил удаление; при входе бот удалит его снова."
    elif removed:
        response = f"ℹ️ @id{target_id} уже в ЧС; повторное удаление из беседы выполнено."
    else:
        response = f"ℹ️ @id{target_id} уже в ЧС. При повторном входе бот удалит его автоматически."
    notice_delay = config.SOCIAL_MUTE_NOTICE_DELETE_SECONDS if added else 120
    send_bot_message(vk, response, temporary=True, delete_after_seconds=notice_delay)
    return True


def handle_admin_ban_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    command = text.strip()
    if not re.match(r"^/(?:бан|разбан|баны)(?:\s+.*)?$", command, re.IGNORECASE):
        return False
    if from_id not in config.ADMIN_IDS:
        send_bot_message(vk, "🛡 Команды бана доступны только администраторам.", temporary=True)
        return True

    if re.fullmatch(r"/баны", command, re.IGNORECASE):
        banned = db.get_chat_blacklist(conn)
        response = (
            "📋 Список банов пуст."
            if not banned
            else "📋 Заблокированные участники чата:\n" + "\n".join(f"• @id{uid}" for uid in banned)
        )
        send_bot_message(vk, response, temporary=True, delete_after_seconds=120)
        return True

    match = re.fullmatch(r"/(бан|разбан)\s+\+\s*(.+)", command, re.IGNORECASE)
    if not match:
        send_bot_message(
            vk,
            "Команды бана:\n/бан + @username — добавить в список и удалить из чата\n"
            "/разбан + @username — снять бан\n/баны — список банов",
            temporary=True,
            delete_after_seconds=120,
        )
        return True

    action, target_text = match.groups()
    target_id = resolve_target_user_id(vk, target_text.strip())
    if target_id <= 0:
        send_bot_message(vk, "❗ Укажи пользователя через @username или @id.", temporary=True)
        return True
    if target_id in config.ADMIN_IDS:
        send_bot_message(vk, "🛡 Администраторов нельзя забанить этой командой.", temporary=True)
        return True

    if action.casefold() == "разбан":
        removed = db.remove_chat_blacklist(conn, target_id)
        response = (f"✅ @id{target_id} разбанен и удалён из списка."
                    if removed else f"ℹ️ @id{target_id} не найден в списке банов.")
        send_bot_message(vk, response, temporary=True, delete_after_seconds=120)
        return True

    added = db.add_chat_blacklist(conn, target_id, from_id, now)
    removed = remove_user_from_chat(vk, config.CHAT_PEER_ID, target_id)
    if added and removed:
        response = f"🚫 @id{target_id} забанен и удалён из чата."
    elif added:
        response = f"🚫 @id{target_id} добавлен в список банов. При входе бот удалит его автоматически."
    elif removed:
        response = f"ℹ️ @id{target_id} уже в списке банов; повторное удаление выполнено."
    else:
        response = f"ℹ️ @id{target_id} уже в списке банов."
    send_bot_message(vk, response, temporary=True, delete_after_seconds=120)
    return True


def handle_manual_premium_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    match = re.fullmatch(r"/подписка(?:\s+(.*))?", text.strip(), re.IGNORECASE)
    if not match:
        return False

    if from_id not in config.ADMIN_IDS:
        send_bot_message(
            vk,
            "🛡 Выдавать премиум-подписку могут только администраторы.",
            temporary=True,
        )
        return True

    argument = (match.group(1) or "").strip()
    target_match = re.fullmatch(r"\+\s+(.+)", argument)
    if not target_match:
        send_bot_message(vk, "Формат команды: /подписка + @username", temporary=True)
        return True

    target_id = resolve_target_user_id(vk, target_match.group(1))
    if target_id <= 0:
        send_bot_message(
            vk,
            "❗ Не удалось найти пользователя. Укажи его как @username или @id.",
            temporary=True,
        )
        return True

    previous_until = db.get_manual_premium_until(conn, target_id)
    expires_at = db.grant_manual_premium_month(conn, target_id, now)
    expires_label = datetime.fromtimestamp(expires_at, config.CHAT_TZ).strftime("%d.%m.%Y")
    names = get_user_names(vk, [target_id])
    target = mention(target_id, names.get(target_id))
    if previous_until > now:
        announcement = (
            "🎁 Премиум-подписка продлена!\n\n"
            f"Для {target} добавлен ещё один месяц премиум-доступа 💎\n"
            f"Новый срок действия — {expires_label}.\n"
            "Спасибо, что ты с нами! ✨"
        )
    else:
        announcement = (
            "🎉 В чате новый премиум-участник!\n\n"
            f"Администрация подарила {target} премиум-доступ на месяц 💎\n"
            f"Статус действует до {expires_label}.\n"
            "Добро пожаловать в премиум-клуб! ✨"
        )
    send_bot_message(vk, announcement, peer_id=config.CHAT_PEER_ID)
    return True


def handle_menu_command(vk, conn, from_id: int, text: str, now: int) -> bool:
    if not re.fullmatch(r"/menu", text.strip(), re.IGNORECASE):
        return False

    local_date = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    day_start = int(datetime.combine(local_date, datetime.min.time(), tzinfo=config.CHAT_TZ).timestamp())
    next_day = local_date + timedelta(days=1)
    day_end = int(datetime.combine(next_day, datetime.min.time(), tzinfo=config.CHAT_TZ).timestamp())
    if not db.claim_daily_command_slot(conn, from_id, "menu_daily", 1, day_start, day_end, now):
        if db.claim_daily_command_slot(conn, from_id, "menu_limit_notice", 1, day_start, day_end, now):
            send_bot_message(
                vk,
                "⏳ Ты уже открывал меню сегодня. Следующий вызов будет доступен после 00:00 по Москве.",
                temporary=True,
                delete_after_seconds=120,
            )
        return True

    send_bot_message(
        vk,
        "🤖 МЕНЮ БОТА\n\n"
        "👤 /profile — профиль и характеристики\n"
        "✨ /токсик + @username — добавить характеристику\n"
        "⭐ /rep @username — повысить репутацию\n"
        "⭐ /rep - @username — понизить репутацию\n"
        "🦸 /мут + @username — мут Героя или право ранга\n\n"
        "Нажми «Руководство», чтобы открыть статью с правилами и командами.",
        keyboard=menu_keyboard(),
        temporary=True,
        delete_after_seconds=120,
    )
    return True


CHAT_MESSAGE_RANKS = (
    (100_000, "Легенда чата"),
    (80_000, "Ветеран любви"),
    (70_000, "Опытный"),
    (60_000, "Душа чата"),
    (50_000, "Флирт-мастер"),
    (40_000, "Активный"),
    (30_000, "Болтун"),
    (20_000, "Читатель"),
    (10_000, "Участник"),
    (0, "Новичок"),
)
CHAT_RANK_MUTE_HOURS = {
    "Новичок": 0,
    "Участник": 2,
    "Читатель": 3,
    "Болтун": 4,
    "Активный": 5,
    "Флирт-мастер": 6,
    "Душа чата": 7,
    "Опытный": 8,
    "Ветеран любви": 9,
    "Легенда чата": 10,
}


def chat_rank_for_messages(total_messages: int) -> str:
    for threshold, title in CHAT_MESSAGE_RANKS:
        if total_messages >= threshold:
            return title
    return "Новичок"


def announce_rank_achievement(vk, user_id: int, previous_total: int) -> None:
    new_total = previous_total + 1
    old_rank = chat_rank_for_messages(previous_total)
    new_rank = chat_rank_for_messages(new_total)
    if old_rank == new_rank:
        return
    db.cancel_pending_rank_mute(ACTIVE_CONN, user_id, old_rank)

    names = get_user_names(vk, [user_id])
    user = mention(user_id, names.get(user_id))
    mute_hours = CHAT_RANK_MUTE_HOURS[new_rank]
    if mute_hours == 0:
        rights_text = "На этом ранге право на мут не предусмотрено."
    else:
        hours_word = "часа" if 2 <= mute_hours <= 4 else "часов"
        rights_text = (
            f"Теперь он может дать мут нарушителя на {mute_hours} {hours_word} "
            "(1 раз за всё время на этом ранге)."
        )
    send_bot_message(
        vk,
        f"🎖️ {user} достиг ранга «{new_rank}»! {rights_text}",
        peer_id=config.CHAT_PEER_ID,
    )


def record_social_message(vk, conn, user_id: int, message, now: int) -> None:
    previous_total = db.get_total_message_count(conn, user_id)
    db.record_message(
        conn,
        user_id,
        message_field(message, "date", now),
        classify_media(message),
    )
    announce_rank_achievement(vk, user_id, previous_total)


def handle_profile_command(vk, conn, from_id: int, text: str, now: int,
                           command_message=None) -> bool:
    match = re.match(r"^/profile(?:\s+(.+))?$", text.strip(), re.IGNORECASE)
    if not match:
        return False

    argument = match.group(1)
    target_id = resolve_target_user_id(vk, argument) if argument else from_id
    if target_id <= 0:
        send_bot_message(vk, "❗ Укажи существующего пользователя через @username или @id.", temporary=True)
        return True
    if from_id not in config.ADMIN_IDS:
        today_key = datetime.fromtimestamp(now, config.CHAT_TZ).date().isoformat()
        view_type = "self" if target_id == from_id else "other"
        limit = (config.PROFILE_DAILY_LIMIT_PREMIUM
                 if db.is_premium_user(conn, from_id)
                 else config.PROFILE_DAILY_LIMIT_REGULAR)
        if not db.try_consume_profile_view(conn, from_id, today_key, view_type, limit):
            if command_message is not None:
                delete_message(vk, command_message)
            return True

    reputation = db.get_reputation(conn, target_id)
    marriage = db.get_marriage(conn, target_id)
    duel_record = db.get_chat_duel_record(conn, target_id)
    names = get_user_names(vk, [target_id] + ([marriage[0]] if marriage else []))
    today = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    stats = db.get_user_profile_stats(conn, target_id, today)
    donut_premium = db.is_donut_premium_user(conn, target_id)
    manual_premium_until = db.get_manual_premium_until(conn, target_id)
    characteristics = db.get_user_characteristics(conn, target_id)

    def count_label(value: int, singular: str, few: str, many: str) -> str:
        last_two = value % 100
        last = value % 10
        form = many if 11 <= last_two <= 14 else (
            singular if last == 1 else few if 2 <= last <= 4 else many
        )
        return f"{value} {form}"

    lines = [f"👤 {mention(target_id, names.get(target_id))}"]
    if target_id in config.ADMIN_IDS:
        lines.extend(["", "🛡️ АДМИНИСТРАТОР ЧАТА"])
    if donut_premium:
        lines.extend([
            "",
            "💎 ПОДДЕРЖИВАЕТ @vlgchat",
            "✨ DON-ПРЕМИУМ АКТИВЕН ✨",
        ])
    lines.extend([
        "",
        f"⭐ Репутация: {reputation:+d}",
        f"🎖 Ранг: {chat_rank_for_messages(stats['all_time_messages'])}",
        f"💬 Сообщений за всё время: {stats['all_time_messages']:,}".replace(",", " "),
    ])
    nomination_wins = db.get_chat_nomination_wins(conn, target_id)
    if nomination_wins:
        lines.append(
            f"{CHAT_NOMINATION_TITLE} — "
            f"{count_label(nomination_wins, 'победа', 'победы', 'побед')}"
        )
    if marriage:
        partner_id, married_at = marriage
        married_on = datetime.fromtimestamp(married_at, config.CHAT_TZ).strftime("%d.%m.%Y")
        lines.extend([
            f"💍 В паре с: {mention(partner_id, names.get(partner_id))}",
            f"💒 Вместе с: {married_on}",
        ])
    if manual_premium_until > now:
        expires_label = datetime.fromtimestamp(
            manual_premium_until, config.CHAT_TZ
        ).strftime("%d.%m.%Y")
        lines.append(f"🎁 Премиум от администрации: до {expires_label}")
    hero_labels = {
        "day": "🏆 Герой дня",
        "week": "👑 Герой недели",
        "month": "💎 Герой месяца",
        "year": "🌟 Герой года",
    }
    for period_type in ("day", "week", "month", "year"):
        count, last_key = stats["hero_titles"][period_type]
        if not count or not last_key:
            continue
        if period_type == "month":
            last_label = datetime.strptime(last_key, "%Y-%m").strftime("%m.%Y")
        elif period_type == "year":
            last_label = last_key
        else:
            last_label = date.fromisoformat(last_key).strftime("%d.%m.%Y")
        lines.append(
            f"{hero_labels[period_type]}: {count_label(count, 'раз', 'раза', 'раз')} "
            f"(последний: {last_label})"
        )
    lines.extend([
        "",
        f"⚔️ ДУЭЛИ (победы/поражения): {duel_record[0]}/{duel_record[1]}",
        "",
        "📊 АКТИВНОСТЬ",
        f"Сегодня: {count_label(stats['today'], 'сообщение', 'сообщения', 'сообщений')}",
        f"За 7 дней: {count_label(stats['last_7_days'], 'сообщение', 'сообщения', 'сообщений')}",
        "",
        "🔥 Самый активный день",
    ])
    if stats["best_day"]:
        best_date = date.fromisoformat(stats["best_day"][0])
        best_date_text = f"{best_date.day} {config.MONTH_NAMES[best_date.month - 1]}"
        best_messages = count_label(stats["best_day"][1], "сообщение", "сообщения", "сообщений")
        lines.append(f"{best_date_text} — {best_messages}")
    else:
        lines.append("Пока нет сообщений")
    if characteristics:
        lines.extend(["", "✨ ХАРАКТЕРИСТИКИ"])
        for trait_key, emoji, title in config.CHAT_CHARACTERISTICS:
            score = characteristics.get(trait_key, 0)
            if score:
                lines.append(f"{emoji} {title}: {score:+d}")
    profile = "\n".join(lines)
    send_bot_message(vk, profile)
    return True


def handle_characteristic_command(vk, conn, from_id: int, text: str, now: int,
                                 command_message=None) -> bool:
    command_match = re.match(r"^/([а-яё]+)(?:\s|$)", text.strip(), re.IGNORECASE)
    if not command_match:
        return False
    typed_trait_key = command_match.group(1).casefold()
    trait_key = config.CHAT_CHARACTERISTIC_ALIASES.get(typed_trait_key, typed_trait_key)
    if trait_key not in config.CHAT_CHARACTERISTIC_BY_COMMAND:
        return False
    match = re.fullmatch(r"/([а-яё]+)\s+([+-])\s+(.+)", text.strip(), re.IGNORECASE)
    if not match:
        send_bot_message(
            vk,
            f"Формат команды: /{trait_key} + @username или /{trait_key} - @username.",
            temporary=True,
        )
        return True
    target_id = resolve_target_user_id(vk, match.group(3))
    if target_id <= 0:
        send_bot_message(vk, "❗ Укажи пользователя через @username или @id.", temporary=True)
        return True

    is_admin = from_id in config.ADMIN_IDS
    if target_id == from_id:
        send_bot_message(vk, "❗ Нельзя назначить характеристику самому себе.", temporary=True)
        return True

    local_date = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    result = db.give_characteristic(
        conn,
        giver_id=from_id,
        target_id=target_id,
        trait_key=trait_key,
        delta=1 if match.group(2) == "+" else -1,
        is_admin=is_admin,
        is_premium=db.is_premium_user(conn, from_id),
        week_key=current_week_key(local_date),
        day_key=local_date.isoformat(),
        regular_weekly_limit=config.CHARACTERISTIC_WEEKLY_LIMIT_REGULAR,
        premium_daily_limit=config.CHARACTERISTIC_DAILY_LIMIT_PREMIUM,
        now=now,
    )
    if result in {"daily_limit", "weekly_limit", "target_week_limit"}:
        if command_message is not None:
            delete_message(vk, command_message)
        return True
    if result == "self":
        send_bot_message(vk, "❗ Нельзя назначить характеристику самому себе.", temporary=True)
    elif result == "applied":
        emoji, title = config.CHAT_CHARACTERISTIC_BY_COMMAND[trait_key]
        names = get_user_names(vk, [from_id, target_id])
        change = "+1" if match.group(2) == "+" else "−1"
        send_bot_message(
            vk,
            f"✨ Характеристика {emoji} «{title}»: "
            f"{mention(from_id, names.get(from_id))} → "
            f"{mention(target_id, names.get(target_id))} ({change}).",
        )
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
    if target_id == from_id:
        send_bot_message(vk, "❗ Нельзя менять репутацию самому себе.", temporary=True)
        return True

    value = -1 if match.group(1) == "-" else 1
    week_key = current_week_key(
        datetime.fromtimestamp(now, config.CHAT_TZ).date()
    )
    if not db.add_reputation_vote(
        conn,
        from_id,
        target_id,
        value,
        week_key,
        now,
        is_admin=from_id in config.ADMIN_IDS,
        is_premium=db.is_premium_user(conn, from_id),
        premium_daily_limit=config.REPUTATION_DAILY_LIMIT_PREMIUM,
    ):
        # Исчерпание недельной квоты и повторная оценка того же человека
        # остаются тихими, как и прежние повторные голоса.
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


def handle_king_mute_command(vk, conn, from_id: int, text: str, now: int,
                            command_message=None) -> bool:
    if not re.match(r"^/мут(?:\s+.+)?$", text.strip(), re.IGNORECASE):
        return False

    local_date = datetime.fromtimestamp(now, config.CHAT_TZ).date()
    hero = db.get_active_hero_title(conn, from_id, local_date)
    if hero is None:
        # Compatibility with a legacy daily result that predates hero_titles backfill.
        legacy = db.get_king(conn, local_date - timedelta(days=1))
        if legacy and legacy[0] == from_id:
            hero = {
                "period_type": "day",
                "period_key": local_date.isoformat(),
                "user_id": from_id,
                "messages": legacy[1],
            }

    rank_title = chat_rank_for_messages(db.get_total_message_count(conn, from_id))
    rank_hours = CHAT_RANK_MUTE_HOURS[rank_title]
    rank_cooldown_key = f"rank_mute:{rank_title}"

    # A non-hero without a rank mute right gets no explanatory bot message.
    # Remove their command quietly, as requested.
    if not hero and rank_hours == 0:
        if command_message is not None:
            delete_message(vk, command_message)
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

    if hero:
        period_type = hero["period_type"]
        period_key = hero["period_key"]
        cooldown_key = f"hero_mute:{period_type}:{period_key}"
        if pending_king_mute(conn, from_id, period_key):
            send_bot_message(vk, "⏳ Твой мут уже ожидает подтверждения VK.", temporary=True)
            return True
        if db.get_command_cooldown(conn, from_id, cooldown_key):
            send_bot_message(
                vk,
                f"🏅 Ты уже использовал мут как {HERO_NAMES[period_type].lower()} в этом периоде.",
                temporary=True,
            )
            return True
        if not apply_mute(
            vk, config.CHAT_PEER_ID, target_id, king_id=from_id, now=now,
            duration_seconds=HERO_MUTE_SECONDS[period_type],
            hero_period_type=period_type, hero_period_key=period_key,
            hero_cooldown_key=cooldown_key,
            hourly_segments=4 if period_type == "week" else 1,
        ):
            send_bot_message(vk, "🔇 Пользователь уже под ограничением. Твой мут не потрачен.", temporary=True)
        return True

    if pending_rank_mute(conn, from_id, rank_title):
        send_bot_message(vk, "⏳ Твой мут уже ожидает подтверждения VK.", temporary=True)
        return True
    if db.get_command_cooldown(conn, from_id, rank_cooldown_key):
        send_bot_message(
            vk,
            "Вы уже использовали мут на этом ранге. Дождитесь повышения.",
            temporary=True,
        )
        return True

    target_total = db.get_total_message_count(conn, target_id)
    target_rank = chat_rank_for_messages(target_total)
    if CHAT_RANK_MUTE_HOURS[target_rank] > rank_hours:
        send_bot_message(vk, "❗ Нельзя мутить участника с более высоким рангом.", temporary=True)
        return True
    target_hero = db.get_active_hero_title(conn, target_id, local_date)
    if target_hero is None:
        legacy_target = db.get_king(conn, local_date - timedelta(days=1))
        if legacy_target and legacy_target[0] == target_id:
            target_hero = True
    if target_hero:
        send_bot_message(vk, "🏆 Нельзя мутить действующего Героя дня, недели, месяца или года.", temporary=True)
        return True

    if not apply_mute(
        vk, config.CHAT_PEER_ID, target_id, now=now,
        duration_seconds=rank_hours * 3600,
        rank_user_id=from_id,
        rank_title=rank_title,
        rank_cooldown_key=rank_cooldown_key,
        hourly_segments=rank_hours,
    ):
        send_bot_message(vk, "🔇 Пользователь уже под ограничением. Твоё право не потрачено.", temporary=True)

    return True


def handle_command(vk, conn, from_id: int, text: str, now: int,
                   command_message=None) -> bool:
    stripped = text.strip()
    if stripped.casefold() in {"👤 мой профиль", "мой профиль"}:
        return handle_profile_command(vk, conn, from_id, "/profile", now, command_message)
    if not stripped.startswith("/"):
        return False

    if handle_manual_premium_command(vk, conn, from_id, stripped, now):
        return True
    if handle_duel_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_court_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_nomination_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_admin_ban_command(vk, conn, from_id, stripped, now):
        return True
    if handle_blacklist_command(vk, conn, from_id, stripped, now):
        return True
    if handle_menu_command(vk, conn, from_id, stripped, now):
        return True
    if handle_commands_link_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_donut_promo_command(vk, from_id, stripped, command_message):
        return True
    if handle_stat_command(vk, conn, from_id, stripped, now):
        return True
    if handle_period_top_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_legends_command(vk, conn, stripped):
        return True
    if handle_reputation_top_command(vk, conn, stripped):
        return True
    if handle_marriage_command(vk, conn, from_id, stripped, now):
        return True
    if handle_characteristic_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_profile_command(vk, conn, from_id, stripped, now, command_message):
        return True
    if handle_reputation_command(vk, conn, from_id, stripped, now):
        return True
    if handle_king_mute_command(vk, conn, from_id, stripped, now, command_message):
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
                "peer_id": peer_id, "message_ids": str(message_id), "delete_for_all": 1,
            }, "peer_id": peer_id})
            db.remove_bot_message(conn, message_id)
        timestamp = int(time.time()) if now is None else now
        for row in conn.execute('SELECT * FROM cleanup_tasks WHERE delete_at <= ?', (timestamp,)).fetchall():
            params = ({'peer_id': row['peer_id'], 'cmids': str(row['cmid']), 'delete_for_all': 1}
                      if row['cmid'] > 0 else {'message_ids': str(row['message_id']), 'delete_for_all': 1})
            db.enqueue(conn, 'delete', {'params': params, 'peer_id': row['peer_id']})
            conn.execute('DELETE FROM cleanup_tasks WHERE id = ?', (row['id'],))


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

    if is_bot_feature_chat(peer_id) and db.is_chat_blacklisted(conn, from_id):
        remove_user_from_chat(vk, peer_id, from_id)
        return

    # Administrators retain moderation exemptions, but can use every feature command.
    if from_id in config.ADMIN_IDS:
        if is_bot_feature_chat(peer_id):
            if handle_command(vk, conn, from_id, text, now, message) or text.strip().startswith("/"):
                return
            record_social_message(vk, conn, from_id, message, now)
        return

    if handle_content_violation(vk, conn, message, from_id, peer_id):
        if is_bot_feature_chat(peer_id):
            db.track_spam_streak(conn, peer_id, from_id, None, now)
        return

    if (is_bot_feature_chat(peer_id) and is_night_mode_active(now)
            and not db.is_premium_user(conn, from_id, now)
            and has_non_text_content(message)):
        delete_message(vk, message)
        log.info("Ночной режим: удалено нетекстовое сообщение user=%s peer=%s", from_id, peer_id)
        return

    if (is_bot_feature_chat(peer_id) and has_audio_recording(message)
            and not db.is_premium_user(conn, from_id, now)
            and not db.claim_interval_cooldown(
                conn, from_id, "audio_recording", now, 60 * 60,
            )):
        delete_message(vk, message)
        log.info("Лимит аудиозаписей: удалено сообщение user=%s peer=%s", from_id, peer_id)
        return

    if is_bot_feature_chat(peer_id) and any(
        re.search(r"(?<!\w)@all(?!\w)", message_text(item), re.IGNORECASE)
        for item in moderated_contents(message)
    ):
        deleted = delete_message(vk, message)
        db.track_spam_streak(conn, peer_id, from_id, None, now)
        log.warning(
            "Удалена запрещённая @all-команда: user=%s deleted=%s",
            from_id, deleted,
        )
        return

    if is_bot_feature_chat(peer_id):
        command_handled = handle_command(vk, conn, from_id, text, now, message)
        if command_handled or text.strip().startswith("/"):
            db.track_spam_streak(conn, peer_id, from_id, None, now)
            return
        if is_spam_message(conn, message, from_id, peer_id, now):
            deleted = delete_message(vk, message)
            muted = apply_mute(
                vk, peer_id, from_id, reason=config.SPAM_MUTE_REASON,
                deletion_id=deleted, now=now,
            )
            log.warning(
                "Спам: user=%s peer=%s deleted=%s muted=%s",
                from_id, peer_id, deleted, muted,
            )
            return
        record_social_message(vk, conn, from_id, message, now)
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



def pending_king_mute(conn, user_id, period_key):
    with db._lock:
        for row in conn.execute("SELECT payload FROM outbox WHERE kind = 'mute' AND status = 'pending'"):
            payload = json.loads(row["payload"])
            if payload.get("king_id") != user_id:
                continue
            if payload.get("hero_period_key") == period_key:
                return True
            if not payload.get("hero_period_key"):
                request_date = datetime.fromtimestamp(payload["requested_at"], config.CHAT_TZ).date()
                if request_date.isoformat() == period_key:
                    return True
    return False


def pending_rank_mute(conn, user_id, rank_title):
    with db._lock:
        for row in conn.execute("SELECT payload FROM outbox WHERE kind = 'mute' AND status = 'pending'"):
            payload = json.loads(row["payload"])
            if (payload.get("rank_user_id") == user_id and
                    payload.get("rank_title") == rank_title and
                    payload.get("segment_index", 0) == 0):
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


class DeliveryError(RuntimeError):
    def __init__(self, message, *, code=None, permanent=False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


def check_embedded_error(item):
    error = item.get('error')
    if error:
        raise DeliveryError(str(error.get('message', error.get('error_msg', 'VK rejected operation'))),
                            code=error.get('code', error.get('error_code')))


def parse_sent_message(response, peer_id):
    if isinstance(response, list):
        matches = [item for item in response if isinstance(item, dict) and item.get('peer_id') == peer_id]
        if len(matches) != 1:
            raise DeliveryError('Ответ отправки не содержит единственного нужного адресата', permanent=True)
        item = matches[0]
        check_embedded_error(item)
        message_id = int(item.get('message_id', 0) or 0)
        cmid = int(item.get('conversation_message_id', 0) or 0)
        if message_id > 0 or cmid > 0:
            return message_id, cmid
    elif isinstance(response, int) and not isinstance(response, bool) and response > 0:
        return response, 0  # Compatibility with legacy numeric API responses.
    # A zero/unknown answer may follow a real send. Never automatically resend it.
    raise DeliveryError('Неоднозначный ответ отправки: нет ID; требуется проверка в чате', permanent=True)


def validate_deleted_messages(response, params):
    if isinstance(response, list):
        if not response:
            raise DeliveryError('Пустой ответ удаления')
        cmids = str(params.get('cmids', '')).split(',') if params.get('cmids') else []
        message_ids = str(params.get('message_ids', '')).split(',') if params.get('message_ids') else []
        seen = set()
        for item in response:
            if not isinstance(item, dict):
                raise DeliveryError('Некорректный элемент ответа удаления')
            check_embedded_error(item)
            if item.get('response') != 1:
                raise DeliveryError('VK не подтвердил удаление', permanent=True)
            if cmids:
                if item.get('peer_id') != params.get('peer_id'):
                    raise DeliveryError('Удаление подтверждено для другой беседы', permanent=True)
                seen.add(str(item.get('conversation_message_id', 0)))
            else:
                seen.add(str(item.get('message_id', 0)))
        if not set(cmids or message_ids).issubset(seen):
            raise DeliveryError('Часть сообщений не подтверждена в ответе удаления', permanent=True)
    elif isinstance(response, dict):
        expected = set(str(params.get('message_ids', '')).split(','))
        if not expected or '' in expected or not expected.issubset(response) or any(response[key] != 1 for key in expected):
            raise DeliveryError('VK не подтвердил удаление всех ID', permanent=True)
    elif response != 1:
        raise DeliveryError('Некорректный ответ удаления', permanent=True)


def execute_operation(vk, conn, row, now):
    payload = json.loads(row["payload"])
    if row["kind"] == "send":
        if payload.get("depends_on"):
            with db._lock:
                dependency = conn.execute("SELECT status FROM outbox WHERE id = ?", (payload["depends_on"],)).fetchone()
            if dependency is None or dependency['status'] in {'failed', 'cancelled'}:
                raise DeliveryError("Удаление не подтверждено; уведомление заблокировано", permanent=True)
            if dependency['status'] != 'done':
                return
        # peer_ids requests a structured response with the conversation-scoped ID.
        send_params = {
            "peer_ids": str(payload["peer_id"]),
            "random_id": row["id"],
            "message": payload["message"],
        }
        if payload.get("keyboard"):
            send_params["keyboard"] = json.dumps(payload["keyboard"], ensure_ascii=False)
        response = vk.messages.send(**send_params)
        message_id, cmid = parse_sent_message(response, payload["peer_id"])
        with db.atomic(conn):
            if payload["temporary"]:
                if cmid:
                    db.queue_cleanup(conn, payload["peer_id"], message_id, cmid, now + payload["delay"])
                else:
                    db.queue_bot_message(conn, message_id, payload["peer_id"], now + payload["delay"])
            db.finish_operation(conn, row["id"], now)
    elif row["kind"] == "delete":
        params = dict(payload["params"])
        # Normalize jobs created before v6 without rewriting their stored payload.
        if 'conversation_message_ids' in params:
            params['cmids'] = params.pop('conversation_message_ids')
        response = vk.messages.delete(**params)
        validate_deleted_messages(response, params)
        db.finish_operation(conn, row["id"], now)
    elif row["kind"] == "mute":
        previous_segment_id = payload.get("previous_segment_id")
        if previous_segment_id:
            with db._lock:
                previous = conn.execute(
                    "SELECT status FROM outbox WHERE id = ?", (previous_segment_id,)
                ).fetchone()
            if previous is None or previous["status"] in {"failed", "cancelled"}:
                db.stop_operation(
                    conn, row["id"], "Предыдущий часовой мут не был подтверждён.", status="cancelled"
                )
                return
            if previous["status"] != "done":
                return
        remaining = payload["until"] - now
        requested_date = datetime.fromtimestamp(payload["requested_at"], config.CHAT_TZ).date()
        today = datetime.fromtimestamp(now, config.CHAT_TZ).date()
        legacy_expired = payload.get("king_id") and not payload.get("hero_period_type") and requested_date != today
        if remaining <= 0 or legacy_expired:
            with db.atomic(conn):
                log.error("Мут не доставлен до истечения срока: operation=%s", row["id"])
                if payload.get("king_id") and payload.get("hero_notice", True):
                    send_bot_message(vk, "❗ VK не подтвердил мут. Право не было потрачено.", temporary=True)
                db.finish_operation(conn, row["id"], now)
            return
        result = vk.messages.changeConversationMemberRestrictions(peer_id=payload["peer_id"],
            member_ids=str(payload["user_id"]), action="ro", **{"for": remaining})
        if isinstance(result, dict):
            check_embedded_error(result)
        if not result:
            raise RuntimeError("VK не подтвердил ограничение")
        with db.atomic(conn):
            db.confirm_mute(conn, payload["peer_id"], payload["user_id"], payload["until"])
            record_successful_mute(conn, payload["user_id"], payload["peer_id"], now)
            if payload.get("king_id"):
                cooldown_key = payload.get("hero_cooldown_key") or "king_mute"
                db.set_command_cooldown(conn, payload["king_id"], cooldown_key, 0, now)
                if payload.get("hero_period_type") == "day":
                    db.set_command_cooldown(conn, payload["king_id"], "king_mute", 0, now)
                if payload.get("hero_notice", True):
                    period_type = payload.get("hero_period_type", "day")
                    names = get_user_names(vk, [payload["user_id"]])
                    send_bot_message(vk, config.HERO_MUTE_MESSAGE.format(
                        hero_title=HERO_NAMES[period_type],
                        duration=HERO_MUTE_LABELS[period_type],
                        mention=mention(payload["user_id"], names.get(payload["user_id"]))),
                        temporary=True,
                        peer_id=payload["peer_id"],
                        delete_after_seconds=config.SOCIAL_MUTE_NOTICE_DELETE_SECONDS)
            elif payload.get("rank_user_id") is not None:
                if payload.get("segment_index", 0) == 0:
                    db.set_command_cooldown(
                        conn, payload["rank_user_id"], payload["rank_cooldown_key"], 0, now
                    )
                if payload.get("rank_notice", True):
                    names = get_user_names(vk, [payload["user_id"]])
                    duration_hours = CHAT_RANK_MUTE_HOURS[payload["rank_title"]]
                    hour_word = "часа" if 2 <= duration_hours <= 4 else "часов"
                    send_bot_message(
                        vk,
                        config.RANK_MUTE_MESSAGE.format(
                            rank_title=payload["rank_title"],
                            mention=mention(payload["user_id"], names.get(payload["user_id"])),
                            duration=f"{duration_hours} {hour_word}",
                        ),
                        temporary=True,
                        peer_id=payload["peer_id"],
                        delete_after_seconds=config.SOCIAL_MUTE_NOTICE_DELETE_SECONDS,
                    )
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
            timestamp = int(time.time()) if now is None else now
            try:
                payload = json.loads(row['payload'])
                peer_id = payload.get('peer_id') or payload.get('params', {}).get('peer_id')
                if peer_id and not is_target_chat(int(peer_id)):
                    db.stop_operation(conn, row['id'], 'Чат исключён из текущей конфигурации', 'cancelled')
                    continue
                if row['kind'] == 'send' and timestamp - row['created_at'] > config.SEND_MAX_AGE_SECONDS:
                    db.stop_operation(conn, row['id'], 'Сообщение устарело до доставки', 'cancelled')
                    continue
                if row['attempts'] >= config.DELIVERY_MAX_ATTEMPTS:
                    db.stop_operation(conn, row['id'], row['last_error'] + '; исчерпаны попытки')
                    continue
                execute_operation(vk, conn, row, timestamp)
            except Exception as error:
                code = getattr(error, 'code', None)
                label = f"{type(error).__name__}: code={code if code is not None else 'n/a'}"
                if isinstance(error, DeliveryError):
                    label += '; ' + str(error)
                permanent = code in {5, 15, 100, 113, 917, 922, 924, 925, 927, 932, 935, 945, 946, 947}
                if permanent or getattr(error, 'permanent', False) or row['attempts'] + 1 >= config.DELIVERY_MAX_ATTEMPTS:
                    db.stop_operation(conn, row['id'], label, attempted=True)
                    log.error("Действие требует исправления: operation=%s kind=%s error=%s", row['id'], row['kind'], label)
                else:
                    log.warning("Действие VK будет повторено: operation=%s kind=%s error=%s", row['id'], row['kind'], label)
                    db.retry_operation(conn, row['id'], label, timestamp)


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
    export_key = f"chat-stats-export:{now.date().isoformat()}"
    if not db.scheduler_event_claimed(conn, export_key):
        if chat_stats_export.export_history(conn, now.date()):
            db.claim_scheduler_event(conn, export_key, int(now.timestamp()))
    finalize_due_chat_trials(vk, conn, int(now.timestamp()))
    finalize_due_chat_nominations(vk, conn, int(now.timestamp()))
    db.expire_chat_duels(conn, int(now.timestamp()))
    today_midnight = datetime.combine(now.date(), datetime.min.time(), config.CHAT_TZ)
    morning_at = today_midnight.replace(hour=config.MORNING_POST_HOUR)
    morning_event_key = f"morning:{now.date().isoformat()}"
    morning_contents = None
    morning_weather = None
    project_news = morning_content.load_news()
    with db._lock:
        initial_checkpoint = conn.execute(
            "SELECT created_at FROM scheduler_state WHERE event_key = 'checkpoint'"
        ).fetchone()
    initial_lower = (
        datetime.fromtimestamp(initial_checkpoint["created_at"], config.CHAT_TZ)
        if initial_checkpoint else today_midnight - timedelta(seconds=1)
    )
    initial_lower = max(initial_lower, now - timedelta(days=config.SCHEDULER_CATCHUP_DAYS))
    morning_age = now - morning_at
    if (initial_lower < morning_at <= now and
            timedelta(0) <= morning_age <= timedelta(minutes=config.MORNING_POST_LATE_WINDOW_MINUTES) and
            not db.scheduler_event_claimed(conn, morning_event_key)):
        morning_contents = morning_content.load_content(now.date())
        morning_weather = morning_content.fetch_weather()

    with db.atomic(conn):
        checkpoint = conn.execute("SELECT created_at FROM scheduler_state WHERE event_key = 'checkpoint'").fetchone()
        first_pass = checkpoint is None
        lower = (datetime.fromtimestamp(checkpoint["created_at"], config.CHAT_TZ) if checkpoint
                 else now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1))
        lower = max(lower, now - timedelta(days=config.SCHEDULER_CATCHUP_DAYS))
        for item in project_news:
            try:
                scheduled_at = datetime.fromisoformat(item["datetime"]).replace(tzinfo=config.CHAT_TZ)
                news_text = str(item.get("text", "")).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not news_text or scheduled_at > now:
                continue
            identity = hashlib.sha256(news_text.encode("utf-8")).hexdigest()[:16]
            key = f"project-news:{scheduled_at.isoformat()}:{identity}"
            if db.scheduler_event_claimed(conn, key):
                continue
            age = now - scheduled_at
            if age > timedelta(minutes=config.PROJECT_NEWS_LATE_WINDOW_MINUTES):
                db.claim_scheduler_event(conn, key, int(now.timestamp()))
                log.info("Новость пропущена: окно публикации прошло (%s)", scheduled_at.isoformat())
            elif publish_project_news(vk, news_text):
                db.claim_scheduler_event(conn, key, int(now.timestamp()))
        cursor = lower.date()
        while cursor <= now.date():
            midnight = datetime.combine(cursor, datetime.min.time(), config.CHAT_TZ)
            if lower < midnight <= now:
                yesterday = cursor - timedelta(days=1)
                for key, publisher in ((f"king:{yesterday.isoformat()}", publish_king),
                                       (f"daily:{yesterday.isoformat()}", publish_daily_stats)):
                    if not db.scheduler_event_claimed(conn, key):
                        queued = (publisher(vk, conn, yesterday, announce=(cursor == now.date()))
                                  if publisher is publish_king
                                  else publisher(
                                      vk, conn, yesterday, include_records=True,
                                      include_night_mode_notice=(
                                          cursor == now.date()
                                          and now < midnight.replace(hour=config.MORNING_POST_HOUR)
                                      ),
                                  ))
                        if queued:
                            db.claim_scheduler_event(conn, key, int(now.timestamp()))
                periodic_heroes = []
                if cursor.weekday() == 0:
                    periodic_heroes.append(("week", cursor))
                if cursor.day == 1:
                    periodic_heroes.append(("month", cursor))
                if cursor.month == 1 and cursor.day == 1:
                    periodic_heroes.append(("year", cursor))
                for period_type, period_start in periodic_heroes:
                    period_key = (period_start.strftime("%Y-%m") if period_type == "month" else
                                  period_start.strftime("%Y") if period_type == "year" else
                                  period_start.isoformat())
                    event_key = f"hero:{period_type}:{period_key}"
                    if not db.scheduler_event_claimed(conn, event_key):
                        if publish_period_hero(vk, conn, period_type, period_start,
                                               announce=(cursor == now.date())):
                            db.claim_scheduler_event(conn, event_key, int(now.timestamp()))
            hours = [hour for hour in config.DAILY_REPORT_HOURS
                     if lower < midnight.replace(hour=hour) <= now]
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
            morning_at = midnight.replace(hour=config.MORNING_POST_HOUR)
            if cursor == now.date() and lower < morning_at <= now:
                key = f"morning:{cursor.isoformat()}"
                if not db.scheduler_event_claimed(conn, key):
                    if now - morning_at > timedelta(minutes=config.MORNING_POST_LATE_WINDOW_MINUTES):
                        db.claim_scheduler_event(conn, key, int(now.timestamp()))
                        log.info("Утренний пост пропущен: окно отправки уже прошло (%s)", cursor)
                    elif publish_morning_post(
                        vk, conn, cursor, content=morning_contents or {"advice": [], "question": []},
                        weather_line=morning_weather, fetch_weather=False,
                    ):
                        db.claim_scheduler_event(conn, key, int(now.timestamp()))
            promo_at = midnight.replace(hour=config.DONUT_PROMO_HOUR)
            if cursor == now.date() and lower < promo_at <= now:
                key = f"donut-promo:{cursor.isoformat()}"
                if not db.scheduler_event_claimed(conn, key):
                    delay = now - promo_at
                    if delay > timedelta(minutes=config.DONUT_PROMO_LATE_WINDOW_MINUTES):
                        db.claim_scheduler_event(conn, key, int(now.timestamp()))
                        log.info("Реклама Don-премиума пропущена: окно отправки прошло (%s)", cursor)
                    elif publish_donut_promo(vk):
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

    threading.Thread(
        target=chat_blacklist_watchdog,
        args=(vk, conn),
        name="chat-blacklist-watchdog",
        daemon=True,
    ).start()

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
                    handle_chat_update(vk, event, conn)
                elif str(getattr(event, "type", "")) in DONUT_EVENT_TYPES:
                    handle_donut_event(conn, event)

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
