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
PROFILE_DAILY_LIMIT_REGULAR = 2
PROFILE_DAILY_LIMIT_PREMIUM = 5
CHARACTERISTIC_WEEKLY_LIMIT_REGULAR = 1
CHARACTERISTIC_DAILY_LIMIT_PREMIUM = 5
REPUTATION_DAILY_LIMIT_PREMIUM = 3
BOT_REASON_DELETE_SECONDS = 10 * 60
SOCIAL_MUTE_NOTICE_DELETE_SECONDS = 5 * 60
ALL_COMMAND_DELETE_SECONDS = 60
MODERATION_REASON_DELETE_SECONDS = 60 * 60
SCHEDULER_INTERVAL_SECONDS = 20
MORNING_POST_HOUR = 8
MORNING_POST_LATE_WINDOW_MINUTES = 30
MORNING_CONTENT_HISTORY_DAYS = 30
DONUT_PROMO_HOUR = 12
DONUT_PROMO_LATE_WINDOW_MINUTES = 30
MARRIAGE_PROPOSAL_SECONDS = 24 * 60 * 60
MARRIAGE_PAIRS_PAGE_SIZE = 20
HISTORY_RETENTION_SECONDS = 30 * 24 * 60 * 60
SCHEDULER_CATCHUP_DAYS = 7
DELIVERY_MAX_ATTEMPTS = 12
SEND_MAX_AGE_SECONDS = 6 * 60 * 60

API_VERSION = "5.199"
RECONNECT_DELAY = 5
DB_PATH = BASE_DIR / "moderator.db"
LOG_PATH = BASE_DIR / "bot.log"
MORNING_GOOGLE_SHEET_ID = os.getenv(
    "MORNING_GOOGLE_SHEET_ID",
    "1us-ZoLGbyNYI-J6HQMHofHCPIdEcUes-Sj3KkKjqLyg",
).strip()
GOOGLE_SERVICE_ACCOUNT_FILE = Path(os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE", str(BASE_DIR / "google-service-account.json")
).strip())
MORNING_CONTENT_CACHE_PATH = BASE_DIR / "morning_content_cache.json"
PROJECT_NEWS_CACHE_PATH = BASE_DIR / "project_news_cache.json"
PROJECT_NEWS_REFRESH_SECONDS = 60
PROJECT_NEWS_LATE_WINDOW_MINUTES = 30
try:
    MORNING_LATITUDE = float(os.getenv("MORNING_LATITUDE", "48.708").strip())
    MORNING_LONGITUDE = float(os.getenv("MORNING_LONGITUDE", "44.513").strip())
except ValueError as exc:
    raise SystemExit("MORNING_LATITUDE и MORNING_LONGITUDE должны быть числами.") from exc
MORNING_CITY = os.getenv("MORNING_CITY", "Волгоград").strip() or "Волгоград"

try:
    PREMIUM_DONUT_MIN_AMOUNT_RUB = int(os.getenv("PREMIUM_DONUT_MIN_AMOUNT_RUB", "100").strip() or "100")
except ValueError as exc:
    raise SystemExit("PREMIUM_DONUT_MIN_AMOUNT_RUB должен быть целым числом.") from exc
if PREMIUM_DONUT_MIN_AMOUNT_RUB < 1:
    raise SystemExit("PREMIUM_DONUT_MIN_AMOUNT_RUB должен быть больше нуля.")

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
    "без предоплат",
    "расход",
    "коммерческой основе",
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

LINK_MUTE_REASON = (
    "Ссылки здесь запрещены. Сообщение удалено, доступ ограничен на 1 час."
)

PROHIBITED_WORDS_MUTE_REASON = (
    "Пожалуйста, избегайте обсуждения финансовых вопросов в этом чате."
)

MEDIA_MUTE_REASON = (
    "🎬 Музыка и видео в чате запрещены. Сообщение удалено, доступ ограничен на 1 час."
)

STICKER_MUTE_REASON = (
    "🚫 Стикеры в этом чате запрещены. Сообщение удалено, доступ ограничен на 1 час."
)

PORN_SALE_MUTE_REASON = (
    "🔞 Продажа порно запрещена. Сообщение удалено, доступ ограничен на 1 час."
)

CARD_PHONE_MUTE_REASON = (
    "💳 Публикация банковских карт и номеров телефонов запрещена. "
    "Сообщение удалено, доступ ограничен на 1 час."
)

SPAM_MUTE_REASON = (
    "Спам запрещен! Сообщение удалено! Доступ ограничен на 1 час!"
)

POLITICAL_MUTE_REASON = (
    "🚫 Политические высказывания в чате запрещены. "
    "Сообщение удалено, доступ ограничен на 1 час."
)

