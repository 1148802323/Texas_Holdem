"""Transport-independent, stepwise no-limit Hold'em engine.

Commands for one table must be serialized by the caller. Private snapshots must
be stored securely and never returned by a player-facing endpoint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Callable
from uuid import uuid4

from .actions import Action, ActionRecord, ActionType, LegalActions
from .cards import Card
from .deck import Deck
from .evaluator import evaluate_7
from .player import Player
from .table import Table


class InvalidAction(ValueError):
    """Rejected command; no state has changed."""


@dataclass(frozen=True)
class GameState:
    hand_id: str
    street: str
    community: tuple[Card, ...]
    pot: int
    button_index: int
    to_act_index: int | None
    current_bet: int
    last_raise_size: int
    stacks: tuple[int, ...]
    bet_this_street: tuple[int, ...]
    contributed_total: tuple[int, ...]
    folded: tuple[bool, ...]
    all_in: tuple[bool, ...]
    history: tuple[ActionRecord, ...]
    version: int


StrategyFn = Callable[[GameState, Player, LegalActions], Action]


class HoldemGame:
    STREETS = ("preflop", "flop", "turn", "river")

    def __init__(self, players: list[Player], small_blind: int = 1,
                 big_blind: int = 2, seed: int | None = None) -> None:
        if not 2 <= len(players) <= 9:
            raise ValueError("A table needs 2 to 9 players")
        if (type(small_blind) is not int or type(big_blind) is not int or
                not 0 < small_blind < big_blind):
            raise ValueError("Blinds must be positive integers with small < big")
        if any(type(p.stack) is not int or p.stack < 0 for p in players):
            raise ValueError("Stacks must be nonnegative integers")
        if len({p.player_id for p in players}) != len(players):
            raise ValueError("Player IDs must be unique")
        self.players = players
        self.sb, self.bb, self.seed = small_blind, big_blind, seed
        self.deck = Deck(seed=seed)
        self.table = Table()
        self.button_index = 0
        self.hand_number = 0
        self.hand_id: str | None = None
        self.street = "idle"
        self.to_act_index: int | None = None
        self.deadline_at: float | None = None
        self.version = 0
        self.history: list[ActionRecord] = []
        self.bet_this_street = [0] * len(players)
        self.contributed_total = [0] * len(players)
        self.current_bet = 0
        self.last_raise_size = big_blind
        self.pending: set[int] = set()
        self.acted_at_bet: dict[int, int] = {}
        self.participants: set[int] = set()
        self.payouts = [0] * len(players)
        self.refunds = [0] * len(players)
        self.showdown: list[int] = []

    def _live(self) -> list[int]:
        return [i for i in sorted(self.participants) if not self.players[i].folded]

    def _can_act(self) -> set[int]:
        return {i for i in self._live() if not self.players[i].all_in}

    def _next_in(self, start: int, seats: set[int]) -> int:
        for offset in range(len(self.players)):
            i = (start + offset) % len(self.players)
            if i in seats:
                return i
        raise RuntimeError("No eligible seat")

    def _blinds(self) -> tuple[int, int]:
        sb = (self.button_index if len(self.participants) == 2 else
              self._next_in(self.button_index + 1, self.participants))
        return sb, self._next_in(sb + 1, self.participants)

    def _take(self, i: int, amount: int) -> int:
        paid = min(amount, self.players[i].stack)
        self.players[i].stack -= paid
        self.players[i].all_in = self.players[i].stack == 0
        self.bet_this_street[i] += paid
        self.contributed_total[i] += paid
        self.table.pot += paid
        return paid

    def start_new_hand(self, hand_id: str | None = None) -> GameState:
        if self.street not in ("idle", "complete"):
            raise InvalidAction("Current hand is still in progress")
        participants = {i for i, p in enumerate(self.players) if p.stack > 0}
        if len(participants) < 2:
            raise InvalidAction("At least two players with chips are required")
        self.participants = participants
        if self.hand_number:
            self.button_index = self._next_in(self.button_index + 1, participants)
        elif self.button_index not in participants:
            self.button_index = self._next_in(self.button_index, participants)
        self.hand_number += 1
        self.hand_id = hand_id or uuid4().hex
        # A seed is for repeatable tests; successive hands still get different decks.
        self.deck = Deck(seed=None if self.seed is None else self.seed + self.hand_number - 1)
        self.table.reset()
        self.history.clear()
        for i, p in enumerate(self.players):
            p.reset_for_new_hand()
            p.folded = i not in participants
        self.bet_this_street = [0] * len(self.players)
        self.contributed_total = [0] * len(self.players)
        self.payouts = [0] * len(self.players)
        self.refunds = [0] * len(self.players)
        self.showdown = []
        self.pending.clear()
        self.acted_at_bet.clear()
        self.last_raise_size = self.bb
        self.current_bet = 0
        self.street = "preflop"
        self.deadline_at = None
        sb, bb = self._blinds()
        self._take(sb, self.sb)
        self._take(bb, self.bb)
        self.current_bet = self.bb  # A short blind does not reduce the bring-in.
        for _ in range(2):
            for i in sorted(participants):
                self.players[i].hole.extend(self.deck.deal())
        self.pending = self._can_act()
        first = sb if len(participants) == 2 else bb + 1
        self.to_act_index = self._next_in(first, self.pending) if self.pending else None
        self._advance_if_ready(start=first)
        self.version += 1
        return self.game_state()

    def game_state(self) -> GameState:
        return GameState(
            self.hand_id or "", self.street, tuple(self.table.community), self.table.pot,
            self.button_index, self.to_act_index, self.current_bet, self.last_raise_size,
            tuple(p.stack for p in self.players), tuple(self.bet_this_street),
            tuple(self.contributed_total), tuple(p.folded for p in self.players),
            tuple(p.all_in for p in self.players), tuple(self.history), self.version,
        )

    def _seat(self, player_id: str) -> int:
        for i, p in enumerate(self.players):
            if p.player_id == player_id:
                return i
        raise InvalidAction("Unknown player")

    def set_deadline(self, deadline_at: float | None) -> None:
        """Attach an absolute UTC timestamp chosen by the room service."""
        if self.to_act_index is None:
            raise InvalidAction("No player is waiting to act")
        if deadline_at is not None and (not isinstance(deadline_at, (int, float)) or
                                        not isfinite(deadline_at)):
            raise ValueError("Deadline must be a finite UTC timestamp or None")
        self.deadline_at = deadline_at
        self.version += 1

    def legal_actions(self, player_id: str) -> LegalActions:
        i = self._seat(player_id)
        if i != self.to_act_index or self.street not in self.STREETS:
            raise InvalidAction("It is not this player's turn")
        p = self.players[i]
        to_call = max(0, self.current_bet - self.bet_this_street[i])
        # Short all-ins only reopen action once cumulative increments faced
        # since the player's previous action reach a full raise.
        raise_open = (i not in self.acted_at_bet or
                      self.current_bet - self.acted_at_bet[i] >= self.last_raise_size)
        others_can_act = bool(self._can_act() - {i})
        max_to = self.bet_this_street[i] + p.stack
        can_raise = self.current_bet > 0 and max_to > self.current_bet and raise_open and others_can_act
        can_bet = self.current_bet == 0 and p.stack > 0 and others_can_act
        return LegalActions(
            to_call, True, to_call == 0, to_call > 0 and p.stack > 0,
            can_bet, can_raise,
            min(self.bb, p.stack) if can_bet else None,
            p.stack if can_bet else None,
            self.current_bet + self.last_raise_size if can_raise else None,
            max_to if can_raise else None,
        )

    def submit_action(self, player_id: str, action: Action,
                      expected_version: int | None = None) -> GameState:
        if expected_version is not None and expected_version != self.version:
            raise InvalidAction("Stale game version")
        legal = self.legal_actions(player_id)
        i = self.to_act_index
        assert i is not None
        if not isinstance(action, Action) or not isinstance(action.type, ActionType):
            raise InvalidAction("Unknown action")
        if type(action.amount) is not int:
            raise InvalidAction("Amount must be an integer")
        if action.type in (ActionType.FOLD, ActionType.CHECK, ActionType.CALL) and action.amount != 0:
            raise InvalidAction("This action does not take an amount")
        allowed = {
            ActionType.FOLD: legal.can_fold, ActionType.CHECK: legal.can_check,
            ActionType.CALL: legal.can_call, ActionType.BET: legal.can_bet,
            ActionType.RAISE: legal.can_raise,
        }
        if not allowed[action.type]:
            raise InvalidAction(f"{action.type.value} is not legal now")
        if action.type == ActionType.BET:
            assert legal.min_bet is not None and legal.max_bet is not None
            if not legal.min_bet <= action.amount <= legal.max_bet:
                raise InvalidAction("Bet amount is outside the legal range")
        if action.type == ActionType.RAISE:
            assert legal.min_raise_to is not None and legal.max_raise_to is not None
            if not (legal.min_raise_to <= action.amount <= legal.max_raise_to or
                    action.amount == legal.max_raise_to and action.amount > self.current_bet):
                raise InvalidAction("Raise must reach the minimum or be a short all-in")
        old_bet, old_min_raise = self.current_bet, self.last_raise_size
        old_actor_bet = self.bet_this_street[i]
        paid = 0
        if action.type == ActionType.FOLD:
            self.players[i].folded = True
        elif action.type == ActionType.CALL:
            paid = self._take(i, legal.to_call)
        elif action.type == ActionType.BET:
            paid = self._take(i, action.amount)
            self.current_bet = self.bet_this_street[i]
            self.last_raise_size = max(self.bb, self.current_bet)
        elif action.type == ActionType.RAISE:
            paid = self._take(i, action.amount - old_actor_bet)
            self.current_bet = self.bet_this_street[i]
            if self.current_bet - old_bet >= old_min_raise:
                self.last_raise_size = self.current_bet - old_bet
        full_raise = action.type == ActionType.BET or (
            action.type == ActionType.RAISE and self.current_bet - old_bet >= old_min_raise)
        self.pending.discard(i)
        if action.type in (ActionType.BET, ActionType.RAISE):
            if full_raise:
                self.acted_at_bet.clear()
                self.pending = self._can_act() - {i}
            else:
                self.pending |= {j for j in self._can_act() - {i}
                                 if self.bet_this_street[j] < self.current_bet}
        self.acted_at_bet[i] = self.current_bet
        self.history.append(ActionRecord(
            self.street, i, self.players[i].name, action.type.value, action.amount,
            legal.to_call, self.table.pot, self.current_bet, paid,
        ))
        self._advance_if_ready(start=i + 1)
        self.deadline_at = None
        self.version += 1
        return self.game_state()

    def _advance_if_ready(self, start: int) -> None:
        while True:
            live = self._live()
            if len(live) == 1:
                self._finish_uncontested(live[0])
                return
            self.pending &= self._can_act()
            if self.pending:
                # The lone actionable player need only respond to a wager.
                if len(self._can_act()) > 1 or any(
                    self.current_bet > self.bet_this_street[i] for i in self.pending
                ):
                    self.to_act_index = self._next_in(start, self.pending)
                    return
                self.pending.clear()
            if len(self._can_act()) <= 1:
                self._run_out_board()
                self._settle_showdown()
                return
            if self.street == "river":
                self._settle_showdown()
                return
            self._deal_next_street()
            start = self.button_index + 1

    def _deal_next_street(self) -> None:
        next_street = self.STREETS[self.STREETS.index(self.street) + 1]
        if next_street == "flop":
            self.table.deal_flop(self.deck)
        elif next_street == "turn":
            self.table.deal_turn(self.deck)
        else:
            self.table.deal_river(self.deck)
        self.street = next_street
        self.bet_this_street = [0] * len(self.players)
        self.current_bet = 0
        self.last_raise_size = self.bb
        self.acted_at_bet.clear()
        self.pending = self._can_act()

    def _run_out_board(self) -> None:
        while self.street != "river":
            self._deal_next_street()

    def _refund_uncalled(self) -> None:
        levels = sorted(self.contributed_total, reverse=True)
        if levels[0] == levels[1]:
            return
        i = self.contributed_total.index(levels[0])
        excess = levels[0] - levels[1]
        self.contributed_total[i] -= excess
        self.players[i].stack += excess
        self.table.pot -= excess
        self.refunds[i] += excess

    def _finish_uncontested(self, winner: int) -> None:
        self._refund_uncalled()
        self.payouts[winner] += self.table.pot
        self.players[winner].stack += self.table.pot
        self._finish()

    def _settle_showdown(self) -> None:
        self._refund_uncalled()
        self.showdown = self._live()
        scores = {i: evaluate_7(self.players[i].hole + self.table.community).score
                  for i in self.showdown}
        previous = 0
        for level in sorted({amount for amount in self.contributed_total if amount > 0}):
            involved = [i for i, amount in enumerate(self.contributed_total) if amount >= level]
            eligible = [i for i in involved if i in scores]
            pot = (level - previous) * len(involved)
            previous = level
            if not eligible:
                raise RuntimeError("Side pot has no eligible player")
            best = max(scores[i] for i in eligible)
            winners = [i for i in eligible if scores[i] == best]
            share, remainder = divmod(pot, len(winners))
            winners.sort(key=lambda i: (i - self.button_index - 1) % len(self.players))
            for position, i in enumerate(winners):
                amount = share + (position < remainder)
                self.players[i].stack += amount
                self.payouts[i] += amount
        self._finish()

    def _finish(self) -> None:
        self.street = "complete"
        self.to_act_index = None
        self.pending.clear()
        self.deadline_at = None
        self.table.pot = 0
        self.bet_this_street = [0] * len(self.players)
        self.current_bet = 0

    def settle_hand(self) -> GameState:
        """Settle a restored hand that is ready; normal actions settle automatically."""
        if self.street not in self.STREETS:
            raise InvalidAction("There is no active hand to settle")
        live = self._live()
        can_act = self._can_act()
        if len(live) == 1:
            self._finish_uncontested(live[0])
        elif len(can_act) <= 1:
            if any(self.current_bet > self.bet_this_street[i] for i in self.pending):
                raise InvalidAction("A player must respond before settlement")
            self._run_out_board()
            self._settle_showdown()
        elif self.street == "river" and not self.pending:
            self._settle_showdown()
        else:
            raise InvalidAction("Betting is not complete")
        self.version += 1
        return self.game_state()

    def state_for_player(self, player_id: str) -> dict:
        """Only this JSON-safe view is suitable for a player-facing API."""
        seat = self._seat(player_id)
        return {
            "hand_id": self.hand_id, "hand_number": self.hand_number,
            "version": self.version, "street": self.street,
            "button_index": self.button_index, "to_act_index": self.to_act_index,
            "deadline_at": self.deadline_at,
            "community": [c.code() for c in self.table.community],
            "pot": self.table.pot, "current_bet": self.current_bet,
            "players": [
                {"player_id": p.player_id, "name": p.name, "seat": i,
                 "stack": p.stack, "bet_this_street": self.bet_this_street[i],
                 "folded": p.folded, "all_in": p.all_in,
                 "hole": [c.code() for c in p.hole] if i == seat or
                 (self.street == "complete" and i in self.showdown) else None}
                for i, p in enumerate(self.players)
            ],
            "history": [asdict(record) for record in self.history],
            "payouts": self.payouts[:] if self.street == "complete" else None,
            "refunds": self.refunds[:] if self.street == "complete" else None,
            "legal_actions": asdict(self.legal_actions(player_id)) if self.to_act_index == seat else None,
        }

    def export_private_snapshot(self) -> dict:
        """Sensitive JSON-safe persistence data; never publish this to players."""
        return {
            "small_blind": self.sb, "big_blind": self.bb, "seed": self.seed,
            "players": [{"name": p.name, "stack": p.stack, "player_id": p.player_id,
                         "hole": [c.code() for c in p.hole], "folded": p.folded,
                         "all_in": p.all_in} for p in self.players],
            "deck": [c.code() for c in self.deck._cards],
            "community": [c.code() for c in self.table.community],
            "burn_pile": [c.code() for c in self.table.burn_pile],
            "pot": self.table.pot, "button_index": self.button_index,
            "hand_number": self.hand_number, "hand_id": self.hand_id,
            "street": self.street, "to_act_index": self.to_act_index,
            "deadline_at": self.deadline_at, "version": self.version,
            "history": [asdict(record) for record in self.history],
            "bet_this_street": self.bet_this_street[:],
            "contributed_total": self.contributed_total[:],
            "current_bet": self.current_bet, "last_raise_size": self.last_raise_size,
            "pending": sorted(self.pending), "acted_at_bet": self.acted_at_bet.copy(),
            "participants": sorted(self.participants), "payouts": self.payouts[:],
            "refunds": self.refunds[:], "showdown": self.showdown[:],
        }

    @classmethod
    def from_private_snapshot(cls, snapshot: dict) -> HoldemGame:
        players = [Player(p["name"], p["stack"], p["player_id"],
                          [Card.from_code(c) for c in p["hole"]],
                          p["folded"], p["all_in"]) for p in snapshot["players"]]
        game = cls(players, snapshot["small_blind"], snapshot["big_blind"], snapshot["seed"])
        game.deck._cards = [Card.from_code(c) for c in snapshot["deck"]]
        game.table.community = [Card.from_code(c) for c in snapshot["community"]]
        game.table.burn_pile = [Card.from_code(c) for c in snapshot["burn_pile"]]
        game.table.pot = snapshot["pot"]
        for key in ("button_index", "hand_number", "hand_id", "street", "to_act_index",
                    "deadline_at", "version", "bet_this_street", "contributed_total",
                    "current_bet", "last_raise_size", "payouts", "refunds", "showdown"):
            setattr(game, key, snapshot[key])
        game.history = [ActionRecord(**record) for record in snapshot["history"]]
        game.pending = set(snapshot["pending"])
        game.acted_at_bet = {int(i): amount for i, amount in snapshot["acted_at_bet"].items()}
        game.participants = set(snapshot["participants"])
        return game

    def play_hand(self, strategies: dict[int, StrategyFn], default_strategy: StrategyFn,
                  verbose: bool = False) -> None:
        """Compatibility adapter for the original local simulation."""
        self.start_new_hand()
        while self.street != "complete":
            i = self.to_act_index
            assert i is not None
            action = strategies.get(i, default_strategy)(
                self.game_state(), self.players[i], self.legal_actions(self.players[i].player_id))
            self.submit_action(self.players[i].player_id, action)
            if verbose:
                record = self.history[-1]
                print(f"[{record.street}] {record.player_name}: {record.action} {record.amount}")
        if verbose:
            print("Payouts:", self.payouts)

    def pretty_print_history(self) -> None:
        for record in self.history:
            print(f"[{record.street}] seat{record.player_index} {record.player_name}: "
                  f"{record.action} {record.amount} pot={record.pot_after}")
