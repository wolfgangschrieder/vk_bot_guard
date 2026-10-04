import logging
import re
import sys
import threading
import time
import traceback
from datetime import datetime

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
    return peer_id == config.CHAT_PEER_ID


def delete_message(vk, message) -> bool:
    """Delete the offending message from the conversation for everyone."""
    message_id = message_field(message, "id")
    conversation_message_id = message_field(
        message, "conversation_message_id"
    )
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

        log.warning(
            "Не удалось удалить сообщение: отсутствуют id "
            "(id=%s, conversation_message_id=%s, peer_id=%s)",
            message_id,
            conversation_message_id,
            peer_id,
        )
        return False
    except ApiError as err:
        log.error(
            "Не удалось удалить сообщение id=%s conversation_message_id=%s "
            "peer_id=%s: %s",
            message_id,
            conversation_message_id,
            peer_id,
            err,
        )
        return False


def apply_mute(vk, peer_id: int, user_id: int) -> bool:
    """Restrict a user from writing in the conversation for MUTE_SECONDS."""
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
    """Restore a user's ability to write in the conversation."""
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
    """Remove a user from a VK conversation."""
    chat_id = peer_id - CHAT_PEER_START

    if chat_id <= 0:
        log.warning(
            "Не удалось удалить user=%s: некорректный peer_id=%s",
            user_id,
            peer_id,
        )
        return False

    try:
        vk.messages.removeChatUser(
            chat_id=chat_id,
            user_id=user_id,
        )
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
        log.warning(
            "CHAT_UPDATE получен, но payload отсутствует: %r",
            getattr(event, "raw", event),
        )
        return

    # VK can provide peer_id directly in the CHAT_UPDATE payload.
    # Fall back to chat_id for compatibility with older payload formats.
    peer_id = message_field(obj, "peer_id")
    if peer_id <= 0:
        chat_id = message_field(obj, "chat_id")
        peer_id = CHAT_PEER_START + chat_id if chat_id > 0 else 0

    if peer_id != config.BLOCKLIST_CHAT_PEER_ID:
        return

    action = event_field(obj, "action")
    if isinstance(action, dict):
        action_type = action.get("type")
        member_id = action.get("member_id") or action.get("user_id")
    else:
        action_type = action
        member_id = (
            event_field(obj, "member_id")
            or event_field(obj, "user_id")
        )

    if action_type not in {"chat_invite_user", "chat_invite_user_by_link"}:
        return

    try:
        member_id = int(member_id or 0)
    except (TypeError, ValueError):
        member_id = 0

    if member_id <= 0:
        log.warning(
            "Не удалось определить добавленного пользователя: peer=%s action=%r",
            peer_id,
            action,
        )
        return

    log.info(
        "Пользователь добавлен в беседу: user=%s peer=%s action=%s",
        member_id,
        peer_id,
        action_type,
    )

    if member_id not in config.BLOCKED_USER_IDS:
        return

    remove_user_from_chat(vk, peer_id, member_id)


