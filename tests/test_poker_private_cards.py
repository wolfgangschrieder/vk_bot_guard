import unittest
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

if __name__ == "__main__":
    unittest.main()
