import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import morning_content


class MorningContentTests(unittest.TestCase):
    def test_parse_sheet_rows_reads_date_advice_question_columns(self):
        values = [
            ["Дата", "Совет", "Вопрос"],
            ["2026-11-14", "Начни с небольшого дела.", "Что тебя сегодня порадует?"],
            ["15.11.2026", "Не забывай отдыхать.", ""],
            ["не дата", "Пропустить", "Не загружать"],
            ["2026-11-16", "", ""],
        ]
        result = morning_content.parse_sheet_rows(values)
        self.assertEqual(result, [
            {"date": "2026-11-14", "advice": "Начни с небольшого дела.",
             "question": "Что тебя сегодня порадует?"},
            {"date": "2026-11-15", "advice": "Не забывай отдыхать.", "question": ""},
        ])

    def test_content_is_selected_by_exact_date(self):
        rows = morning_content.parse_sheet_rows([
            ["Дата", "Совет", "Вопрос"],
            ["2026-11-14", "Совет на 14-е", "Вопрос на 14-е"],
            ["2026-11-15", "Совет на 15-е", ""],
        ])
        self.assertEqual(morning_content._for_date(rows, date(2026, 11, 15)), {
            "advice": [{"id": "2026-11-15", "text": "Совет на 15-е"}],
            "question": [],
        })
        self.assertEqual(morning_content._for_date(rows, date(2026, 11, 16)), {
            "advice": [], "question": [],
        })

    def test_project_news_parser_reads_date_time_and_text(self):
        rows = morning_content.parse_news_rows([
            ["Дата и время (дд/мм/гггг чч:мм)", "Текст новости"],
            ["14/11/2026 12:30", "Запускаем новую функцию!"],
            ["15.11.2026 09:05", "Вторая новость"],
            ["bad date", "Пропустить"],
            ["16/11/2026 10:00", ""],
        ])
        self.assertEqual(rows, [
            {"datetime": "2026-11-14T12:30", "text": "Запускаем новую функцию!"},
            {"datetime": "2026-11-15T09:05", "text": "Вторая новость"},
        ])

    def test_weather_forecast_is_formatted_in_russian(self):
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {"daily": {
            "weather_code": [61], "temperature_2m_min": [2.2], "temperature_2m_max": [8.7],
            "precipitation_probability_max": [45], "precipitation_sum": [1.4],
            "wind_speed_10m_max": [18.2],
        }}
        session.get.return_value = response
        with patch.object(config, "MORNING_CITY", "Волгоград"):
            result = morning_content.fetch_weather(session)
        self.assertIn("Волгоград: +2…+9°C", result)
        self.assertIn("слабый дождь", result)
        self.assertIn("осадки: 45%", result)
        self.assertIn("ветер до 18 км/ч", result)
        self.assertIn("Open-Meteo", result)
        self.assertEqual(session.get.call_args.kwargs["timeout"], 10)

    def test_weather_failure_returns_none(self):
        session = MagicMock()
        session.get.side_effect = OSError("network down")
        self.assertIsNone(morning_content.fetch_weather(session))

    def test_content_uses_local_cache_when_service_account_key_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "content.json"
            cache.write_text(json.dumps({"rows": [
                {"date": "2026-11-14", "advice": "Совет", "question": "Вопрос"},
            ]}), encoding="utf-8")
            with patch.object(config, "MORNING_CONTENT_CACHE_PATH", cache), \
                    patch.object(config, "GOOGLE_SERVICE_ACCOUNT_FILE", Path(tmp) / "missing.json"):
                result = morning_content.load_content(date(2026, 11, 14))
        self.assertEqual(result["advice"][0]["text"], "Совет")
        self.assertEqual(result["question"][0]["text"], "Вопрос")


if __name__ == "__main__":
    unittest.main()