def scan_blocklist_chat(vk) -> None:
    """Find configured blocked users currently present in the blocklist chat and remove them."""
    if not config.BLOCKED_USER_IDS:
        return

    try:
        response = vk.messages.getConversationMembers(
            peer_id=config.BLOCKLIST_CHAT_PEER_ID,
            count=1000,
        )
        members = response.get("items", []) if isinstance(response, dict) else []
        present_ids = set()

        for member in members:
            if not isinstance(member, dict):
                continue
            try:
                member_id = int(member.get("member_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if member_id > 0:
                present_ids.add(member_id)

        blocked_present = present_ids & config.BLOCKED_USER_IDS
        for user_id in blocked_present:
            remove_user_from_chat(
                vk,
                config.BLOCKLIST_CHAT_PEER_ID,
                user_id,
            )

    except ApiError as err:
        log.warning(
            "Не удалось проверить участников blocklist-чата peer=%s: %s",
            config.BLOCKLIST_CHAT_PEER_ID,
            err,
        )
    except Exception:
        log.error(
            "Ошибка проверки участников blocklist-чата:\\n%s",
            traceback.format_exc(),
        )


def blocklist_watchdog(vk) -> None:
    """Continuously enforce the blocklist independently of CHAT_UPDATE events."""
    log.info(
        "Watchdog blocklist-чата запущен: peer=%s, интервал=5 сек.",
        config.BLOCKLIST_CHAT_PEER_ID,
    )
    while True:
        try:
            scan_blocklist_chat(vk)
        except Exception:
            log.error(
                "Ошибка blocklist watchdog:\\n%s",
                traceback.format_exc(),
            )
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
        if isinstance(attachment, dict):
            attachment_type = str(attachment.get("type", "") or "").lower()
        else:
            attachment_type = str(getattr(attachment, "type", "") or "").lower()
        if attachment_type in {"audio", "video"}:
            return True
    return False


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def contains_any_prohibited_term(text: str) -> bool:
    normalized = normalized_text(text)
    for term in config.PROHIBITED_LEXICON:
        if term in normalized:
            return True
    return any(re.search(pattern, normalized) for pattern in config.PRICE_PATTERNS)


def contains_porn_sale_term(text: str) -> bool:
    normalized = normalized_text(text)
    return "порновидео" in normalized or "домашнее видео" in normalized or "продам видео" in normalized


def send_mute_reason(vk, user_id: int, reason: str) -> None:
    try:
        vk.messages.send(
            peer_id=config.CHAT_PEER_ID,
            random_id=0,
            message=f"[id{user_id}|Пользователь], {reason}",
        )
    except ApiError as err:
        log.warning(
            "Не удалось отправить причину мута user=%s peer=%s: %s",
            user_id,
            config.CHAT_PEER_ID,
            err,
        )


def handle_content_violation(vk, message, from_id: int) -> bool:
    """Apply content rules. Returns True when a message was handled as a violation."""
    text = message_text(message)

    # More specific porn-sale wording gets its dedicated message.
    if contains_porn_sale_term(text):
        deleted = delete_message(vk, message)
        muted = apply_mute(vk, config.CHAT_PEER_ID, from_id)
        send_mute_reason(vk, from_id, config.PORN_SALE_MUTE_REASON)
        log.warning(
            "Нарушение: продажа порно user=%s deleted=%s muted=%s",
            from_id, deleted, muted,
        )
        return True

    if has_media_attachment(message):
        deleted = delete_message(vk, message)
        muted = apply_mute(vk, config.CHAT_PEER_ID, from_id)
        send_mute_reason(vk, from_id, config.MEDIA_MUTE_REASON)
        log.warning(
            "Нарушение: музыка/видео user=%s deleted=%s muted=%s",
            from_id, deleted, muted,
        )
        return True

    if contains_any_prohibited_term(text):
        deleted = delete_message(vk, message)
        muted = apply_mute(vk, config.CHAT_PEER_ID, from_id)
        send_mute_reason(vk, from_id, config.PROSTITUTION_MUTE_REASON)
        log.warning(
            "Нарушение: запрещённая лексика user=%s deleted=%s muted=%s",
            from_id, deleted, muted,
        )
        return True

    return False



def classify_media(message):
    counters = {"audio": 0, "voice": 0, "video": 0, "image": 0}
    for attachment in message_attachments(message):
        attachment_type = str(attachment.get("type", "") if isinstance(attachment, dict) else getattr(attachment, "type", "")).lower()
        if attachment_type == "audio": counters["audio"] = 1
        elif attachment_type == "audio_message": counters["voice"] = 1
        elif attachment_type == "video": counters["video"] = 1
        elif attachment_type == "photo": counters["image"] = 1
    return counters


def format_delta(current, baseline):
    if baseline <= 0: return "→ 0%"
    delta = (current - baseline) / baseline * 100
    arrow = "↑" if delta > 0.05 else "↓" if delta < -0.05 else "→"
    return f"{arrow} {abs(delta):.0f}%"


def publish_hourly_activity(vk, conn, hour_start):
    stats = db.build_hourly_stats(conn, hour_start)
    user_ids = [uid for uid, _ in stats["top_users"]]
    names = {}
    if user_ids:
        try:
            profiles = vk.users.get(user_ids=",".join(str(x) for x in user_ids))
            names = {int(p["id"]): f"{p.get('first_name','')} {p.get('last_name','')}".strip() for p in profiles}
        except ApiError:
            pass
    start = datetime.fromtimestamp(hour_start).strftime("%H:%M")
    end = datetime.fromtimestamp(hour_start + 3600).strftime("%H:%M")
    medals = ("🥇", "🥈", "🥉")
    top = "\n".join(f"{medals[i]} {names.get(uid, f'ID {uid}')} — {count} сообщ." for i,(uid,count) in enumerate(stats["top_users"])) or "Пока нет сообщений"
    report = (
        f"📊 АКТИВНОСТЬ ЧАСА\n{start} — {end}\n\n"
        f"🔥 Сообщения — {stats['activity_score']:.0f}/100 {format_delta(stats['total_messages'], stats['baseline_messages'])}\n"
        f"Всего: {stats['total_messages']}\n"
        f"🎵 Аудио: {stats['audio_messages']}\n"
        f"🎙 Голосовые: {stats['voice_messages']}\n"
        f"🎬 Видео: {stats['video_messages']}\n"
        f"🖼 Изображения: {stats['image_messages']}\n\n"
        f"💬 Активность — {stats['user_activity_score']:.0f}/100 {format_delta(stats['unique_users'], stats['baseline_users'])}\n"
        f"Уникальных пользователей: {stats['unique_users']}\n\n"
        f"🏆 ТОП-3 ЗА ЧАС\n{top}"
    )
    try:
        vk.messages.send(peer_id=config.BLOCKLIST_CHAT_PEER_ID, random_id=0, message=report)
        log.info("Опубликована статистика часа: %s", hour_start)
    except ApiError as err:
        log.warning("Не удалось отправить статистику часа: %s", err)


def activity_watchdog(vk, conn):
    db.ensure_hourly_stats_table(conn)
    last = None
    while True:
        now = int(time.time())
        current = now - now % 3600
        completed = current - 3600
        if completed != last:
            publish_hourly_activity(vk, conn, completed)
            last = completed
        time.sleep(20)

def handle_new_message(vk, conn, message) -> None:
    from_id = message_field(message, "from_id")
    peer_id = message_field(message, "peer_id")
    text = (
        message.get("text", "")
        if isinstance(message, dict)
        else getattr(message, "text", "") or ""
    )

    log.info(
        "MESSAGE_NEW получен: from_id=%s peer_id=%s text=%r",
        from_id,
        peer_id,
        text[:200],
    )

    if from_id <= 0:
        log.warning("MESSAGE_NEW пропущен: некорректный from_id=%s", from_id)
        return

    if peer_id == config.BLOCKLIST_CHAT_PEER_ID and from_id > 0:
        media = classify_media(message)
        db.record_hourly_message(
            conn, from_id, int(message_field(message, "date") or time.time()),
            audio=media["audio"], voice=media["voice"],
            video=media["video"], image=media["image"],
        )
        return

    if not is_target_chat(peer_id):
        log.info(
            "MESSAGE_NEW пропущен: peer_id=%s не является целевой беседой %s",
            peer_id,
            config.CHAT_PEER_ID,
        )
        return

    if from_id in config.ADMIN_IDS:
        log.info("MESSAGE_NEW пропущен: user=%s находится в ADMIN_IDS", from_id)
        return

    # Content rules are intentionally limited to the main moderation chat.
    # They take priority over the generic one-message-per-hour rule.
    if handle_content_violation(vk, message, from_id):
        return

    now = int(time.time())

    for expired_user, expired_peer in db.clear_expired_mutes(conn, now):
        lift_mute(vk, expired_peer, expired_user)

    result = db.register_message(conn, from_id, peer_id, now)

    if result["already_muted"]:
        delete_message(vk, message)
        muted = apply_mute(vk, peer_id, from_id)
        if muted:
            send_mute_reason(vk, from_id, config.LIMIT_MUTE_REASON)
        return

    if not result["should_mute"]:
        log.info("Сообщение принято: user=%s peer=%s", from_id, peer_id)
        return

    deleted = delete_message(vk, message)
    muted = apply_mute(vk, peer_id, from_id)

    if muted:
        send_mute_reason(vk, from_id, config.LIMIT_MUTE_REASON)

    log.warning(
        "Нарушение: user=%s peer=%s warnings=%s mute_until=%s "
        "vk_mute=%s message_deleted=%s",
        from_id,
        peer_id,
        result["warnings"],
        result["mute_until"],
        muted,
        deleted,
    )


def run_forever() -> None:
    conn = db.connect()
    log.info("База готова: %s", config.DB_PATH)

    session = vk_api.VkApi(token=config.VK_TOKEN, api_version=config.API_VERSION)
    vk = session.get_api()

    log.info(
        "Бот запущен: GROUP_ID=%s, moderation_chat=%s, blocklist_chat=%s",
        config.GROUP_ID,
        config.CHAT_PEER_ID,
        config.BLOCKLIST_CHAT_PEER_ID,
    )
    log.info(
        "Лимит: 1 сообщение в час; второе удаляется и пользователь получает мут на 1 час"
    )
    log.info(
        "Автоудаление из blocklist-чата включено для %s пользователей",
        len(config.BLOCKED_USER_IDS),
    )

    if config.BLOCKED_USER_IDS:
        threading.Thread(
            target=blocklist_watchdog,
            args=(vk,),
            name="blocklist-watchdog",
            daemon=True,
        ).start()

    threading.Thread(target=activity_watchdog, args=(vk, conn), name="activity-watchdog", daemon=True).start()

    try:
        while True:
            try:
                longpoll = VkBotLongPoll(session, config.GROUP_ID, wait=25)
                log.info("Long Poll подключён")

                for event in longpoll.listen():
                    log.info(
                        "VK event: type=%s group_id=%s",
                        getattr(event, "type", None),
                        getattr(event, "group_id", None),
                    )

                    # vk_api 11.9.9 does not expose CHAT_UPDATE in
                    # VkBotEventType, while VK Long Poll sends it as "chat_update".
                    event_type = getattr(event, "type", None)
                    event_type_value = getattr(event_type, "value", event_type)

                    if event_type_value == "chat_update":
                        handle_chat_update(vk, event)
                        continue

                    if event_type != VkBotEventType.MESSAGE_NEW and event_type_value != "message_new":
                        continue

                    message = getattr(event, "message", None)

                    if message is None:
                        obj = getattr(event, "obj", None)
                        nested = (
                            obj.get("message")
                            if isinstance(obj, dict)
                            else getattr(obj, "message", None)
                        )
                        message = nested if nested is not None else obj

                    if message is None:
                        log.warning(
                            "MESSAGE_NEW получен, но payload сообщения отсутствует: %r",
                            getattr(event, "raw", event),
                        )
                        continue

                    handle_new_message(vk, conn, message)

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
    finally:
        conn.close()


if __name__ == "__main__":
    run_forever()
