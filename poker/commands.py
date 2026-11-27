"""VK-neutral command dispatcher for ASTRA Poker Room."""
from secrets import randbelow
from time import time
from .engine import Table
from .evaluator import evaluate_seven
from .private_cards import format_hole_cards, display_card


HELP = (
    "♠ ASTRA POKER ROOM\n"
    "/poker — стол, /join — занять место, /leave — выйти, "
    "/кик @username — владелец может исключить игрока между раздачами, "
    "/ready — готов, /unready — отменить готовность, "
    "/start — начать раздачу, /table — состояние, "
    "/next — следующая раздача, /newsession — новая сессия без сброса фишек, "
    "/fold, /check, /call, /raise СУММА, /allin — действия. "
    "Блайнды 10/20. Начальный баланс: 1000 фишек за очко репутации; "
    "повторно не начисляется. /баланс @username фишкиN — администратор."
)


ERROR_MESSAGES = {
    "Need at least two ready players": (
        "Для начала игры необходимо минимум 2 участника с положительным балансом фишек.\n"
        "Каждый игрок должен занять место командой /join и подтвердить готовность командой /ready.\n"
        "Когда все будут готовы, используйте /start.\n"
        "Если за столом больше двух игроков, готовность требуется от каждого."
    ),
    "Not your turn": "Сейчас ход другого игрока.",
    "Cannot check": "Нельзя пропустить ход: сначала нужно уравнять ставку или сбросить карты.",
    "Nothing to call": "Уравнивать нечего: используйте /check или сделайте ставку.",
    "Raise too small": "Повышение ставки слишком маленькое.",
    "Raise below minimum": "Повышение ниже минимально допустимого.",
    "Invalid bet": "Некорректный размер ставки или недостаточно фишек.",
    "Cannot join": "Нельзя занять место: вы уже за столом, идёт раздача или все 9 мест заняты.",
    "Hand in progress": "Дождитесь завершения текущей раздачи.",
    "Hand not complete": "Текущая раздача ещё не завершена.",
    "Only the table owner may reset the session": "Новую сессию может начать только владелец стола.",
    "Only seated players may start a hand": "Начать раздачу может только игрок за столом.",
    "Only seated players may advance the game": "Следующую раздачу может запустить только игрок за столом.",
    "Betting not reopened": "Повторное повышение ставки пока недоступно.",
    "Cannot raise without another player able to respond": "Нельзя повышать ставку, когда никто из соперников не может ответить.",
    "Invalid chip balance": "Некорректный баланс фишек.",
    "Unknown action": "Неизвестное игровое действие.",
    "Cannot reset chips during an active hand": "Нельзя начать новую сессию во время раздачи.",
    "Invalid blinds or starting stack": "Некорректные блайнды или стартовый баланс.",
    "Only the table owner may kick players": "Исключать игроков может только владелец стола.",
    "Cannot kick during a hand": "Нельзя исключать игроков во время раздачи. Дождитесь её завершения.",
    "Owner cannot kick themselves": "Владелец не может исключить самого себя. Используйте /leave.",
    "Player is not seated": "Этот пользователь не сидит за покерным столом.",
}


def localize_error(message):
    return ERROR_MESSAGES.get(str(message), str(message))


HAND_NAMES = (
    "Старшая карта", "Пара", "Две пары", "Сет", "Стрит",
    "Флеш", "Фулл-хаус", "Каре", "Стрит-флеш",
)

STREET_NAMES = {
    "waiting": "ОЖИДАНИЕ", "preflop": "ПРЕФЛОП",
    "flop": "ФЛОП", "turn": "ТЁРН", "river": "РИВЕР",
    "complete": "ЗАВЕРШЕНА",
}


def _pot_breakdown(table):
    """Return main/side pot sizes from contributions, without revealing hands."""
    levels = sorted({p.committed for p in table.players.values() if p.committed > 0})
    pots = []
    previous = 0
    for level in levels:
        count = sum(p.committed >= level for p in table.players.values())
        pots.append((level - previous) * count)
        previous = level
    return pots


