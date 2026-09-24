from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, IntEnum


class Suit(str, Enum):
    CLUBS = "♣"
    DIAMONDS = "♦"
    HEARTS = "♥"
    SPADES = "♠"

    @property
    def short(self) -> str:
        return {
            Suit.CLUBS: "c",
            Suit.DIAMONDS: "d",
            Suit.HEARTS: "h",
            Suit.SPADES: "s",
        }[self]


class Rank(IntEnum):
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6
    SEVEN = 7
    EIGHT = 8
    NINE = 9
    TEN = 10
    JACK = 11
    QUEEN = 12
    KING = 13
    ACE = 14

    @property
    def label(self) -> str:
        return {
            Rank.TWO: "2",
            Rank.THREE: "3",
            Rank.FOUR: "4",
            Rank.FIVE: "5",
            Rank.SIX: "6",
            Rank.SEVEN: "7",
            Rank.EIGHT: "8",
            Rank.NINE: "9",
            Rank.TEN: "T",
            Rank.JACK: "J",
            Rank.QUEEN: "Q",
            Rank.KING: "K",
            Rank.ACE: "A",
        }[self]


@dataclass(frozen=True, slots=True)
class Card:
    rank: Rank
    suit: Suit

    def __str__(self) -> str:
        # 例如：A♠
        return f"{self.rank.label}{self.suit.value}"

    def code(self) -> str:
        # 例如：As, Td
        return f"{self.rank.label}{self.suit.short}"

    @classmethod
    def from_code(cls, code: str) -> Card:
        if len(code) != 2:
            raise ValueError("Invalid card code")
        ranks = {rank.label: rank for rank in Rank}
        suits = {suit.short: suit for suit in Suit}
        try:
            return cls(ranks[code[0]], suits[code[1]])
        except KeyError as exc:
            raise ValueError("Invalid card code") from exc