HERO_MUTE_MESSAGE = "🏅 {hero_title} выдал мут пользователю {mention} на {duration}."
RANK_MUTE_MESSAGE = "🎖️ Мут от ранга «{rank_title}»: {mention} ограничен на {duration}."

STAT_ALLOWED_LOGINS = {
    "piterparker34",
    "kenaya",
    "id1122341522",
}

# Duplicate names from the original list are intentionally represented once:
# each trait has one command and one signed score in a user's profile.
CHAT_CHARACTERISTICS = (
    ("интеллект", "🧠", "Интеллект"),
    ("эрудит", "🎓", "Эрудит"),
    ("стратег", "♟️", "Стратег"),
    ("мудрец", "🦉", "Мудрец"),
    ("аналитик", "🔬", "Аналитик"),
    ("гений", "💡", "Гений"),
    ("загадка", "🧩", "Загадка"),
    ("наблюдатель", "🦅", "Наблюдатель"),
    ("детектив", "🕵️", "Детектив"),
    ("википедия", "📚", "Википедия"),
    ("философ", "🧘", "Философ"),
    ("психолог", "🎭", "Психолог"),
    ("мемолог", "😂", "Мемолог"),
    ("клоун", "🤡", "Клоун"),
    ("комик", "🎭", "Комик"),
    ("мем", "🗿", "Мем"),
    ("приколист", "🧨", "Приколист"),
    ("шоумен", "🍿", "Шоумен"),
    ("драматург", "🎬", "Драматург"),
    ("комментатор", "🗣️", "Комментатор"),
    ("хаос", "🔥", "Хаос"),
    ("провокатор", "😈", "Провокатор"),
    ("ураган", "🌪️", "Ураган"),
    ("пожарный", "🧯", "Пожарный"),
    ("скрытный", "🐈", "Скрытный"),
    ("казанова", "💋", "Казанова"),
    ("флиртёр", "😏", "Флиртёр"),
    ("сердцеед", "💘", "Сердцеед"),
    ("лизун", "👅", "Лизун"),
    ("дрочун", "✊", "Дрочун"),
    ("скуф", "🧔‍♂️", "Скуф"),
    ("милашка", "🥰", "Милашка"),
    ("ангел", "🕊️", "Ангел"),
    ("манипулятор", "🐍", "Манипулятор"),
    ("актер", "🎭", "Актёр"),
    ("дипломат", "🗣️", "Дипломат"),
    ("миротворец", "🕊️", "Миротворец"),
    ("хитрец", "🦊", "Хитрец"),
    ("альфа", "🐺", "Альфа"),
    ("переговорщик", "🤝", "Переговорщик"),
    ("авторитет", "👑", "Авторитет"),
    ("джентльмен", "🎩", "Джентльмен"),
    ("бизнесмен", "🤑", "Бизнесмен"),
    ("аристократ", "👑", "Аристократ"),
    ("дуэлянт", "⚔️", "Дуэлянт"),
    ("токсик", "💀", "Токсик"),
    ("крыса", "🐀", "Крыса"),
    ("душнила", "🗿", "Душнила"),
    ("спамер", "📢", "Спамер"),
    ("лицемер", "🎭", "Лицемер"),
    ("скучный", "🥱", "Скучный"),
    ("эстет", "📸", "Эстет"),
    ("меломан", "🎧", "Меломан"),
    ("киноман", "🎬", "Киноман"),
    ("интеллигент", "☕", "Интеллигент"),
    ("гедонист", "🥂", "Гедонист"),
    ("романтик", "❤️", "Романтик"),
    ("тусовщик", "🥂", "Тусовщик"),
    ("артист", "🎤", "Артист"),
    ("маэстро", "🎼", "Маэстро"),
)
CHAT_CHARACTERISTIC_BY_COMMAND = {
    command: (emoji, title)
    for command, emoji, title in CHAT_CHARACTERISTICS
}
CHAT_CHARACTERISTIC_ALIASES = {
    "джентельмен": "джентльмен",
    "серцеед": "сердцеед",
    "актёр": "актер",
}

# Optional stable IDs for the same three accounts; avoids a VK lookup for /stat.
STAT_ALLOWED_IDS = {
    int(part.strip()) for part in os.getenv("STAT_ALLOWED_IDS", "").split(",")
    if part.strip().isdigit()
}
if STAT_ALLOWED_IDS and len(STAT_ALLOWED_IDS) != 3:
    raise SystemExit("STAT_ALLOWED_IDS должен содержать ровно три ID разрешённых аккаунтов.")

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
