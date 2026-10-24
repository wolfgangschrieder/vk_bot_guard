import tempfile
import unittest
from pathlib import Path
from poker.storage import PokerStore
from poker.private_cards import VkPrivateCards

class FakeMessages:
    def __init__(self):
        self.calls = []
    def isMessagesFromGroupAllowed(self, **kwargs):
        return {"is_allowed": int(kwargs["user_id"] == 42)}
    def send(self, **kwargs):
        self.calls.append(kwargs)
        return 123

class FakeVK:
    def __init__(self):
        self.messages = FakeMessages()

class PrivateCardTests(unittest.TestCase):
    def test_permission(self):
        adapter = VkPrivateCards(FakeVK(), 123)
        self.assertTrue(adapter.can_message(42))
        self.assertFalse(adapter.can_message(43))
        self.assertFalse(VkPrivateCards(FakeVK()).can_message(42))

    def test_delivery_to_user_peer(self):
        vk = FakeVK()
        VkPrivateCards(vk, 123).send(42, ("Ah", "Td"), 7, random_id=54321)
        sent = vk.messages.calls[0]
        self.assertEqual(sent["peer_id"], 42)
        self.assertIn("10", sent["message"])
        self.assertEqual(sent["random_id"], 54321)

    def test_pending_delivery_uses_same_random_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokerStore(Path(tmp) / "poker.db")
            store.enqueue_private_cards(77, 1, {42: (4321, "private cards")})
            vk = FakeVK()
            adapter = VkPrivateCards(vk, 123)
            self.assertEqual(adapter.deliver_pending(store, 77, 1), 1)
            self.assertEqual(vk.messages.calls[0]["random_id"], 4321)
            self.assertEqual(adapter.deliver_pending(store, 77, 1), 0)
            self.assertEqual(len(vk.messages.calls), 1)
            store.close()

    def test_uncertain_vk_failure_retries_same_private_cards_after_restart(self):
        class UncertainMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.seen = set()
                self.fail_once = True
            def send(self, **kwargs):
                key = (kwargs["peer_id"], kwargs["random_id"])
                self.calls.append(kwargs)
                self.seen.add(key)
                if self.fail_once:
                    self.fail_once = False
                    raise ConnectionError("VK accepted message but response was lost")
                return 123

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poker.db"
            store = PokerStore(path)
            store.enqueue_private_cards(77, 1, {
                42: (4321, "first private hand"),
                43: (4322, "second private hand"),
            })
            vk = FakeVK()
            vk.messages = UncertainMessages()
            adapter = VkPrivateCards(vk, 123)
            with self.assertRaises(ConnectionError):
                adapter.deliver_pending(store, 77, 1)
            self.assertEqual(store.private_delivery_status(77, 1), (2, 0))
            store.close()
            reopened = PokerStore(path)
            self.assertEqual(adapter.deliver_pending(reopened, 77, 1), 2)
            self.assertEqual(store.path, reopened.path)
            self.assertEqual(vk.messages.calls[0]["random_id"],
                             vk.messages.calls[1]["random_id"])
            self.assertEqual(len(vk.messages.seen), 2)
            self.assertEqual(reopened.private_delivery_status(77, 1), (2, 2))
            self.assertEqual(adapter.deliver_pending(reopened, 77, 1), 0)
            reopened.close()

if __name__ == "__main__":
    unittest.main()
