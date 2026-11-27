import tempfile
import unittest
from pathlib import Path

from poker.commands import PokerCommands, parse_command
from poker.storage import PokerStore


class CommandTests(unittest.TestCase):
    def test_table_shows_turn_cost_and_completed_payouts(self):
        from poker.commands import format_table
        from poker.engine import Table

        table = Table()
        for user_id in (101, 202):
            table.join(user_id)
            table.ready(user_id)
        table.start()
        during = format_table(table)
        self.assertIn("к уравниванию: 10 фишек", during)
        self.assertIn("Минимальное повышение: 20 фишек", during)
        table.act(table.turn, "fold")
        finished = format_table(table)
        self.assertIn("🏆 Итоги раздачи:", finished)
        self.assertIn("получил 30 фишек", finished)
        self.assertIn("/next → /ready → /start", finished)
        self.assertNotIn("Закрытые карты", finished)

    def test_readable_cards_and_real_vk_names(self):
        from poker.commands import format_table
        from poker.engine import Table
        table = Table()
        table.join(101)
        table.join(202)
        table.stage = "river"
        table.turn = 101
        table.board = ["7c", "Kc", "Js", "Kd", "As"]
        text = format_table(table, {101: "Артём Иванов", 202: "Мария Петрова"})
        self.assertIn("[id101|Артём Иванов]", text)
        self.assertIn("[id202|Мария Петрова]", text)
        self.assertIn("Карты стола: 7♣ K♣ J♠ K♦ A♠", text)
        self.assertNotIn("Общие карты:", text)
        self.assertNotIn("|Игрок]", text)
        self.assertIn("▶ [id101|Артём Иванов]", text)
        self.assertIn("[id101|ID 101]", format_table(table))
        table.stage = "complete"
        table.turn = 0
        table.payouts = {202: 40}
        self.assertIn("[id202|Мария Петрова] получил 40 фишек", format_table(table, {202: "Мария Петрова"}))

    def test_start_requires_every_seated_player_ready(self):
        class Delivery:
            def can_message(self, uid):
                return True
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999, private_delivery=Delivery())
            for uid in (101, 202, 303):
                bot.handle(uid, "/join", f"join-{uid}")
            bot.handle(101, "/ready", "ready-101")
            bot.handle(202, "/ready", "ready-202")
            message = bot.handle(101, "/start", "start-unready")
            self.assertIn("минимум 2 участника", message)
            self.assertIn("/join", message)
            self.assertIn("/ready", message)
            self.assertIn("/start", message)
            self.assertIn("от каждого", message)
            self.assertEqual(store.load(2000000999)[1].stage, "waiting")
            store.close()

    def test_parser(self):
        self.assertEqual(parse_command("/raise 500"), ("raise", 500))
        self.assertEqual(parse_command("/кик @kenaya"), ("kick", "@kenaya"))
        self.assertIsNone(parse_command("обычное сообщение"))
        with self.assertRaises(ValueError):
            parse_command("/raise -5")

    def test_owner_kick_only_between_hands_and_preserves_wallet(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999)
            for uid in (101, 202, 303):
                bot.handle(uid, "/join", f"join-{uid}")
            self.assertIn("только владелец", bot.handle(202, "/кик @id303", "unauth", target_user_id=303))
            self.assertIn("самого себя", bot.handle(101, "/кик @id101", "self", target_user_id=101))
            self.assertIn("не сидит", bot.handle(101, "/кик @id404", "missing", target_user_id=404))
            self.assertIn("не найден", bot.handle(101, "/кик @missing", "unresolved"))
            for uid in (101, 202, 303):
                bot.handle(uid, "/ready", f"ready-{uid}")
            class Delivery:
                def can_message(self, uid):
                    return True
                def deliver_pending(self, store, room, hand):
                    for uid, rid, msg in store.pending_private_cards(room, hand):
                        store.mark_private_delivered(room, hand, uid)
            bot.private_delivery = Delivery()
            bot.handle(101, "/start", "start")
            self.assertIn("во время раздачи", bot.handle(101, "/кик @id303", "inhand", target_user_id=303))
            table = store.load(2000000999)[1]
            while table.stage != "complete":
                bot.handle(table.turn, "/fold", f"fold-{table.turn}-{table.stage}")
                table = store.load(2000000999)[1]
            balance = store.wallet(303)
            result = bot.handle(101, "/кик @id303", "kick303", target_user_id=303)
            self.assertIn("исключён", result)
            self.assertNotIn(303, store.load(2000000999)[1].players)
            self.assertEqual(store.wallet(303), balance)
            self.assertIsNone(bot.handle(101, "/кик @id303", "kick303", target_user_id=303))
            self.assertIn("WAITING", bot.handle(101, "/next", "next"))
            self.assertIn("Место 3", bot.handle(303, "/join", "rejoin"))
            store.close()

    def test_automatic_delivery_retries_then_refunds_and_recovers_after_restart(self):
        class BrokenDelivery:
            def can_message(self, uid):
                return True
            def deliver_pending(self, store, room, hand):
                raise RuntimeError("VK unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "poker.db"
            store = PokerStore(db)
            bot = PokerCommands(store, 2000000999, private_delivery=BrokenDelivery())
            for uid in (101, 202):
                bot.handle(uid, "/join", f"join-{uid}")
                bot.handle(uid, "/ready", f"ready-{uid}")
            self.assertIn("приостановлена", bot.handle(101, "/start", "start"))
            table = store.load(2000000999)[1]
            self.assertEqual(table.stage, "preflop")
            store.close()
            reopened = PokerStore(db)
            recovered = PokerCommands(reopened, 2000000999, private_delivery=BrokenDelivery())
            self.assertIsNone(recovered.recover_delivery(now=100))
            self.assertIsNone(recovered.recover_delivery(now=131))
            message = recovered.recover_delivery(now=162)
            self.assertIn("Раздача отменена", message)
            table = reopened.load(2000000999)[1]
            self.assertEqual(table.stage, "waiting")
            self.assertEqual(sum(p.stack for p in table.players.values()), 20000)
            self.assertEqual(reopened.pending_private_cards(2000000999, 1), [])
            self.assertTrue(reopened.pending_public(2000000999))
            self.assertIsNone(recovered.recover_delivery(now=200))
            reopened.close()

    def test_lobby_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999)
            self.assertIn("Место 1", bot.handle(101, "/join", "1"))
            self.assertIsNone(bot.handle(101, "/join", "1"))
            self.assertIn("Место 2", bot.handle(202, "/join", "2"))
            self.assertIn("готов", bot.handle(101, "/ready", "3"))
            self.assertIn("отключена", bot.handle(101, "/start", "4"))
            self.assertEqual(store.load(2000000999)[1].stage, "waiting")
            store.close()

    def test_join_requires_private_message_permission(self):
        class Denied:
            def can_message(self, user_id):
                return False
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999, private_delivery=Denied())
            self.assertIn("Разрешите личные сообщения", bot.handle(101, "/join", "a"))
            self.assertEqual(store.load(2000000999), (0, None))
            store.close()

    def test_full_hand_delivery_failure_and_recovery(self):
        class Messages:
            def __init__(self):
                self.calls = []
                self.fail_once = True
            def send(self, **kwargs):
                self.calls.append(kwargs)
                if self.fail_once and len(self.calls) == 2:
                    self.fail_once = False
                    raise RuntimeError("VK temporary failure")
                return 123

        class FakeVK:
            def __init__(self):
                self.messages = Messages()

        from poker.private_cards import VkPrivateCards
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            vk = FakeVK()
            adapter = VkPrivateCards(vk, 123)
            adapter.can_message = lambda user_id: True
            bot = PokerCommands(store, 2000000999, private_delivery=adapter)
            for uid in (101, 202):
                self.assertIn("Место", bot.handle(uid, "/join", "join-" + str(uid)))
                bot.handle(uid, "/ready", "ready-" + str(uid))
            reply = bot.handle(101, "/start", "start-1")
            self.assertIn("приостановлена", reply)
            table = store.load(2000000999)[1]
            self.assertEqual(table.stage, "preflop")
            self.assertEqual(store.private_delivery_status(2000000999, 1), (2, 1))
            self.assertIn("Доставка", bot.handle(table.turn, "/call", "blocked"))
            store.close()
            reopened = PokerStore(Path(tmp) / "poker.db")
            recovered = PokerCommands(reopened, 2000000999, private_delivery=adapter)
            self.assertIn("PREFLOP", recovered.handle(101, "/table", "recovery"))
            self.assertEqual(reopened.private_delivery_status(2000000999, 1), (2, 2))
            self.assertEqual(vk.messages.calls[1]["random_id"],
                             vk.messages.calls[2]["random_id"])
            self.assertEqual(len(vk.messages.calls), 3)
            reopened.close()

    def test_complete_hand_next_hand_and_fresh_session(self):
        class Delivery:
            def can_message(self, uid):
                return True
            def deliver_pending(self, store, room, hand):
                for uid, random_id, msg in store.pending_private_cards(room, hand):
                    store.mark_private_delivered(room, hand, uid)

        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999, private_delivery=Delivery())
            for uid in (101, 202):
                bot.handle(uid, "/join", "join" + str(uid))
                bot.handle(uid, "/ready", "ready" + str(uid))
            self.assertIn("PREFLOP", bot.handle(101, "/start", "start1"))
            queued = store.pending_public(2000000999)
            announcements = [row[2] for row in queued if "Раздача #1 началась!" in row[2]]
            self.assertEqual(len(announcements), 1)
            announcement = announcements[0]
            self.assertIn("Раздача #1 началась!", announcement)
            self.assertIn("ASTRA POKER | PREFLOP", announcement)
            self.assertIn("Место 1:", announcement)
            self.assertIn("Место 2:", announcement)
            self.assertIn("к уравниванию:", announcement)
            self.assertNotIn("Ваши закрытые карты:", announcement)
            self.assertEqual(announcement.count("ASTRA POKER | PREFLOP"), 1)
            first = store.load(2000000999)[1]
            self.assertEqual(first.hand_number, 1)
            self.assertIn("COMPLETE", bot.handle(first.turn, "/fold", "fold1"))
            self.assertIn("WAITING", bot.handle(101, "/next", "next1"))
            for uid in (101, 202):
                bot.handle(uid, "/ready", "ready2" + str(uid))
            self.assertIn("PREFLOP", bot.handle(101, "/start", "start2"))
            second = store.load(2000000999)[1]
            self.assertEqual(second.hand_number, 2)
            self.assertIn("COMPLETE", bot.handle(second.turn, "/fold", "fold2"))
            self.assertIn("WAITING", bot.handle(101, "/newsession", "session2"))
            fresh = store.load(2000000999)[1]
            self.assertTrue(all(p.stack == 10000 for p in fresh.players.values()))
            for uid in (101, 202):
                bot.handle(uid, "/ready", "ready3" + str(uid))
            self.assertIn("PREFLOP", bot.handle(101, "/start", "start3"))
            self.assertEqual(store.load(2000000999)[1].hand_number, 3)
            self.assertEqual(store.private_delivery_status(2000000999, 3), (2, 2))
            store.close()

    def test_unseated_user_cannot_reset_or_start_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999)
            bot.handle(101, "/join", "join101")
            bot.handle(202, "/join", "join202")
            for command in ("/start", "/next", "/newsession"):
                self.assertIn("только владелец стола" if command == "/newsession" else "только игрок за столом",
                              bot.handle(999, command, "other" + command))
            self.assertEqual(store.load(2000000999)[1].stage, "waiting")
            store.close()

    def test_only_owner_can_reset_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999)
            bot.handle(101, "/join", "j1")
            bot.handle(202, "/join", "j2")
            self.assertEqual(store.load(2000000999)[1].owner_id, 101)
            self.assertIn("только владелец стола",
                          bot.handle(202, "/newsession", "denied"))
            self.assertIn("WAITING", bot.handle(101, "/newsession", "allowed"))
            bot.handle(101, "/leave", "left")
            self.assertEqual(store.load(2000000999)[1].owner_id, 202)
            self.assertIn("WAITING", bot.handle(202, "/newsession", "new-owner"))
            store.close()

    def test_private_delivery_restarts_turn_clock_after_delay(self):
        from unittest.mock import patch
        class Delivery:
            def can_message(self, uid):
                return True
            def deliver_pending(self, store, room, hand):
                for uid, random_id, msg in store.pending_private_cards(room, hand):
                    store.mark_private_delivered(room, hand, uid)
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            bot = PokerCommands(store, 2000000999, private_delivery=Delivery())
            for uid in (101, 202):
                bot.handle(uid, "/join", "j" + str(uid))
                bot.handle(uid, "/ready", "r" + str(uid))
            with patch("poker.commands.time", return_value=2000000000.0):
                bot.handle(101, "/start", "start-delayed")
            table = store.load(2000000999)[1]
            self.assertEqual(table.turn_started_at, 2000000000.0)
            self.assertIsNone(bot.process_expired_turn(now=2000000119.0))
            for job_id, _, _ in store.pending_public(2000000999):
                store.mark_public_delivered(job_id)
            self.assertIsNone(bot.process_expired_turn(now=2000000119.0))
            self.assertIsNone(bot.process_expired_turn(now=2000000238.0))
            self.assertIsNotNone(bot.process_expired_turn(now=2000000239.0))
            store.close()


if __name__ == "__main__":
    unittest.main()
