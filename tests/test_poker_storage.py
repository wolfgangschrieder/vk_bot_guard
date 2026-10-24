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


if __name__ == "__main__":
    unittest.main()
