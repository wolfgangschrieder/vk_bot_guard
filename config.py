import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"Не задана переменная {name}. Скопируйте .env.example в .env и заполните её."
        )
    return value


VK_TOKEN = _required("VK_TOKEN")

try:
    GROUP_ID = int(_required("GROUP_ID"))
except ValueError as exc:
    raise SystemExit("GROUP_ID должен быть целым числом.") from exc

try:
    CHAT_PEER_ID = int(_required("CHAT_PEER_ID"))
except ValueError as exc:
    raise SystemExit("CHAT_PEER_ID должен быть целым числом.") from exc

try:
    MODERATION_CHAT_PEER_ID = int(_required("MODERATION_CHAT_PEER_ID"))
except ValueError as exc:
    raise SystemExit("MODERATION_CHAT_PEER_ID должен быть целым числом.") from exc

try:
    BLOCKLIST_CHAT_PEER_ID = int(os.getenv("BLOCKLIST_CHAT_PEER_ID", "0").strip() or "0")
except ValueError as exc:
    raise SystemExit("BLOCKLIST_CHAT_PEER_ID должен быть целым числом.") from exc

if CHAT_PEER_ID < 2_000_000_000:
    raise SystemExit(
        "CHAT_PEER_ID должен быть peer_id VK-беседы (обычно начинается с 2000000000)."
    )

if MODERATION_CHAT_PEER_ID < 2_000_000_000:
    raise SystemExit(
        "MODERATION_CHAT_PEER_ID должен быть peer_id VK-беседы."
    )
if MODERATION_CHAT_PEER_ID == CHAT_PEER_ID:
    raise SystemExit(
        "CHAT_PEER_ID и MODERATION_CHAT_PEER_ID должны быть разными чатами."
    )

if BLOCKLIST_CHAT_PEER_ID and BLOCKLIST_CHAT_PEER_ID < 2_000_000_000:
    raise SystemExit(
        "BLOCKLIST_CHAT_PEER_ID должен быть peer_id VK-беседы или 0 для отключения."
    )

ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
}

BLOCKED_USER_IDS = {
    int(part.strip())
    for part in os.getenv("BLOCKED_USER_IDS", "").split(",")
    if part.strip().isdigit()
}

RATE_LIMIT_SECONDS = 60 * 60
MUTE_SECONDS = 60 * 60
PROFILE_COOLDOWN_SECONDS = 3 * 24 * 60 * 60
BOT_REASON_DELETE_SECONDS = 10 * 60
ALL_COMMAND_DELETE_SECONDS = 60
MODERATION_REASON_DELETE_SECONDS = 60
SCHEDULER_INTERVAL_SECONDS = 20

API_VERSION = "5.199"
RECONNECT_DELAY = 5
DB_PATH = BASE_DIR / "moderator.db"
LOG_PATH = BASE_DIR / "bot.log"

CHAT_TIMEZONE = os.getenv("CHAT_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
try:
    CHAT_TZ = ZoneInfo(CHAT_TIMEZONE)
except Exception as exc:
    raise SystemExit(
        f"Некорректный CHAT_TIMEZONE={CHAT_TIMEZONE!r}. "
        "Используйте IANA timezone, например Europe/Moscow."
    ) from exc


# Existing content moderation rules — applied only to CHAT_PEER_ID.
PROHIBITED_LEXICON = (
    "дорого",
    "цена",
    "стоимость",
    "мп",
    "деньги",
    "не бюджет",
    "без предоплаты",
    "скидки",
    "порно",
    "продам видео",
    "порновидео",
    "домашнее видео",
)

PRICE_PATTERNS = (
    r"\b\d{1,3}\s*(?:тыс|тыщ|тысячи|тысяч|к)\b",
)

POLITICAL_TERMS = (
    "украина",
    "хохлы",
    "ауе",
)

LIMIT_MUTE_REASON = (
    "⏳ Лимит: 1 сообщение в час. Сообщение удалено, доступ ограничен на 1 час."
)

PROSTITUTION_MUTE_REASON = (
    "🚫 Проституция здесь запрещена. Сообщение удалено, доступ ограничен на 1 час."
)

MEDIA_MUTE_REASON = (
    "🎬 Музыка и видео в чате запрещены. Сообщение удалено, доступ ограничен на 1 час."
)

STICKER_MUTE_REASON = (
    "🚫 Стикеры в этом чате запрещены. Сообщение удалено, доступ ограничен на 1 час."
)

ALL_COMMAND_MUTE_REASON = (
    "@all -Здесь такие команды запрещены"
)

PORN_SALE_MUTE_REASON = (
    "🔞 Продажа порно запрещена. Сообщение удалено, доступ ограничен на 1 час."
)

CARD_PHONE_MUTE_REASON = (
    "💳 Публикация банковских карт и номеров телефонов запрещена. "
    "Сообщение удалено, доступ ограничен на 1 час."
)

POLITICAL_MUTE_REASON = (
    "🚫 Политические высказывания в чате запрещены. "
    "Сообщение удалено, доступ ограничен на 1 час."
)

KING_MUTE_MESSAGE = "🦸 Герой чата выдал мут пользователю {mention} на 1 час."

STAT_ALLOWED_LOGINS = {
    "piterparker34",
    "kenaya",
    "id1122341522",
}

MONTH_NAMES = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
