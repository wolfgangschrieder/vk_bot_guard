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

if __name__ == "__main__":
    unittest.main()
