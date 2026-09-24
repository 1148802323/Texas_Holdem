from __future__ import annotations
import random
import secrets
from typing import List
from .cards import Card, Rank, Suit


class Deck:
    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed) if seed is not None else secrets.SystemRandom()
        self._cards: List[Card] = [Card(r, s) for s in Suit for r in Rank]
        self.shuffle()

    def shuffle(self) -> None:
        self._rng.shuffle(self._cards)

    def remaining(self) -> int:
        return len(self._cards)

    def deal(self, n: int = 1) -> List[Card]:
        if n < 0:
            raise ValueError("n must be >= 0")
        if n > len(self._cards):
            raise ValueError("Not enough cards in deck")
        if n == 0:
            return []
        out = self._cards[-n:]
        del self._cards[-n:]
        return out

    def burn(self) -> Card:
        return self.deal(1)[0]
