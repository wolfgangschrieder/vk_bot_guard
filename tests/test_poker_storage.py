import tempfile
import unittest
from pathlib import Path

from poker.engine import Table
from poker.storage import PokerStore


class StorageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
