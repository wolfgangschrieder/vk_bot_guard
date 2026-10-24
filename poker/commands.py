"""VK-neutral command dispatcher for ASTRA Poker Room."""
from secrets import randbelow
from time import time
from .engine import Table
from .private_cards import format_hole_cards


HELP = (
    "♠ ASTRA POKER ROOM\n"
    "/poker — стол, /join — занять место, /leave — выйти, "
    "/ready — готов, /unready — отменить готовность, "
    "/start — начать раздачу, /table — состояние, "
    "/next — следующая раздача, /newsession — новая сессия (10000 фишек), "
    "/fold, /check, /call, /raise СУММА, /allin — действия."
)


def format_table(table):
    if table is None:
        return "♠ Стол ещё не создан. Напишите /poker."
    lines = [f"♠ ASTRA POKER | {table.stage.upper()} | Раздача #{table.hand_number}"]
    for p in table.seated():
        flags = []
        if p.user_id == table.owner_id:
            flags.append("👑 владелец")
        if table.stage == "waiting":
            flags.append("готов" if p.ready else "ожидает")
        elif p.folded:
            flags.append("пас")
        elif p.all_in:
            flags.append("all-in")
        if table.turn == p.user_id:
            flags.append("▶ ход")
        lines.append(
            f"Место {p.seat}: [id{p.user_id}|Игрок] — {p.stack} фишек"
            + (f" ({', '.join(flags)})" if flags else "")
        )
    lines.append(f"Банк: {sum(p.committed for p in table.players.values())}")
    if table.board:
        lines.append("Общие карты: " + " ".join(table.board))
    if table.stage in ("preflop", "flop", "turn", "river"):
        lines.append(f"Ставка раунда: {table.current_bet}")
    return "\n".join(lines)


def parse_command(text):
    parts = text.strip().lower().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0]
    supported = {
        "/poker", "/pokerhelp", "/join", "/leave", "/ready", "/unready",
        "/start", "/table", "/next", "/newsession", "/fold", "/check", "/call", "/raise", "/allin",
    }
    if cmd not in supported:
        return None
    if cmd == "/raise":
        if len(parts) != 2 or not parts[1].isdecimal():
            raise ValueError("Используйте /raise СУММА, например /raise 500")
        return ("raise", int(parts[1]))
    if len(parts) != 1:
        raise ValueError("Лишние аргументы команды")
    return (cmd[1:], None)


