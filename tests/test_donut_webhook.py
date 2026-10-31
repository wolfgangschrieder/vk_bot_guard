import http.client
import json
import tempfile
import unittest
from pathlib import Path

import database as db
import donut_webhook


class DonutWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "donut.db"
        with db.connect(str(self.db_path)):
            pass
        self.server = donut_webhook.start_server(
            host="127.0.0.1",
            port=0,
            group_id=123,
            secret="callback-secret",
            confirmation_code="confirm-me",
            db_path=self.db_path,
            minimum_amount=50,
        )
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, event):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = json.dumps(event).encode("utf-8")
        connection.request("POST", "/vk/donut", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, response.read().decode("utf-8")
        connection.close()
        return result

    def base_event(self, event_type, event_id="event-1", **obj):
        return {
            "type": event_type,
            "event_id": event_id,
            "group_id": 123,
            "secret": "callback-secret",
            "object": obj,
        }

    def test_confirmation_and_secret_group_validation(self):
        confirmation = self.base_event("confirmation")
        self.assertEqual(self.post(confirmation), (200, "confirm-me"))

        invalid_secret = dict(confirmation, secret="wrong")
        self.assertEqual(self.post(invalid_secret), (403, "forbidden"))

        invalid_group = dict(confirmation, group_id=999)
        self.assertEqual(self.post(invalid_group), (403, "wrong group"))

    def test_create_cancel_expire_and_replayed_callback(self):
        created = self.base_event("donut_subscription_create", user_id=777, amount=50)
        self.assertEqual(self.post(created), (200, "ok"))
        self.assertEqual(self.post(created), (200, "ok"))

        with db.connect(str(self.db_path)) as conn:
            self.assertTrue(db.is_premium_user(conn, 777))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM processed_donut_events").fetchone()[0], 1)

        cancelled = self.base_event("donut_subscription_cancelled", "event-2", user_id=777)
        self.assertEqual(self.post(cancelled), (200, "ok"))
        with db.connect(str(self.db_path)) as conn:
            self.assertTrue(db.is_premium_user(conn, 777))

        expired = self.base_event("donut_subscription_expired", "event-3", user_id=777)
        self.assertEqual(self.post(expired), (200, "ok"))
        with db.connect(str(self.db_path)) as conn:
            self.assertFalse(db.is_premium_user(conn, 777))


if __name__ == "__main__":
    unittest.main()
