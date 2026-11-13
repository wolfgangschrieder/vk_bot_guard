"""Google Sheets content and Open-Meteo daily forecast for the morning post."""

import json
import logging
import os
from pathlib import Path
from datetime import date, datetime

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


def _parse_date(value: str) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    for parser in (date.fromisoformat, lambda raw: datetime.strptime(raw, "%d.%m.%Y").date()):
        try:
            return parser(value).isoformat()
        except ValueError:
            pass
    return None


def parse_sheet_rows(values) -> list[dict[str, str]]:
    if not values:
        return []
    header = [str(cell).strip().casefold() for cell in values[0]]
    indexes = {name: index for index, name in enumerate(header)}
    date_index = indexes.get("дата")
    advice_index = indexes.get("совет")
    question_index = indexes.get("вопрос")
    if None in (date_index, advice_index, question_index):
        log.warning("В листе утреннего контента ожидаются заголовки: Дата, Совет, Вопрос.")
        return []
    result = []
    for row in values[1:]:
        def cell(index):
            return str(row[index]).strip() if index < len(row) and row[index] is not None else ""

        parsed_date = _parse_date(cell(date_index))
        advice = cell(advice_index)
        question = cell(question_index)
        if not parsed_date or not (advice or question):
            continue
        result.append({
            "date": parsed_date,
            "advice": advice,
            "question": question,
        })
    return result


def _read_cache() -> list[dict[str, str]]:
    try:
        value = json.loads(Path(config.MORNING_CONTENT_CACHE_PATH).read_text(encoding="utf-8"))
        rows = value.get("rows", []) if isinstance(value, dict) else []
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_cache(rows: list[dict[str, str]]) -> None:
    path = Path(config.MORNING_CONTENT_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
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


def _fetch_sheet_rows(session: requests.Session, token: str):
    title = "Утренний контент"
    url = f"{SHEETS_API}/{config.MORNING_GOOGLE_SHEET_ID}/values/{requests.utils.quote(title, safe='')}!A:C"
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        timeout=10,
    )
    response.raise_for_status()
    return parse_sheet_rows(response.json().get("values", []))


def _for_date(rows: list[dict[str, str]], stat_date: date) -> dict[str, list[dict[str, str]]]:
    day_key = stat_date.isoformat()
    row = next((item for item in rows if item.get("date") == day_key), None)
    if not row:
        return {"advice": [], "question": []}
    return {
        "advice": ([{"id": day_key, "text": row["advice"]}] if row.get("advice") else []),
        "question": ([{"id": day_key, "text": row["question"]}] if row.get("question") else []),
    }


def load_content(stat_date: date, session=None) -> dict[str, list[dict[str, str]]]:
    cached = _read_cache()
    if not config.MORNING_GOOGLE_SHEET_ID or not config.GOOGLE_SERVICE_ACCOUNT_FILE.is_file():
        return _for_date(cached, stat_date)
    session = session or requests.Session()
    try:
        token = _google_access_token()
        fresh = _fetch_sheet_rows(session, token)
        _write_cache(fresh)
        return _for_date(fresh, stat_date)
    except Exception:
        log.exception("Не удалось прочитать Google Таблицу; использую локальный кеш.")
        return _for_date(cached, stat_date)


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
