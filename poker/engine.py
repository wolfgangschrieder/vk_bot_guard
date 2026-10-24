"""Independent no-limit Hold'em table. No VK calls or persistence."""
from dataclasses import dataclass, field
from secrets import SystemRandom

from .evaluator import DECK, settle_pots


@dataclass
class Player:
    user_id: int
    seat: int
    stack: int
    ready: bool = False
    folded: bool = False
    all_in: bool = False
    committed: int = 0
    street_bet: int = 0
    hole: tuple = ()
    acted: bool = False
    can_raise: bool = True


@dataclass
class Table:
    starting_stack: int = 10000
    small_blind: int = 50
    big_blind: int = 100
    players: dict = field(default_factory=dict)
    stage: str = "waiting"
    dealer: int = 0
    turn: int = 0
    current_bet: int = 0
    min_raise: int = 100
    deck: list = field(default_factory=list)
    board: list = field(default_factory=list)
    hand_number: int = 0
    payouts: dict = field(default_factory=dict)
    last_dealer: int = 0

    def __post_init__(self):
        if not 0 < self.small_blind < self.big_blind <= self.starting_stack:
            raise ValueError("Invalid blinds or starting stack")

    def seated(self):
        return sorted(self.players.values(), key=lambda p: p.seat)

    def next_after(self, seat, candidates):
        candidates = sorted(candidates, key=lambda p: p.seat)
        return next((p for p in candidates if p.seat > seat), candidates[0])

    def live(self):
        return [p for p in self.seated() if not p.folded]

    def actionable(self):
        return [p for p in self.live() if not p.all_in]

    def join(self, user_id):
        if self.stage != "waiting" or user_id in self.players or len(self.players) == 9:
            raise ValueError("Cannot join")
        seat = next(i for i in range(1, 10) if all(p.seat != i for p in self.players.values()))
        self.players[user_id] = Player(user_id, seat, self.starting_stack)
        return seat

    def leave(self, user_id):
        if self.stage != "waiting":
            raise ValueError("Hand in progress")
        del self.players[user_id]

    def ready(self, user_id, value=True):
        if self.stage != "waiting":
            raise ValueError("Hand in progress")
        self.players[user_id].ready = bool(value)

    def commit(self, player, amount):
        paid = min(amount, player.stack)
        player.stack -= paid
        player.committed += paid
        player.street_bet += paid
        player.all_in = player.stack == 0

    def start(self):
        if self.stage != "waiting":
            raise ValueError("Hand in progress")
        ps = self.seated()
        if len(ps) < 2 or any(not p.ready or p.stack <= 0 for p in ps):
            raise ValueError("Need at least two ready players")
        for p in ps:
            p.folded = p.all_in = p.acted = False
            p.can_raise = True
            p.committed = p.street_bet = 0
            p.ready = False
        self.deck = list(DECK)
        SystemRandom().shuffle(self.deck)
        self.board = []
        self.payouts = {}
        self.hand_number += 1
        self.dealer = self.next_after(self.last_dealer, ps).seat
        self.last_dealer = self.dealer
        for p in ps:
            p.hole = (self.deck.pop(), self.deck.pop())
        sb = next(p for p in ps if p.seat == self.dealer) if len(ps) == 2 else self.next_after(self.dealer, ps)
        bb = self.next_after(sb.seat, ps)
        self.commit(sb, self.small_blind)
        self.commit(bb, self.big_blind)
        self.current_bet = max(p.street_bet for p in ps)
        self.min_raise = self.big_blind
        self.stage = "preflop"
        self.turn = self.next_after(bb.seat, self.actionable()).user_id if self.actionable() else 0
        actionable = self.actionable()
        if not actionable or (len(actionable) == 1 and actionable[0].street_bet >= self.current_bet):
            self.next_street()

    def act(self, user_id, action, amount=None):
        if self.stage not in ("preflop", "flop", "turn", "river") or self.turn != user_id:
            raise ValueError("Not your turn")
        p = self.players[user_id]
        owed = self.current_bet - p.street_bet
        if action == "fold":
            p.folded = True
        elif action == "check":
            if owed:
                raise ValueError("Cannot check")
        elif action == "call":
            if not owed:
                raise ValueError("Nothing to call")
            self.commit(p, owed)
        elif action in ("raise", "allin"):
            target = amount if action == "raise" else p.street_bet + p.stack
            if type(target) is not int or target <= p.street_bet or target > p.street_bet + p.stack:
                raise ValueError("Invalid bet")
            if target <= self.current_bet:
                if action != "allin" or p.stack > owed:
                    raise ValueError("Raise too small")
                self.commit(p, p.stack)
            else:
                if not p.can_raise:
                    raise ValueError("Betting not reopened")
                increase = target - self.current_bet
                if increase < self.min_raise and target != p.street_bet + p.stack:
                    raise ValueError("Raise below minimum")
                self.commit(p, target - p.street_bet)
                self.current_bet = target
                if increase >= self.min_raise:
                    self.min_raise = increase
                    for other in self.actionable():
                        if other.user_id != user_id:
                            other.acted = False
                            other.can_raise = True
                else:
                    for other in self.actionable():
                        if other.user_id != user_id and other.acted:
                            other.can_raise = False
        else:
            raise ValueError("Unknown action")
        p.acted = True
        self.progress(user_id)

    def progress(self, last_actor):
        live = self.live()
        if len(live) == 1:
            winner = live[0]
            amount = sum(p.committed for p in self.players.values())
            winner.stack += amount
            self.payouts = {winner.user_id: amount}
            self.stage = "complete"
            self.turn = 0
            return
        actionable = self.actionable()
        pending = [p for p in actionable if not p.acted or p.street_bet != self.current_bet]
        if len(actionable) == 1 and actionable[0].street_bet >= self.current_bet:
            pending = []
        if not pending:
            self.next_street()
        else:
            self.turn = self.next_after(self.players[last_actor].seat, pending).user_id

    def next_street(self):
        while True:
            if self.stage == "river":
                self.showdown()
                return
            self.stage = {"preflop": "flop", "flop": "turn", "turn": "river"}[self.stage]
            self.deck.pop()  # burn
            self.board.extend(self.deck.pop() for _ in range(3 if self.stage == "flop" else 1))
            for p in self.players.values():
                p.street_bet = 0
                p.acted = False
                p.can_raise = True
            self.current_bet = 0
            self.min_raise = self.big_blind
            if len(self.actionable()) >= 2:
                self.turn = self.next_after(self.dealer, self.actionable()).user_id
                return
            # Nobody can bet any further: deal remaining board and resolve.
            if self.stage == "river":
                self.showdown()
                return

    def showdown(self):
        contributions = {p.user_id: p.committed for p in self.players.values()}
        hands = {p.user_id: p.hole for p in self.live()}
        order = sorted(self.seated(), key=lambda p: (p.seat - self.dealer - 1) % 9)
        self.payouts = settle_pots(contributions, hands, self.board, [p.user_id for p in order])
        for user_id, chips in self.payouts.items():
            self.players[user_id].stack += chips
        self.stage = "complete"
        self.turn = 0

    def reset(self):
        if self.stage != "complete":
            raise ValueError("Hand not complete")
        self.players = {uid: p for uid, p in self.players.items() if p.stack > 0}
        for p in self.players.values():
            p.hole = ()
            p.ready = False
        self.stage = "waiting"
        self.board = []
        self.deck = []
        self.turn = 0
