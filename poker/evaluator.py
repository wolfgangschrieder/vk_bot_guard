"""Pure Texas Hold'em hand evaluator. No VK or database dependencies."""
from collections import Counter
from itertools import combinations

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(r + s for r in RANKS for s in SUITS)


def _validate(cards, expected=None):
    cards = tuple(cards)
    if expected is not None and len(cards) != expected:
        raise ValueError(f"Expected {expected} cards, got {len(cards)}")
    if any(not isinstance(c, str) or len(c) != 2 or c[0] not in RANKS or c[1] not in SUITS for c in cards):
        raise ValueError("Invalid card")
    if len(set(cards)) != len(cards):
        raise ValueError("Duplicate card")
    return cards


def evaluate_five(cards):
    """Comparable tuple: category 0..8 followed by tiebreakers."""
    cards = _validate(cards, 5)
    ranks = sorted((RANKS.index(c[0]) + 2 for c in cards), reverse=True)
    counts = Counter(ranks)
    unique = set(ranks)
    if 14 in unique:
        unique.add(1)
    straight = next((high for high in range(14, 4, -1)
                     if all(high - delta in unique for delta in range(5))), 0)
    flush = len({c[1] for c in cards}) == 1
    if flush and straight:
        return (8, straight)
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    if groups[0][0] == 4:
        return (7, groups[0][1], groups[1][1])
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *ranks)
    if straight:
        return (4, straight)
    if groups[0][0] == 3:
        return (3, groups[0][1], *sorted((r for r in ranks if r != groups[0][1]), reverse=True))
    pairs = sorted((r for r, count in counts.items() if count == 2), reverse=True)
    if len(pairs) == 2:
        kicker = next(r for r in ranks if r not in pairs)
        return (2, *pairs, kicker)
    if pairs:
        return (1, pairs[0], *sorted((r for r in ranks if r != pairs[0]), reverse=True))
    return (0, *ranks)


def evaluate_seven(cards):
    """Best five-card rank from exactly seven cards."""
    cards = _validate(cards, 7)
    return max(evaluate_five(five) for five in combinations(cards, 5))


def determine_winners(hands, board):
    """Return all winning player IDs, including ties.

    hands: mapping of player_id -> exactly two hole cards.
    board: exactly five community cards.
    """
    board = _validate(board, 5)
    if not hands:
        raise ValueError("At least one hand is required")
    all_cards = list(board)
    for hole in hands.values():
        hole = _validate(hole, 2)
        all_cards.extend(hole)
    _validate(all_cards)
    scores = {player: evaluate_seven(tuple(hole) + board) for player, hole in hands.items()}
    best = max(scores.values())
    return [player for player, score in scores.items() if score == best]


def settle_pots(contributions, eligible_hands, board, seat_order):
    """Compute side pots and deterministic odd-chip allocation.

    contributions maps player IDs to total committed chips, including folded players.
    eligible_hands contains only non-folded players' hole cards.
    seat_order orders players for odd chips (caller supplies correct clockwise order).
    Returns payout mapping; chip conservation is enforced.
    """
    if not contributions or not eligible_hands:
        raise ValueError("Missing contributions or eligible players")
    if set(eligible_hands) - set(contributions):
        raise ValueError("Eligible player without contribution")
    if len(seat_order) != len(set(seat_order)) or set(seat_order) != set(contributions):
        raise ValueError("Seat order must contain every player exactly once")
    if any(type(amount) is not int or amount < 0 for amount in contributions.values()):
        raise ValueError("Invalid contribution")
    board = _validate(board, 5)
    _validate(list(board) + [card for hole in eligible_hands.values() for card in hole])
    payouts = {player: 0 for player in contributions}
    levels = sorted(set(amount for amount in contributions.values() if amount > 0))
    previous = 0
    for level in levels:
        participants = [p for p, amount in contributions.items() if amount >= level]
        pot = (level - previous) * len(participants)
        contenders = {p: eligible_hands[p] for p in participants if p in eligible_hands}
        if not contenders:
            raise ValueError("Unmatched or invalid contribution layer")
        winners = determine_winners(contenders, board)
        share, remainder = divmod(pot, len(winners))
        for player in winners:
            payouts[player] += share
        for player in seat_order:
            if remainder == 0:
                break
            if player in winners:
                payouts[player] += 1
                remainder -= 1
        previous = level
    if sum(payouts.values()) != sum(contributions.values()):
        raise AssertionError("Chip conservation violated")
    return payouts
