"""VK-neutral command dispatcher for ASTRA Poker Room.

This is intentionally NOT wired into bot.py yet. The integration layer must
supply a verified private-message delivery implementation before dealing.
"""
from .engine import Table


HELP = (
    "♠ ASTRA POKER ROOM\n"
    "/poker — стол, /join — занять место, /leave — выйти, "
    "/ready — готов, /unready — отменить готовность, "
    "/start — начать раздачу, /table — состояние, "
    "/fold, /check, /call, /raise СУММА, /allin — действия."
)


def format_table(table):
    if table is None:
        return "♠ Стол ещё не создан. Напишите /poker."
    lines = [f"♠ ASTRA POKER | {table.stage.upper()} | Раздача #{table.hand_number}"]
    for p in table.seated():
        flags = []
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
        "/start", "/table", "/fold", "/check", "/call", "/raise", "/allin",
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

    def handle(self, user_id, text, event_id):
        """Return a chat reply, or None for a non-poker message.

        Caller MUST serialize invocations per room. Do not publish a reply
        if the transaction fails. No live hand starts without private delivery.
        """
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
                if self.private_delivery is None:
                    return "Раздача пока отключена: личная доставка карт не настроена."
                # A reliable transactional outbox is required before activation.
                return "Раздача пока отключена до реализации надёжной доставки карт."
            elif cmd in ("fold", "check", "call", "raise", "allin"):
                if self.private_delivery is None:
                    return "Игровые действия пока отключены."
                table.act(user_id, cmd, amount)
            else:
                return HELP
            saved = self.store.save(self.room_id, table, revision, event_id=event_id)
            if saved is None:
                return None
        except (ValueError, KeyError) as exc:
            return f"⚠️ {exc}"
        return format_table(table)
