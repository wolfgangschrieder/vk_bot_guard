"""Small HTTP receiver for VK Donut Callback API events."""

import hmac
import json
import logging
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import database as db

log = logging.getLogger("vk_bot_guard.donut")
MAX_BODY_BYTES = 1024 * 1024


def make_handler(*, group_id, secret, confirmation_code, db_path, minimum_amount):
    class DonutCallbackHandler(BaseHTTPRequestHandler):
        server_version = "VKDonutCallback/1.0"

        def log_message(self, _format, *args):
            log.info("Donut HTTP: " + _format, *args)

        def _reply(self, status, body):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, "invalid content length")
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(413 if length > MAX_BODY_BYTES else 400, "invalid body size")
                return
            try:
                event = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reply(400, "invalid json")
                return
            if not isinstance(event, dict):
                self._reply(400, "invalid event")
                return

            provided_secret = str(event.get("secret", ""))
            if not hmac.compare_digest(provided_secret, str(secret)):
                self._reply(403, "forbidden")
                return
            try:
                received_group = int(event.get("group_id", 0))
            except (TypeError, ValueError):
                received_group = 0
            if received_group != int(group_id):
                self._reply(403, "wrong group")
                return

            event_type = event.get("type")
            if event_type == "confirmation":
                self._reply(200, str(confirmation_code))
                return

            try:
                with closing(db.connect(str(db_path))) as conn:
                    result = db.process_donut_event(conn, event, minimum_amount)
            except Exception:
                log.exception("Не удалось обработать Donut callback")
                self._reply(500, "temporary error")
                return

            if result in {"invalid", "below_minimum"}:
                log.warning("Donut callback не выдал премиум: event=%s result=%s",
                            event.get("event_id", ""), result)
            elif result == "activated":
                log.info("Donut-премиум активирован: user=%s", event.get("object", {}).get("user_id"))
            elif result == "expired":
                log.info("Donut-премиум завершён: user=%s", event.get("object", {}).get("user_id"))
            elif result == "cancelled":
                log.info("Donut-подписка отменена; премиум сохранён до окончания срока: user=%s",
                         event.get("object", {}).get("user_id"))
            self._reply(200, "ok")

    return DonutCallbackHandler


def start_server(*, host, port, group_id, secret, confirmation_code, db_path, minimum_amount):
    """Bind the local webhook listener and serve it on a daemon thread."""
    handler = make_handler(
        group_id=group_id,
        secret=secret,
        confirmation_code=confirmation_code,
        db_path=db_path,
        minimum_amount=minimum_amount,
    )
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="donut-callback", daemon=True)
    thread.start()
    log.info("VK Donut Callback API слушает %s:%s", *server.server_address[:2])
    return server
