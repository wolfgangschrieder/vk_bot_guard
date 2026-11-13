"""Google Sheets content and Open-Meteo daily forecast for the morning post."""

import json
import logging
import os
from pathlib import Path

import requests

import config

log = logging.getLogger("vk_bot_guard.morning")

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)

WEATHER_CODES = {
    0: ("☀️", "ясно"),
    1: ("🌤", "преимущественно ясно"),
    2: ("⛅", "переменная облачность"),
    3: ("☁️", "облачно"),
    45: ("🌫", "туман"),
    48: ("🌫", "изморозь и туман"),
    51: ("🌦", "слабая морось"),
    53: ("🌦", "морось"),
    55: ("🌧", "сильная морось"),
    56: ("🌧", "ледяная морось"),
    57: ("🌧", "сильная ледяная морось"),
    61: ("🌧", "слабый дождь"),
    63: ("🌧", "дождь"),
    65: ("🌧", "сильный дождь"),
    66: ("🌧", "ледяной дождь"),
    67: ("🌧", "сильный ледяной дождь"),
    71: ("🌨", "слабый снег"),
    73: ("🌨", "снег"),
    75: ("❄️", "сильный снег"),
    77: ("🌨", "снежные зёрна"),
    80: ("🌦", "слабый ливень"),
    81: ("🌧", "ливень"),
    82: ("🌧", "сильный ливень"),
    85: ("🌨", "слабый снежный ливень"),
    86: ("❄️", "сильный снежный ливень"),
    95: ("⛈", "гроза"),
    96: ("⛈", "гроза с градом"),
    99: ("⛈", "сильная гроза с градом"),
}


def _active(value) -> bool:
    if value is None or str(value).strip() == "":
        return True
    return str(value).strip().casefold() not in {"false", "0", "нет", "no", "off", "неактивно"}


def parse_sheet_rows(values, content_type: str) -> list[dict[str, str]]:
    if not values:
        return []
    header = [str(cell).strip().casefold() for cell in values[0]]
    indexes = {name: index for index, name in enumerate(header)}
    text_index = indexes.get("текст", 1)
    id_index = indexes.get("id", 0)
    active_index = indexes.get("активно", 2)
    category_index = indexes.get("категория", 3)
    result = []
    for row in values[1:]:
        def cell(index):
            return str(row[index]).strip() if index < len(row) and row[index] is not None else ""

        text = cell(text_index)
        if not text or not _active(cell(active_index)):
            continue
        result.append({
            "id": cell(id_index),
            "text": text,
            "category": cell(category_index),
            "type": content_type,
        })
    return result


def _read_cache() -> dict[str, list[dict[str, str]]]:
    try:
        value = json.loads(Path(config.MORNING_CONTENT_CACHE_PATH).read_text(encoding="utf-8"))
        return {
            "advice": value.get("advice", []) if isinstance(value, dict) else [],
            "question": value.get("question", []) if isinstance(value, dict) else [],
        }
    except (OSError, ValueError, TypeError):
        return {"advice": [], "question": []}


def _write_cache(contents: dict[str, list[dict[str, str]]]) -> None:
    path = Path(config.MORNING_CONTENT_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(contents, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _google_access_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        str(config.GOOGLE_SERVICE_ACCOUNT_FILE), scopes=SCOPES
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("Google не выдал токен доступа для сервисного аккаунта.")
    return credentials.token


def _fetch_sheet_tab(session: requests.Session, token: str, title: str, content_type: str):
    url = f"{SHEETS_API}/{config.MORNING_GOOGLE_SHEET_ID}/values/{requests.utils.quote(title, safe='')}!A:D"
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        timeout=10,
    )
    response.raise_for_status()
    return parse_sheet_rows(response.json().get("values", []), content_type)


def load_content(session=None) -> dict[str, list[dict[str, str]]]:
    cached = _read_cache()
    if not config.MORNING_GOOGLE_SHEET_ID or not config.GOOGLE_SERVICE_ACCOUNT_FILE.is_file():
        return cached
    session = session or requests.Session()
    try:
        token = _google_access_token()
        fresh = {
            "advice": _fetch_sheet_tab(session, token, "Советы", "advice"),
            "question": _fetch_sheet_tab(session, token, "Вопросы", "question"),
        }
        _write_cache(fresh)
        return fresh
    except Exception:
        log.exception("Не удалось прочитать Google Таблицу; использую локальный кеш.")
        return cached


def fetch_weather(session=None) -> str | None:
    session = session or requests.Session()
    params = {
        "latitude": config.MORNING_LATITUDE,
        "longitude": config.MORNING_LONGITUDE,
        "daily": ",".join((
            "weather_code", "temperature_2m_min", "temperature_2m_max",
            "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max",
        )),
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "timezone": config.CHAT_TIMEZONE,
        "forecast_days": 1,
    }
    try:
        response = session.get(WEATHER_API, params=params, timeout=10)
        response.raise_for_status()
        daily = response.json().get("daily", {})
        code = int(daily["weather_code"][0])
        low = round(float(daily["temperature_2m_min"][0]))
        high = round(float(daily["temperature_2m_max"][0]))
        probability = daily.get("precipitation_probability_max", [None])[0]
        precip = daily.get("precipitation_sum", [None])[0]
        wind = round(float(daily["wind_speed_10m_max"][0]))
        icon, description = WEATHER_CODES.get(code, ("🌤", "погодные условия уточняются"))
        if probability is not None and float(probability) > 0:
            precipitation = f"осадки: {round(float(probability))}%"
        elif precip is not None and float(precip) > 0:
            precipitation = f"осадки: {float(precip):g} мм"
        else:
            precipitation = "без осадков"
        temperature = f"{low:+d}…{high:+d}°C"
        return (
            f"{icon} {config.MORNING_CITY}: {temperature}, {description}, {precipitation}, "
            f"ветер до {wind} км/ч. (Open-Meteo)"
        )
    except Exception:
        log.warning("Не удалось получить прогноз погоды.", exc_info=True)
        return None