def format_table(table, names=None, now=None):
    names = names or {}

    def player_link(user_id):
        label = str(names.get(user_id) or f"ID {user_id}").replace("|", " ").replace("]", " ")
        return f"[id{user_id}|{label}]"

    if table is None:
        return "♠ Стол ещё не создан. Напишите /poker."
    stage = STREET_NAMES.get(table.stage, table.stage.upper())
    lines = [f"♠ ASTRA POKER | {table.stage.upper()} · {stage} | Раздача #{table.hand_number}"]
    seated = table.seated()
    active = table.stage in ("preflop", "flop", "turn", "river")
    if active or table.stage == "complete":
        dealer = next((p for p in seated if p.seat == table.dealer), None)
        if dealer:
            sb = dealer if len(seated) == 2 else table.next_after(dealer.seat, seated)
            bb = table.next_after(sb.seat, seated)
            lines.append(
                f"🎲 Дилер: {player_link(dealer.user_id)} | "
                f"SB: {player_link(sb.user_id)} ({table.small_blind}) | "
                f"BB: {player_link(bb.user_id)} ({table.big_blind})"
            )
    lines.append("👥 Игроки:")
    for p in seated:
        flags = []
        if p.user_id == table.owner_id:
            flags.append("👑 владелец")
        if table.stage == "waiting":
            flags.append("готов" if p.ready else "ожидает")
        elif p.user_id in table.pending_ejections:
            flags.append("⏱ исключён после тайм-аута")
        elif p.folded:
            flags.append("пас")
        elif p.all_in:
            flags.append("all-in")
        if table.turn == p.user_id:
            flags.append("▶ ход")
        lines.append(
            f"Место {p.seat}: {player_link(p.user_id)} — {p.stack} фишек"
            + (f" ({', '.join(flags)})" if flags else "")
        )
    if table.stage == "complete":
        report = getattr(table, "showdown_report", {}) or {}
        if table.board:
            lines.append("🃏 Карты стола: " + " ".join(display_card(card) for card in table.board))
        if report.get("hands"):
            lines.append("🎭 ВСКРЫТИЕ КАРТ")
            for uid, hole in report["hands"].items():
                if len(hole) == 2 and len(table.board) == 5:
                    category = evaluate_seven(tuple(hole) + tuple(table.board))[0]
                    cards = " ".join(display_card(card) for card in hole)
                    lines.append(f"• {player_link(uid)}: {cards} — {HAND_NAMES[category]}")
            lines.append("💰 РАСПРЕДЕЛЕНИЕ БАНКОВ")
            for index, pot in enumerate(report.get("pots", [])):
                label = "Основной банк" if index == 0 else f"Побочный банк #{index}"
                lines.append(f"{label}: {pot['amount']} фишек")
                for uid, chips in pot["awards"].items():
                    lines.append(f"  🏆 {player_link(uid)} +{chips}")
        elif table.payouts:
            lines.append("🏆 Победа без вскрытия карт" if not table.board else "🏆 Итоги раздачи:")
        if table.payouts:
            lines.append("🏆 Итоги раздачи:")
            winners = [(uid, chips) for uid, chips in table.payouts.items() if chips > 0]
            winners.sort(key=lambda entry: (-entry[1], table.players[entry[0]].seat if entry[0] in table.players else 99))
            lines.append("🏆 Итоговые выплаты:")
            for uid, chips in winners:
                detail = ""
                player = table.players.get(uid)
                if not report.get("hands") and len(table.live()) > 1 and player and len(table.board) == 5 and len(player.hole) == 2:
                    detail = f" — {HAND_NAMES[evaluate_seven(tuple(player.hole) + tuple(table.board))[0]]}"
                lines.append(f"• {player_link(uid)} получил {chips} фишек{detail}")
        lines.append("Следующая раздача: /next → /ready → /start")
    else:
        total = sum(p.committed for p in table.players.values()) if active else 0
        lines.append(f"💰 Банк: {total} фишек")
        pots = _pot_breakdown(table)
        if len(pots) > 1 and any(p.all_in for p in table.players.values()):
            lines.append(f"Предварительные банки: основной {pots[0]} | дополнительные: " + ", ".join(str(p) for p in pots[1:]))
        if table.board:
            lines.append("Карты стола: " + " ".join(display_card(card) for card in table.board))
        if active:
            lines.append(f"Ставка раунда: {table.current_bet}")
            if table.turn:
                acting = table.players[table.turn]
                to_call = max(0, table.current_bet - acting.street_bet)
                lines.append(f"▶ {player_link(table.turn)}, к уравниванию: {to_call} фишек")
                lines.append(f"Минимальное повышение: {table.min_raise} фишек")
                if table.turn_started_at:
                    remaining = max(0, int(table.turn_started_at + table.turn_timeout_seconds - (time() if now is None else now)))
                    lines.append(f"⏱ Осталось на ход: {remaining} сек.")
    return "\n".join(lines)


def parse_command(text):
    parts = text.strip().lower().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0]
    supported = {
        "/poker", "/pokerhelp", "/join", "/leave", "/ready", "/unready",
        "/start", "/table", "/next", "/newsession", "/fold", "/check", "/call", "/raise", "/allin", "/кик",
    }
    if cmd not in supported:
        return None
    if cmd == "/кик":
        if len(parts) != 2 or not parts[1].startswith("@"):
            raise ValueError("Используйте /кик @username")
        return ("kick", parts[1])
    if cmd == "/raise":
        if len(parts) != 2 or not parts[1].isdecimal():
            raise ValueError("Используйте /raise СУММА, например /raise 500")
        return ("raise", int(parts[1]))
    if len(parts) != 1:
        raise ValueError("Лишние аргументы команды")
    return (cmd[1:], None)


