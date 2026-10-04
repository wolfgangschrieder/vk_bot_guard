import os
from pathlib import Path

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
    BLOCKLIST_CHAT_PEER_ID = int(_required("BLOCKLIST_CHAT_PEER_ID"))
except ValueError as exc:
    raise SystemExit("BLOCKLIST_CHAT_PEER_ID должен быть целым числом.") from exc

for name, peer_id in (
    ("CHAT_PEER_ID", CHAT_PEER_ID),
    ("BLOCKLIST_CHAT_PEER_ID", BLOCKLIST_CHAT_PEER_ID),
):
    if peer_id < 2_000_000_000:
        raise SystemExit(
            f"{name} должен быть peer_id VK-беседы (обычно начинается с 2000000000)."
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
API_VERSION = "5.199"
RECONNECT_DELAY = 5
DB_PATH = BASE_DIR / "moderator.db"
LOG_PATH = BASE_DIR / "bot.log"


# Content moderation rules — applied ONLY to CHAT_PEER_ID.
PROHIBITED_LEXICON = (
    "дорого",
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

# Price expressions commonly used in the same prohibited context:
# "10 тыщ", "20 тыщ", "10к", etc.
PRICE_PATTERNS = (
    r"\b\d{1,3}\s*(?:тыс|тыщ|тысячи|тысяч|к)\b",
)

LIMIT_MUTE_REASON = (
    "⏳ Лимит: 1 сообщение в час.\n"
    "Второе сообщение удалено. Доступ ограничен на 1 час.\n"
    "Увидимся позже 👋"
)

PROSTITUTION_MUTE_REASON = (
    "🚫 Проституция запрещена.\n"
    "Сообщение удалено из-за запрещённой лексики: «дорого», «мп», "
    "«деньги», «не бюджет», «без предоплаты», «скидки», «10 тыщ», «20 тыщ», «10к».\n"
    "Доступ ограничен."
)

MEDIA_MUTE_REASON = (
    "🎬 Музыка и видео в чате запрещены.\n"
    "Контент удалён. Доступ ограничен на 1 час.\n"
    "Спасибо за понимание 🙌"
)

PORN_SALE_MUTE_REASON = (
    "🔞 Продажа порно запрещена.\n"
    "Сообщение удалено из-за запрещённых слов: «порновидео», «домашнее видео».\n"
    "Доступ ограничен."
)
