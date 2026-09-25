from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class ActionType(str, Enum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"       # amount = bet size
    RAISE = "raise"   # amount = raise_to (this street total)


@dataclass(frozen=True, slots=True)
class Action:
    type: ActionType
    amount: int = 0   # BET: bet size; RAISE: raise_to; others ignored


@dataclass(frozen=True, slots=True)
class ActionRecord:
    street: str
    player_index: int
    player_name: str
    action: str
    amount: int
    to_call: int
    pot_after: int
    current_bet: int
    paid: int = 0  # chips actually moved by this action
    all_in: bool = False


@dataclass(frozen=True, slots=True)
class LegalActions:
    """
    to_call: 需要补齐到 current_bet 的差额
    BET: [min_bet, max_bet]
    RAISE: [min_raise_to, max_raise_to] (raise_to是“本街该玩家总投入”)
    """
    to_call: int
    can_fold: bool
    can_check: bool
    can_call: bool
    can_bet: bool
    can_raise: bool

    min_bet: int | None = None
    max_bet: int | None = None

    min_raise_to: int | None = None
    max_raise_to: int | None = None
