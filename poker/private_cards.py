"""VK private-card delivery adapter.

Only sends to the player's personal VK peer, never to a group chat.
The integration must check private messaging availability before starting.
"""
from secrets import randbelow

SUIT_SYMBOLS = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}


def display_card(card):
    if len(card) != 2 or card[1] not in SUIT_SYMBOLS:
        raise ValueError("Invalid card")
    return card[0].replace("T", "10") + SUIT_SYMBOLS[card[1]]


def format_hole_cards(hole, hand_number):
    if len(hole) != 2:
        raise ValueError("Exactly two cards required")
    return (
        f"♠ ASTRA POKER ROOM — раздача #{hand_number}\n"
        f"Ваши закрытые карты: {display_card(hole[0])} {display_card(hole[1])}\n"
        "Не пересылайте это сообщение другим игрокам."
    )


class VkPrivateCards:
    def __init__(self, vk, group_id=None):
        self.vk = vk
        self.group_id = group_id

    def can_message(self, user_id):
        """Best-effort permission preflight; VK may still reject the send."""
        if type(user_id) is not int or user_id <= 0:
            return False
        if not self.group_id:
            return False
        result = self.vk.messages.isMessagesFromGroupAllowed(group_id=self.group_id, user_id=user_id)
        return bool(result.get("is_allowed", 0))

    def deliver_pending(self, store, room_id, hand_number):
        """Retry queued VK messages with their original random IDs.

        An uncertain VK failure leaves the job pending for later retry.
        """
        delivered = 0
        for user_id, random_id, message in store.pending_private_cards(room_id, hand_number):
            self.vk.messages.send(
                peer_id=user_id,
                random_id=random_id,
                message=message,
            )
            store.mark_private_delivered(room_id, hand_number, user_id)
            delivered += 1
        return delivered

    def send(self, user_id, hole, hand_number, random_id=None):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Invalid user ID")
        # VK random_id must not be zero for retry-safe delivery.
        if random_id is None:
            random_id = randbelow(2_147_483_647) + 1
        if type(random_id) is not int or not 0 < random_id <= 2_147_483_647:
            raise ValueError("Invalid VK random_id")
        return self.vk.messages.send(
            peer_id=user_id,
            random_id=random_id,
            message=format_hole_cards(hole, hand_number),
        )