class PokerCommands:
    def __init__(self, store, room_id, private_delivery=None):
        self.store = store
        self.room_id = room_id
        self.private_delivery = private_delivery
        self.last_public_queued = False

    def _deliver_and_report(self, table):
        before_total, before_delivered = self.store.private_delivery_status(
            self.room_id, table.hand_number
        )
        try:
            self.private_delivery.deliver_pending(
                self.store, self.room_id, table.hand_number
            )
        except Exception:
            return "⏳ Раздача сохранена, доставка карт не завершена. Игра приостановлена."
        total, delivered = self.store.private_delivery_status(
            self.room_id, table.hand_number
        )
        if total != len(table.players) or delivered != total:
            return "⏳ Ожидаем доставки карт."
        if before_delivered < total and table.turn:
            revision, persisted = self.store.load(self.room_id)
            if persisted is not None and persisted.hand_number == table.hand_number:
                persisted.turn_started_at = time()
                self.store.save(self.room_id, persisted, revision)
                table.turn_started_at = persisted.turn_started_at
        return format_table(table)

    def process_expired_turn(self, now=None):
        """Apply and persist at most one expired action for this room.

        Invoke from a serialized scheduler; returns a chat-safe update or None.
        """
        revision, table = self.store.load(self.room_id)
        if table is None or table.stage not in ("preflop", "flop", "turn", "river"):
            return None
        total, delivered = self.store.private_delivery_status(
            self.room_id, table.hand_number
        )
        if total != len(table.players) or delivered != total:
            return None
        # Legacy snapshots may not have a timestamp; initialize the clock
        # without immediately penalizing a player after a restart.
        if table.turn and not table.turn_started_at:
            from time import time
            table.turn_started_at = time() if now is None else now
            self.store.save(self.room_id, table, revision)
            return None
        result = table.expire_turn(now)
        if result is None:
            return None
        user_id, action = result
        response = (
            f"⏱ [id{user_id}|Игрок] не сделал ход за "
            f"{table.turn_timeout_seconds} секунд: {action.upper()}.\\n"
            + format_table(table)
        )
        self.store.save(self.room_id, table, revision, public_message=response)
        self.last_public_queued = True
        return response

    def handle(self, user_id, text, event_id):
        """Return a chat reply, or None for a non-poker message.

        Caller MUST serialize invocations per room. Do not publish a reply
        if the transaction fails. No live hand starts without private delivery.
        """
        self.last_public_queued = False
        if event_id is not None and self.store.is_processed(self.room_id, event_id):
            return None
        try:
            parsed = parse_command(text)
        except ValueError as exc:
            return str(exc)
        if parsed is None:
            return None
        cmd, amount = parsed
        if cmd == "pokerhelp":
            return HELP
        revision, table = self.store.load(self.room_id)
        if table is None:
            table = Table()
        if cmd in ("poker", "table"):
            if table.stage in ("preflop", "flop", "turn", "river") and self.private_delivery is not None:
                total, delivered = self.store.private_delivery_status(
                    self.room_id, table.hand_number
                )
                if total == len(table.players) and delivered < total:
                    return self._deliver_and_report(table)
                if total != len(table.players):
                    return "⚠️ Неполная очередь карт. Игра приостановлена."
            return format_table(table)
        try:
            if cmd == "join":
                if self.private_delivery is not None:
                    try:
                        permitted = self.private_delivery.can_message(user_id)
                    except Exception:
                        permitted = False
                    if not permitted:
                        return ("⚠️ Разрешите личные сообщения от сообщества Астра "
                                "и повторите /join, иначе закрытые карты не придут.")
                table.join(user_id)
            elif cmd == "leave":
                table.leave(user_id)
            elif cmd in ("ready", "unready"):
                table.ready(user_id, cmd == "ready")
            elif cmd == "start":
                if user_id not in table.players:
                    raise ValueError("Only seated players may start a hand")
                if self.private_delivery is None:
                    return "Раздача пока отключена: личная доставка карт не настроена."
                for player in table.seated():
                    try:
                        permitted = self.private_delivery.can_message(player.user_id)
                    except Exception:
                        permitted = False
                    if not permitted:
                        return "⚠️ Каждый игрок должен разрешить сообщения от сообщества."
                table.start()
                messages = {
                    p.user_id: (
                        randbelow(2147483647) + 1,
                        format_hole_cards(p.hole, table.hand_number),
                    )
                    for p in table.seated()
                }
                saved = self.store.save_hand_with_private_cards(
                    self.room_id, table, revision, messages, event_id=event_id,
                    public_message=(
                        f"♠ Раздача #{table.hand_number} началась. "
                        "Закрытые карты отправляются в личные сообщения."
                    ),
                )
                if saved is None:
                    return None
                self.last_public_queued = True
                return self._deliver_and_report(table)
            elif cmd == "next":
                if user_id not in table.players:
                    raise ValueError("Only seated players may advance the game")
                table.reset()
            elif cmd == "newsession":
                if user_id != table.owner_id:
                    raise ValueError("Only the table owner may reset the session")
                table.new_session()
            elif cmd in ("fold", "check", "call", "raise", "allin"):
                if self.private_delivery is None:
                    return "Игровые действия пока отключены."
                total, delivered = self.store.private_delivery_status(
                    self.room_id, table.hand_number
                )
                if total != len(table.players) or delivered != total:
                    return "⏳ Доставка закрытых карт ещё не завершена."
                table.act(user_id, cmd, amount)
            else:
                return HELP
            response = format_table(table)
            saved = self.store.save(
                self.room_id, table, revision, event_id=event_id,
                public_message=response,
            )
            if saved is None:
                return None
            self.last_public_queued = True
        except (ValueError, KeyError) as exc:
            return f"⚠️ {exc}"
        return format_table(table)
