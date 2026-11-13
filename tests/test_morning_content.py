import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config
import morning_content


class MorningContentTests(unittest.TestCase):
    def test_parse_sheet_rows_uses_headers_and_ignores_disabled_or_empty_rows(self):
        values = [
            ["ID", "Текст", "Активно", "Категория"],
            ["tip-1", "Пейте воду", "TRUE", "здоровье"],
            ["tip-2", "Не показывать", "FALSE", "прочее"],
            ["tip-3", "", "TRUE", ""],
            ["tip-4", "Проветри комнату", "", "дом"],
        ]
        result = morning_content.parse_sheet_rows(values, "advice")
        self.assertEqual([item["id"] for item in result], ["tip-1", "tip-4"])
        self.assertEqual(result[0]["category"], "здоровье")
        self.assertEqual(result[1]["type"], "advice")

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
            cache.write_text(json.dumps({"advice": [{"id": "a", "text": "Совет"}],
                                         "question": []}), encoding="utf-8")
            with patch.object(config, "MORNING_CONTENT_CACHE_PATH", cache), \
                    patch.object(config, "GOOGLE_SERVICE_ACCOUNT_FILE", Path(tmp) / "missing.json"):
                result = morning_content.load_content()
        self.assertEqual(result["advice"][0]["text"], "Совет")
        self.assertEqual(result["question"], [])