class PokerCommands:
    def __init__(self, store, room_id, private_delivery=None, initial_chips=None, get_names=None):
        self.store = store
        self.room_id = room_id
        self.private_delivery = private_delivery
        self.initial_chips = initial_chips
        self.get_names = get_names
        self.last_public_queued = False

    def _player_name(self, user_id):
        try:
            names = self.get_names([user_id]) if self.get_names else {}
        except Exception:
            names = {}
        label = str((names or {}).get(user_id) or f"ID {user_id}").replace("|", " ").replace("]", " ")
        return f"[id{user_id}|{label}]"

    def _format_table(self, table):
        names = {}
        if table is not None and self.get_names is not None:
            try:
                names = self.get_names([p.user_id for p in table.seated()]) or {}
            except Exception:
                names = {}
        return format_table(table, names)

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
        return self._format_table(table)

    def recover_delivery(self, now=None):
        """Retry persisted private outbox; cancel safely after repeated failures."""
        now = time() if now is None else now
        revision, table = self.store.load(self.room_id)
        if table is None or table.stage not in ("preflop", "flop", "turn", "river"):
            return None
        total, delivered = self.store.private_delivery_status(self.room_id, table.hand_number)
        if total == len(table.players) and delivered == total:
            return None
        if now < table.delivery_retry_at:
            return None
        if total != len(table.players):
            table.delivery_failures = 3
        else:
            try:
                self.private_delivery.deliver_pending(self.store, self.room_id, table.hand_number)
            except Exception:
                pass
            total, delivered = self.store.private_delivery_status(self.room_id, table.hand_number)
            if delivered == total:
                table.delivery_failures = 0
                table.delivery_retry_at = 0.0
                self.store.save(self.room_id, table, revision)
                return "✅ Все закрытые карты доставлены. Игра продолжается."
            table.delivery_failures += 1
        if table.delivery_failures >= 3:
            message = "⚠️ Раздача отменена: закрытые карты не доставлены. Все ставки возвращены. Используйте /ready и /start."
            self.store.cancel_undelivered_hand(self.room_id, table, revision, message)
            self.last_public_queued = True
            return message
        table.delivery_retry_at = now + 30
        self.store.save(self.room_id, table, revision)
        return None

    def process_expired_lobby(self, now=None):
        """Remove five-minute unready seats, including the owner if inactive."""
        self.last_public_queued = False
        revision, table = self.store.load(self.room_id)
        if table is None or table.stage != "waiting":
            return None
        before = [(p.user_id, p.joined_at) for p in table.seated()]
        expired = table.expire_unready(now)
        after = [(p.user_id, p.joined_at) for p in table.seated()]
        if before == after:
            return None
        if expired:
            users = ", ".join(self._player_name(uid) for uid in expired)
            message = (
                f"⏳ Прошло 5 минут без /ready. Игроки удалены из-за стола: {users}.\\n\\n"
                + self._format_table(table)
            )
            saved = self.store.save(self.room_id, table, revision, public_message=message)
            if saved is not None:
                self.last_public_queued = True
            return message if saved is not None else None
        # Persist grace-period timestamps for seats created by older versions.
        self.store.save(self.room_id, table, revision)
        return None

    def process_expired_turn(self, now=None):
        """Apply and persist at most one expired action for this room.

        Invoke from a serialized scheduler; returns a chat-safe update or None.
        """
        self.last_public_queued = False
        revision, table = self.store.load(self.room_id)
        if table is None or table.stage not in ("preflop", "flop", "turn", "river"):
            return None
        total, delivered = self.store.private_delivery_status(
            self.room_id, table.hand_number
        )
        if total != len(table.players) or delivered != total:
            return None
        # Never penalize a player while their turn announcement is still queued.
        if self.store.has_pending_public(self.room_id):
            if table.turn and table.turn_started_at:
                table.turn_started_at = 0.0
                self.store.save(self.room_id, table, revision)
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
            f"⏱ {self._player_name(user_id)} не сделал ход за "
            f"{table.turn_timeout_seconds} секунд. Карты сброшены, игрок исключён из-за стола.\\n"
            + self._format_table(table)
        )
        self.store.save(self.room_id, table, revision, public_message=response)
        self.last_public_queued = True
        return response

    def handle(self, user_id, text, event_id, target_user_id=None):
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
            return self._format_table(table)
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
                balance = self.store.wallet(user_id)
                if balance is None:
                    balance = self.initial_chips(user_id) if self.initial_chips else table.starting_stack
                if type(balance) is not int or balance < 0:
                    raise ValueError("Некорректный начальный баланс")
                table.join(user_id, stack=balance)
            elif cmd == "kick":
                if not target_user_id:
                    return "⚠️ Пользователь не найден. Используйте /кик @username."
                table.kick(user_id, target_user_id)
                response = f"🚪 {self._player_name(target_user_id)} исключён из-за стола владельцем.\n\n" + self._format_table(table)
                saved = self.store.save(self.room_id, table, revision, event_id=event_id, public_message=response)
                if saved is None:
                    return None
                self.last_public_queued = True
                return response
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
                        f"🃏 Раздача #{table.hand_number} началась! "
                        "Закрытые карты отправляются игрокам в личные сообщения.\n\n"
                        + self._format_table(table)
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
            response = self._format_table(table)
            saved = self.store.save(
                self.room_id, table, revision, event_id=event_id,
                public_message=response,
            )
            if saved is None:
                return None
            self.last_public_queued = True
        except (ValueError, KeyError) as exc:
            return f"⚠️ {localize_error(exc)}"
        return self._format_table(table)
