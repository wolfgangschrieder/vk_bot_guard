import tempfile
import threading
import unittest
from pathlib import Path

from poker.engine import Table
from poker.storage import PokerStore


class StorageTests(unittest.TestCase):
    def test_watchdog_thread_can_expire_unready_players(self):
        from poker.commands import PokerCommands
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            table = Table()
            table.join(101, stack=16000)
            table.join(202, stack=0)
            table.players[101].joined_at = 1000.0
            table.players[202].joined_at = 1000.0
            store.save(2000000005, table, 0)
            commands = PokerCommands(store, 2000000005)
            result = []
            errors = []

            def watchdog():
                try:
                    result.append(commands.process_expired_lobby(now=1301.0))
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=watchdog)
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "Watchdog deadlocked")
            self.assertFalse(errors, errors)
            self.assertEqual(len(result), 1)
            self.assertIn("5 минут", result[0])
            revision, after = store.load(2000000005)
            self.assertEqual(revision, 2)
            self.assertEqual(after.players, {})
            self.assertEqual(after.owner_id, 0)
            self.assertEqual(store.wallet(101), 16000)
            store.close()


    def test_restart_and_duplicate_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poker.db"
            store = PokerStore(path)
            table = Table()
            table.join(101)
            table.join(202)
            table.ready(101)
            table.ready(202)
            table.start()
            revision = store.save(2000000005, table, 0, event_id="vk-1")
            self.assertEqual(revision, 1)
            self.assertIsNone(store.save(2000000005, table, 1, event_id="vk-1"))
            store.close()

            reopened = PokerStore(path)
            rev, loaded = reopened.load(2000000005)
            self.assertEqual(rev, 1)
            self.assertEqual(loaded.stage, "preflop")
            self.assertEqual(loaded.turn, table.turn)
            self.assertEqual(loaded.players[101].hole, table.players[101].hole)
            self.assertEqual(loaded.deck, table.deck)
            with self.assertRaises(ValueError):
                reopened.save(2000000005, loaded, 0, event_id="vk-2")
            reopened.close()

    def test_isolated_rooms(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            store.save(1, Table(), 0)
            self.assertEqual(store.load(2), (0, None))
            store.close()

    def test_private_outbox_survives_restart_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poker.db"
            store = PokerStore(path)
            store.enqueue_private_cards(123, 7, {42: (12345, "secret hole cards")})
            store.enqueue_private_cards(123, 7, {42: (12345, "secret hole cards")})
            self.assertEqual(store.pending_private_cards(123, 7),
                             [(42, 12345, "secret hole cards")])
            with self.assertRaises(ValueError):
                store.enqueue_private_cards(123, 7, {42: (67890, "different cards")})
            store.close()
            reopened = PokerStore(path)
            self.assertEqual(reopened.pending_private_cards(123, 7),
                             [(42, 12345, "secret hole cards")])
            reopened.mark_private_delivered(123, 7, 42)
            self.assertEqual(reopened.pending_private_cards(123, 7), [])
            reopened.close()

    def test_dealt_hand_and_outbox_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            table = Table()
            for uid in (101, 202):
                table.join(uid)
                table.ready(uid)
            table.start()
            messages = {uid: (uid + 1000, "private cards")
                        for uid in table.players}
            self.assertEqual(
                store.save_hand_with_private_cards(77, table, 0, messages, "deal-1"),
                1,
            )
            self.assertEqual(len(store.pending_private_cards(77, 1)), 2)
            self.assertIsNone(
                store.save_hand_with_private_cards(77, table, 1, messages, "deal-1")
            )
            self.assertEqual(store.load(77)[0], 1)
            with self.assertRaises(ValueError):
                store.save_hand_with_private_cards(
                    88, table, 0, {101: (1101, "missing player")}, "deal-2"
                )
            self.assertEqual(store.load(88), (0, None))
            self.assertEqual(store.pending_private_cards(88, 1), [])
            store.close()

    def test_atomic_deal_rolls_back_outbox_if_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            table = Table()
            for uid in (101, 202):
                table.join(uid)
                table.ready(uid)
            table.start()
            store.conn.execute(
                "CREATE TRIGGER fail_poker_state BEFORE INSERT ON poker_state "
                "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
            )
            store.conn.commit()
            with self.assertRaises(Exception):
                store.save_hand_with_private_cards(
                    77, table, 0,
                    {101: (9001, "card A"), 202: (9002, "card B")},
                    "atomic-failure",
                )
            self.assertEqual(store.load(77), (0, None))
            self.assertEqual(store.pending_private_cards(77, 1), [])
            self.assertFalse(store.is_processed(77, "atomic-failure"))
            store.close()

    def test_legacy_snapshot_gets_owner_on_deserialization(self):
        import json
        from poker.storage import serialize, deserialize
        from poker.engine import Table
        table = Table()
        table.join(101)
        table.join(202)
        raw = json.loads(serialize(table))
        raw.pop("owner_id")
        restored = deserialize(json.dumps(raw))
        self.assertEqual(restored.owner_id, 101)

    def test_public_outbox_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "public.db"
            store = PokerStore(path)
            store.enqueue_public(2000000999, 1234567, "First public update")
            store.enqueue_public(2000000999, 1234568, "Second public update")
            with self.assertRaises(Exception):
                store.enqueue_public(2000000999, 1234567, "Duplicate")
            store.close()
            reopened = PokerStore(path)
            jobs = reopened.pending_public(2000000999)
            self.assertEqual([j[1] for j in jobs], [1234567, 1234568])
            reopened.mark_public_delivered(jobs[0][0])
            self.assertEqual([j[1] for j in reopened.pending_public(2000000999)], [1234568])
            reopened.close()

    def test_atomic_state_and_public_message_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "atomic.db")
            table = Table()
            table.join(101)
            self.assertEqual(
                store.save(2000000999, table, 0, event_id="event-1",
                           public_message="Player joined"), 1
            )
            self.assertEqual(store.load(2000000999)[0], 1)
            self.assertEqual(len(store.pending_public(2000000999)), 1)
            self.assertIsNone(
                store.save(2000000999, table, 0, event_id="event-1",
                           public_message="Duplicate")
            )
            self.assertEqual(len(store.pending_public(2000000999)), 1)
            with self.assertRaises(ValueError):
                store.save(2000000999, table, 0, event_id="event-2",
                           public_message="Stale")
            self.assertEqual(len(store.pending_public(2000000999)), 1)
            store.close()

    def test_hand_private_and_public_jobs_commit_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "atomic-hand.db")
            table = Table()
            table.join(101)
            table.join(202)
            table.ready(101)
            table.ready(202)
            table.start()
            messages = {uid: (1000 + uid, "Private cards") for uid in table.players}
            self.assertEqual(
                store.save_hand_with_private_cards(
                    2000000999, table, 0, messages, event_id="start",
                    public_message="Hand started",
                ), 1
            )
            self.assertEqual(store.private_delivery_status(2000000999, 1), (2, 0))
            self.assertEqual(len(store.pending_public(2000000999)), 1)
            self.assertIsNone(store.save_hand_with_private_cards(
                2000000999, table, 0, messages, event_id="start",
                public_message="Duplicate",
            ))
            self.assertEqual(len(store.pending_public(2000000999)), 1)
            store.close()


    def test_ejected_balance_ids_are_integers_after_restart(self):
        from poker.storage import serialize, deserialize
        table = Table()
        table.ejected_balances = {101: 900}
        restored = deserialize(serialize(table))
        self.assertEqual(restored.ejected_balances, {101: 900})
        self.assertTrue(all(isinstance(uid, int) for uid in restored.ejected_balances))

    def test_chip_conservation_rejects_corrupted_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "integrity.db")
            table = Table()
            for uid in (101, 202):
                table.join(uid)
                table.ready(uid)
            table.start()
            revision = store.save(77, table, 0)
            table.players[101].stack += 100
            table.stage = "complete"
            with self.assertRaisesRegex(ValueError, "conservation"):
                store.save(77, table, revision)
            self.assertEqual(store.load(77)[0], revision)
            store.close()


if __name__ == "__main__":
    unittest.main()
