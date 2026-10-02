import logging
import random
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


def is_chat(peer_id: int) -> bool:
    return peer_id >= CHAT_PEER_START


def send_message(vk, peer_id: int, text: str) -> None:
    vk.messages.send(
        peer_id=peer_id,
        message=text,
        random_id=random.randint(1, 2_147_483_647),
    )


def apply_mute(vk, peer_id: int, user_id: int) -> bool:
    try:
        vk.method(
            "messages.changeConversationMemberRestrictions",
            {
                "peer_id": peer_id,
                "member_ids": str(user_id),
                "action": "ro",
                "for": config.MUTE_SECONDS,
            },
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
        vk.method(
            "messages.changeConversationMemberRestrictions",
            {
                "peer_id": peer_id,
                "member_ids": str(user_id),
                "action": "rw",
            },
        )
        log.info("Мут снят: user=%s peer=%s", user_id, peer_id)
    except ApiError as err:
        log.warning(
            "Не удалось снять мут user=%s peer=%s: %s",
            user_id,
            peer_id,
            err,
        )


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


def handle_new_message(vk, conn, message) -> None:
    from_id = message_field(message, "from_id")
    peer_id = message_field(message, "peer_id")
    text = message.get("text", "") if isinstance(message, dict) else getattr(message, "text", "") or ""

    # Log before filtering so we can prove VK delivered the message event.
    log.info(
        "MESSAGE_NEW получен: from_id=%s peer_id=%s text=%r",
        from_id,
        peer_id,
        text[:200],
    )

    if from_id <= 0:
        log.warning("MESSAGE_NEW пропущен: некорректный from_id=%s", from_id)
        return

    if not is_chat(peer_id):
        log.warning(
            "MESSAGE_NEW пропущен: peer_id=%s не похож на беседу (ожидался >= %s)",
            peer_id,
            CHAT_PEER_START,
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
        # VK normally blocks delivery while the restriction is active.
        # Re-applying is a safety net if an event still reaches the bot.
        apply_mute(vk, peer_id, from_id)
        return

    if not result["should_mute"]:
        log.info("Сообщение принято: user=%s peer=%s", from_id, peer_id)
        return

    muted = apply_mute(vk, peer_id, from_id)

    if muted:
        text = (
            f"[id{from_id}|Пользователь], слишком часто пишете.\n"
            "Лимит: 1 сообщение в 1 час.\n"
            f"Мут на {config.MUTE_SECONDS // 3600} часов."
        )
    else:
        text = (
            f"[id{from_id}|Пользователь], превышен лимит: "
            "не чаще 1 сообщения в 1 час.\n"
            "Мут не применён: проверьте права сообщества в беседе."
        )

    try:
        send_message(vk, peer_id, text)
    except ApiError as err:
        log.error("Не удалось отправить уведомление: %s", err)

    log.warning(
        "Нарушение: user=%s peer=%s warnings=%s mute_until=%s vk_ok=%s",
        from_id,
        peer_id,
        result["warnings"],
        result["mute_until"],
        muted,
    )


def run_forever() -> None:
    conn = db.connect()
    log.info("База готова: %s", config.DB_PATH)

    session = vk_api.VkApi(token=config.VK_TOKEN, api_version=config.API_VERSION)
    vk = session.get_api()

    log.info(
        "Бот запущен: GROUP_ID=%s, limit=1/hour, mute=24h",
        config.GROUP_ID,
    )
    log.info("Ожидаются сообщения из VK-бесед: peer_id >= %s", CHAT_PEER_START)

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

                    if event.type != VkBotEventType.MESSAGE_NEW:
                        continue

                    # vk_api exposes the actual MESSAGE_NEW payload as event.message
                    # and also as event.obj.message.
                    message = getattr(event, "message", None)

                    if message is None:
                        obj = getattr(event, "obj", None)
                        nested = obj.get("message") if isinstance(obj, dict) else getattr(obj, "message", None)
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
