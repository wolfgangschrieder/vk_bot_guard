import logging
import sys
import time
import traceback

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

    now = int(time.time())

    for expired_user, expired_peer in db.clear_expired_mutes(conn, now):
        lift_mute(vk, expired_peer, expired_user)

    result = db.register_message(conn, from_id, peer_id, now)

    if result["already_muted"]:
        delete_message(vk, message)
        apply_mute(vk, peer_id, from_id)
        return

    if not result["should_mute"]:
        log.info("Сообщение принято: user=%s peer=%s", from_id, peer_id)
        return

    deleted = delete_message(vk, message)
    muted = apply_mute(vk, peer_id, from_id)

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

                    if event.type == VkBotEventType.CHAT_UPDATE:
                        handle_chat_update(vk, event)
                        continue

                    if event.type != VkBotEventType.MESSAGE_NEW:
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
