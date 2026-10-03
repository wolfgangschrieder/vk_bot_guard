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
